# -*- coding: utf-8 -*-
"""Product waiting list / staging queue (WooCommerce → Odoo).

Bulk import, webhooks, scheduled import, and order-driven fetches enqueue here.
Matching and product writes run in the waiting-list processor (cron, sync jobs,
or the 50-row batch threshold). Image downloads for normal imports run in a
separate image job; order-driven rows download images immediately.
"""

import logging
from datetime import datetime

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

WAITING_BATCH_TRIGGER = 50
WAITING_MAX_ATTEMPTS = 3
WAITING_PROCESS_BATCH = 50
IMAGE_PROCESS_BATCH = 100


class KitsWooProductWaiting(models.Model):
    _name = 'kits.woo.product.waiting'
    _description = 'WooCommerce Product Waiting List'
    _order = 'id asc'
    _check_company_auto = True
    _rec_name = 'external_id'

    instance_id = fields.Many2one(
        'kits.woo.instance',
        string='WooCommerce Store',
        required=True,
        index=True,
        ondelete='cascade',
        domain="[('platform_type', '=', 'woocommerce')]",
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        index=True,
        default=lambda self: self.env.company,
    )
    external_id = fields.Char(
        string='WooCommerce Product ID',
        required=True,
        index=True,
    )
    product_name = fields.Char(string='Product Name')
    payload = fields.Json(
        string='Product Payload',
        help='Incoming WooCommerce product JSON including variations when fetched.',
    )
    source = fields.Selection(
        selection=[
            ('bulk_import', 'Bulk Import'),
            ('webhook', 'Webhook'),
            ('order', 'Order'),
            ('specific', 'Specific IDs'),
            ('scheduled', 'Scheduled'),
        ],
        required=True,
        index=True,
        default='bulk_import',
    )
    event_type = fields.Selection(
        selection=[
            ('create', 'Create'),
            ('update', 'Update'),
            ('delete', 'Delete'),
        ],
        required=True,
        index=True,
        default='update',
    )
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('processing', 'Processing'),
            ('done', 'Done'),
            ('failed', 'Failed'),
            ('cancelled', 'Cancelled'),
        ],
        default='draft',
        required=True,
        index=True,
        copy=False,
    )
    image_state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('done', 'Done'),
        ],
        default='done',
        required=True,
        index=True,
        copy=False,
    )
    attempt_count = fields.Integer(default=0, copy=False)
    max_attempts = fields.Integer(default=WAITING_MAX_ATTEMPTS)
    last_error = fields.Text(copy=False)
    event_timestamp = fields.Datetime(
        string='Remote Modification Date',
        help='Remote modification time used to protect event ordering.',
        index=True,
    )
    delivery_id = fields.Char(index=True, copy=False)
    body_hash = fields.Char(copy=False)
    product_tmpl_id = fields.Many2one(
        'product.template',
        string='Odoo Product',
        copy=False,
    )
    skip_existing = fields.Boolean(copy=False)
    product_mode = fields.Char(copy=False)
    sync_images_now = fields.Boolean(
        string='Download Images Immediately',
        default=False,
        copy=False,
        help='When set (order import), images download inside product processing.',
    )
    processed_at = fields.Datetime(readonly=True, copy=False)
    processing_job_id = fields.Many2one(
        'kits.connector.sync.job',
        string='Processing Job',
        copy=False,
        ondelete='set null',
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('instance_id') and not vals.get('company_id'):
                instance = self.env['kits.woo.instance'].browse(vals['instance_id'])
                vals['company_id'] = instance.company_id.id
            if vals.get('external_id') is not None:
                vals['external_id'] = str(vals['external_id'])
        return super().create(vals_list)

    @api.model
    def _preview_from_payload(self, payload):
        payload = payload or {}
        return (payload.get('name') or '').strip() or False

    @api.model
    def _parse_event_timestamp(self, payload, fallback=None):
        raw = None
        if isinstance(payload, dict):
            raw = payload.get('date_modified_gmt') or payload.get('date_modified')
        if not raw:
            return fallback or fields.Datetime.now()
        text = str(raw).replace('T', ' ').replace('Z', '')
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
            try:
                return fields.Datetime.to_datetime(datetime.strptime(text[:26], fmt))
            except (TypeError, ValueError):
                continue
        try:
            return fields.Datetime.to_datetime(text)
        except Exception:  # noqa: BLE001
            return fallback or fields.Datetime.now()

    # -------------------------------------------------------------------------
    # Enqueue / dedupe
    # -------------------------------------------------------------------------

    @api.model
    def _enqueue(
        self,
        instance,
        *,
        external_id,
        payload=None,
        source='bulk_import',
        event_type='update',
        delivery_id=False,
        body_hash=False,
        event_timestamp=False,
        process_delete_now=False,
        trigger_batch=True,
        skip_existing=False,
        product_mode=False,
        sync_images_now=False,
    ):
        """Create or refresh a waiting entry for store + external product id."""
        if not instance or not external_id:
            raise UserError(self.env._('Store and WooCommerce product id are required.'))
        external_id = str(external_id)
        payload = payload if isinstance(payload, dict) else {}
        event_ts = event_timestamp or self._parse_event_timestamp(payload)
        preview_name = self._preview_from_payload(payload)

        processing = self.sudo().search([
            ('instance_id', '=', instance.id),
            ('external_id', '=', external_id),
            ('state', '=', 'processing'),
        ], order='id desc', limit=1)
        if processing:
            if (
                processing.event_timestamp
                and event_ts
                and event_ts < processing.event_timestamp
                and event_type != 'delete'
            ):
                return processing
            row = self.sudo().create({
                'instance_id': instance.id,
                'company_id': instance.company_id.id,
                'external_id': external_id,
                'payload': payload,
                'product_name': preview_name,
                'source': source,
                'event_type': event_type,
                'state': 'draft',
                'event_timestamp': event_ts,
                'delivery_id': delivery_id or False,
                'body_hash': body_hash or False,
                'skip_existing': bool(skip_existing),
                'product_mode': product_mode or False,
                'sync_images_now': bool(sync_images_now),
                'image_state': 'done',
            })
            if trigger_batch:
                self._maybe_trigger_batch(instance)
            return row

        open_states = ('draft', 'failed')
        existing = self.sudo().search([
            ('instance_id', '=', instance.id),
            ('external_id', '=', external_id),
            ('state', 'in', open_states),
        ], order='id desc', limit=1)

        if existing:
            if (
                existing.event_timestamp
                and event_ts
                and event_ts < existing.event_timestamp
                and event_type != 'delete'
            ):
                return existing
            merged_payload = payload if event_type != 'delete' else (existing.payload or payload)
            name = self._preview_from_payload(merged_payload)
            existing.write({
                'payload': merged_payload,
                'product_name': name or existing.product_name,
                'source': source,
                'event_type': event_type,
                'state': 'draft',
                'event_timestamp': event_ts,
                'delivery_id': delivery_id or existing.delivery_id,
                'body_hash': body_hash or existing.body_hash,
                'skip_existing': bool(skip_existing),
                'product_mode': product_mode or existing.product_mode,
                'sync_images_now': bool(sync_images_now) or existing.sync_images_now,
                'last_error': False,
                'processed_at': False,
            })
            row = existing
        else:
            row = self.sudo().create({
                'instance_id': instance.id,
                'company_id': instance.company_id.id,
                'external_id': external_id,
                'payload': payload,
                'product_name': preview_name,
                'source': source,
                'event_type': event_type,
                'state': 'draft',
                'event_timestamp': event_ts,
                'delivery_id': delivery_id or False,
                'body_hash': body_hash or False,
                'skip_existing': bool(skip_existing),
                'product_mode': product_mode or False,
                'sync_images_now': bool(sync_images_now),
                'image_state': 'done',
            })

        if process_delete_now and event_type == 'delete':
            row._process_entries()
            return row

        if trigger_batch:
            self._maybe_trigger_batch(instance)
        return row

    @api.model
    def _pending_count(self, instance):
        return self.sudo().search_count([
            ('instance_id', '=', instance.id),
            ('state', '=', 'draft'),
        ])

    @api.model
    def _retryable_failed_domain(self, instance=None):
        domain = [
            ('state', '=', 'failed'),
            ('attempt_count', '<', WAITING_MAX_ATTEMPTS),
        ]
        if instance:
            domain.insert(0, ('instance_id', '=', instance.id))
        return domain

    @api.model
    def _maybe_trigger_batch(self, instance):
        if self._pending_count(instance) < WAITING_BATCH_TRIGGER:
            return False
        return self._enqueue_process_job(instance, reason='threshold')

    @api.model
    def _enqueue_process_job(self, instance, reason='scheduled'):
        Job = self.env['kits.connector.sync.job'].sudo()
        existing = Job.search([
            ('instance_id', '=', instance._kits_instance_ref()),
            ('job_type', '=', 'woo_process_product_waiting'),
            ('state', 'in', ('pending', 'running')),
        ], limit=1)
        if existing:
            return existing
        return instance.action_create_sync_job(
            'woo_process_product_waiting',
            name=instance.env._(
                'Process product waiting list (%(store)s / %(reason)s)',
                store=instance.name,
                reason=reason,
            ),
            payload={'reason': reason},
            priority=8,
        )

    @api.model
    def _enqueue_image_job(self, instance, reason='scheduled'):
        Job = self.env['kits.connector.sync.job'].sudo()
        existing = Job.search([
            ('instance_id', '=', instance._kits_instance_ref()),
            ('job_type', '=', 'woo_process_product_images'),
            ('state', 'in', ('pending', 'running')),
        ], limit=1)
        if existing:
            return existing
        return instance.action_create_sync_job(
            'woo_process_product_images',
            name=instance.env._(
                'Process product images (%(store)s / %(reason)s)',
                store=instance.name,
                reason=reason,
            ),
            payload={'reason': reason},
            priority=15,
        )

    @api.model
    def _cron_process_product_waiting(self):
        Waiting = self.sudo()
        draft = Waiting.search([('state', '=', 'draft')])
        retryable = Waiting.search(self._retryable_failed_domain())
        instances = (draft | retryable).mapped('instance_id').filtered(lambda inst: inst.active)
        for instance in instances:
            self._enqueue_process_job(instance, reason='cron')
        return True

    @api.model
    def _cron_process_product_images(self):
        Waiting = self.sudo()
        pending_images = Waiting.search([
            ('state', '=', 'done'),
            ('image_state', '=', 'pending'),
        ])
        instances = pending_images.mapped('instance_id').filtered(lambda inst: inst.active)
        for instance in instances:
            self._enqueue_image_job(instance, reason='cron')
        return True

    # -------------------------------------------------------------------------
    # Processing
    # -------------------------------------------------------------------------

    def action_process_selected(self):
        return self._process_entries()

    def action_retry_failed(self):
        for row in self:
            if row.state not in ('failed',):
                raise UserError(self.env._('Only failed rows can be retried.'))
            row.write({
                'state': 'draft',
                'attempt_count': 0,
                'last_error': False,
                'processed_at': False,
            })
        self.flush_recordset()
        return self._process_entries()

    def action_cancel(self):
        for row in self:
            if row.state in ('done', 'processing'):
                raise UserError(self.env._(
                    'Cannot cancel a product waiting row that is done or processing.'
                ))
            row.write({
                'state': 'cancelled',
                'processed_at': fields.Datetime.now(),
            })
        return True

    def action_open_product(self):
        self.ensure_one()
        if not self.product_tmpl_id:
            raise UserError(self.env._('No Odoo product is linked yet.'))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'product.template',
            'res_id': self.product_tmpl_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_open_store(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'kits.woo.instance',
            'res_id': self.instance_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    @api.model
    def _process_pending_for_instance(self, instance, limit=WAITING_PROCESS_BATCH, job=None):
        limit = min(int(limit or WAITING_PROCESS_BATCH), WAITING_PROCESS_BATCH)
        self.env['kits.woo.product.waiting'].flush_model()
        self.env.cr.execute(
            """
            SELECT id FROM kits_woo_product_waiting
            WHERE instance_id = %s
              AND (
                state = 'draft'
                OR (state = 'failed' AND attempt_count < %s)
              )
            ORDER BY event_timestamp ASC NULLS FIRST, id ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (instance.id, WAITING_MAX_ATTEMPTS, limit),
        )
        ids = [row[0] for row in self.env.cr.fetchall()]
        rows = self.sudo().browse(ids)
        summary = rows._process_entries(job=job)
        remaining = self.sudo().search_count([
            ('instance_id', '=', instance.id),
            '|',
            ('state', '=', 'draft'),
            '&',
            ('state', '=', 'failed'),
            ('attempt_count', '<', WAITING_MAX_ATTEMPTS),
        ])
        if remaining:
            self._enqueue_process_job(instance, reason='continue')
        return summary

    @api.model
    def _process_images_for_instance(self, instance, limit=IMAGE_PROCESS_BATCH, job=None):
        limit = min(int(limit or IMAGE_PROCESS_BATCH), IMAGE_PROCESS_BATCH)
        self.env['kits.woo.product.waiting'].flush_model()
        self.env.cr.execute(
            """
            SELECT id FROM kits_woo_product_waiting
            WHERE instance_id = %s
              AND state = 'done'
              AND image_state = 'pending'
            ORDER BY id ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (instance.id, limit),
        )
        ids = [row[0] for row in self.env.cr.fetchall()]
        rows = self.sudo().browse(ids)
        done = failed = 0
        for row in rows:
            try:
                row._apply_images()
                done += 1
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    'Product image import failed for waiting %s on instance %s',
                    row.id, instance.id,
                )
                row.write({'last_error': str(exc)})
                failed += 1
                instance._kits_log_operation(
                    operation='woo_process_product_images',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f'waiting_id={row.id} woo_product_id={row.external_id}',
                    job=job,
                )
        remaining = self.sudo().search_count([
            ('instance_id', '=', instance.id),
            ('state', '=', 'done'),
            ('image_state', '=', 'pending'),
        ])
        if remaining:
            self._enqueue_image_job(instance, reason='continue')
        return f'Product images processed: done={done}, failed={failed}'

    def _claim_row(self, job=None):
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            """
            UPDATE kits_woo_product_waiting
               SET state = 'processing',
                   processing_job_id = %s,
                   attempt_count = attempt_count + 1
             WHERE id = %s
               AND state IN ('draft', 'failed')
         RETURNING id
            """,
            (job.id if job and getattr(job, 'id', None) else None, self.id),
        )
        claimed = self.env.cr.fetchone()
        if not claimed:
            return self.browse()
        self.invalidate_recordset()
        return self

    def _process_entries(self, job=None):
        done = failed = skipped = cancelled = 0
        created = updated = deleted = 0
        for row in self:
            claimed = row._claim_row(job=job)
            if not claimed:
                continue
            try:
                result = claimed._apply_one(job=job)
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    'Product waiting list failed for %s on instance %s',
                    claimed.external_id, claimed.instance_id.id,
                )
                claimed._mark_attempt_failure(str(exc))
                failed += 1
                continue

            if isinstance(result, dict):
                status = result.get('status')
                outcome = result.get('outcome')
            else:
                status = result
                outcome = False
            if status == 'done':
                done += 1
                if outcome == 'created':
                    created += 1
                elif outcome == 'updated':
                    updated += 1
                elif outcome == 'deleted':
                    deleted += 1
            elif status == 'skipped':
                skipped += 1
            elif status == 'cancelled':
                cancelled += 1
            else:
                failed += 1
        return (
            f'Waiting list processed: done={done}, skipped={skipped}, '
            f'failed={failed}, cancelled={cancelled}, '
            f'created={created}, updated={updated}, deleted={deleted}'
        )

    def _mark_attempt_failure(self, message):
        self.ensure_one()
        # After max attempts remain failed for manual intervention (retry resets count).
        self.write({
            'state': 'failed',
            'last_error': message,
            'processed_at': fields.Datetime.now(),
        })

    def _apply_one(self, job=None):
        self.ensure_one()
        instance = self.instance_id
        if self.event_type == 'delete':
            instance._woo_mark_product_deleted_remote(
                self.external_id,
                message=self.env._(
                    'Archived product binding after waiting-list delete for '
                    'WooCommerce product %(id)s on %(store)s.',
                    id=self.external_id,
                    store=instance.display_name,
                ),
            )
            self.write({
                'state': 'done',
                'image_state': 'done',
                'last_error': False,
                'processed_at': fields.Datetime.now(),
            })
            return {'status': 'done', 'outcome': 'deleted'}

        payload = dict(self.payload or {})
        if not payload.get('id'):
            payload['id'] = int(self.external_id)

        if self.skip_existing or self.product_mode:
            skip_payload = {
                'skip_existing': self.skip_existing,
                'product_mode': self.product_mode,
            }
            if instance._woo_skip_product_import(payload, skip_payload):
                self.write({
                    'state': 'done',
                    'image_state': 'done',
                    'last_error': False,
                    'processed_at': fields.Datetime.now(),
                })
                return {'status': 'skipped', 'outcome': False}

        binding = self.env['kits.woo.product.binding'].with_context(active_test=False)._find(
            instance, external_id=self.external_id,
        )
        if binding and not binding.active:
            binding.write({
                'active': True,
                'exists_in_store': True,
            })
            binding.variant_binding_ids.with_context(active_test=False).filtered(
                lambda b: not b.active
            ).write({'active': True, 'exists_in_store': True})

        if instance._woo_payload_is_stale(binding, payload):
            self.write({
                'state': 'done',
                'image_state': 'done',
                'last_error': self.env._(
                    'Skipped older WooCommerce product event for %(id)s.',
                    id=self.external_id,
                ),
                'processed_at': fields.Datetime.now(),
            })
            return {'status': 'skipped', 'outcome': False}

        sync_images = bool(self.sync_images_now)
        outcome = instance._woo_import_one_product(
            payload,
            job=job,
            waiting=self,
            sync_images=sync_images,
        )
        template = self.env['product.template']
        product_binding = self.env['kits.woo.product.binding']._find(
            instance, external_id=self.external_id,
        )
        if product_binding:
            template = product_binding.product_tmpl_id

        need_images = (
            instance.woo_import_images
            and not sync_images
            and outcome in ('created', 'updated')
        )
        vals = {
            'state': 'done',
            'product_tmpl_id': template.id if template else False,
            'last_error': False,
            'processed_at': fields.Datetime.now(),
            'image_state': 'pending' if need_images else 'done',
        }
        self.write(vals)
        if need_images:
            self._enqueue_image_job(instance, reason='images')
        return {'status': 'done', 'outcome': outcome}

    def _apply_images(self):
        """Download images for a completed waiting row (deferred image job)."""
        self.ensure_one()
        instance = self.instance_id
        if not instance.woo_import_images:
            self.write({'image_state': 'done'})
            return
        payload = dict(self.payload or {})
        product_binding = self.env['kits.woo.product.binding']._find(
            instance, external_id=self.external_id,
        )
        template = self.product_tmpl_id or (
            product_binding.product_tmpl_id if product_binding else self.env['product.template']
        )
        if not template:
            self.write({
                'image_state': 'done',
                'last_error': self.env._(
                    'No Odoo product linked for image import of WooCommerce product %(id)s.',
                    id=self.external_id,
                ),
            })
            return
        instance._woo_sync_template_images(
            template, payload.get('images') or [], binding=product_binding,
        )
        variations = payload.get('variations') or []
        if not variations and (payload.get('type') or 'simple') == 'simple':
            self.write({'image_state': 'done', 'last_error': False})
            return
        for variation in variations:
            if not isinstance(variation, dict):
                continue
            image = variation.get('image') or {}
            external_id = str(variation.get('id') or '')
            if not external_id or not image:
                continue
            variant_binding = self.env['kits.woo.variant.binding']._find(
                instance, external_id=external_id,
            )
            if variant_binding and variant_binding.product_id:
                instance._woo_sync_variant_image(
                    variant_binding.product_id, image, binding=variant_binding,
                )
        self.write({'image_state': 'done', 'last_error': False})
