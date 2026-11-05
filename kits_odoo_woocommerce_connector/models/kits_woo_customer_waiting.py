# -*- coding: utf-8 -*-
"""Customer waiting list / staging queue (WooCommerce → Odoo only).

Bulk import and webhooks enqueue here. Matching and partner writes run in the
waiting-list processor, triggered by cron (5 min), manual action, sync jobs, or
when 50 eligible pending rows accumulate.
"""

import logging
from datetime import datetime

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

WAITING_BATCH_TRIGGER = 50
WAITING_MAX_ATTEMPTS = 3
WAITING_PROCESS_BATCH = 50


class KitsWooCustomerWaiting(models.Model):
    _name = 'kits.woo.customer.waiting'
    _description = 'WooCommerce Customer Waiting List'
    _order = 'id asc'
    _check_company_auto = True
    _rec_name = 'external_id'

    instance_id = fields.Many2one(
        'kits.connector.instance',
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
        string='WooCommerce Customer ID',
        required=True,
        index=True,
    )
    customer_name = fields.Char(string='Customer Name')
    customer_email = fields.Char(string='Customer Email')
    payload = fields.Json(
        string='Customer Payload',
        help='Incoming WooCommerce customer JSON. Hidden from non-managers via ACL.',
    )
    source = fields.Selection(
        selection=[
            ('bulk_import', 'Bulk Import'),
            ('webhook', 'Webhook'),
            ('order', 'Order'),
            ('fetch', 'Fetch'),
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
            ('pending', 'Pending'),
            ('processing', 'Processing'),
            ('done', 'Completed'),
            ('failed', 'Failed'),
            ('manual', 'Manual Action Required'),
            ('skipped', 'Skipped'),
        ],
        default='pending',
        required=True,
        index=True,
        copy=False,
    )
    attempt_count = fields.Integer(default=0, copy=False)
    max_attempts = fields.Integer(default=WAITING_MAX_ATTEMPTS)
    last_error = fields.Text(copy=False)
    event_timestamp = fields.Datetime(
        string='Event Time',
        help='Remote modification time used to protect event ordering.',
        index=True,
    )
    delivery_id = fields.Char(index=True, copy=False)
    body_hash = fields.Char(copy=False)
    partner_id = fields.Many2one('res.partner', string='Odoo Contact', copy=False)
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
                instance = self.env['kits.connector.instance'].browse(vals['instance_id'])
                vals['company_id'] = instance.company_id.id
            if vals.get('external_id') is not None:
                vals['external_id'] = str(vals['external_id'])
        return super().create(vals_list)

    @api.model
    def _preview_from_payload(self, payload):
        payload = payload or {}
        billing = payload.get('billing') or {}
        first = (payload.get('first_name') or billing.get('first_name') or '').strip()
        last = (payload.get('last_name') or billing.get('last_name') or '').strip()
        name = ' '.join(p for p in (first, last) if p) or False
        email = (payload.get('email') or billing.get('email') or '').strip() or False
        return name, email

    # -------------------------------------------------------------------------
    # Enqueue / dedupe
    # -------------------------------------------------------------------------

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
    ):
        """Create or refresh a waiting entry for store + external customer id."""
        if not instance or not external_id:
            raise UserError(self.env._('Store and WooCommerce customer id are required.'))
        external_id = str(external_id)
        payload = payload if isinstance(payload, dict) else {}
        event_ts = event_timestamp or self._parse_event_timestamp(payload)
        preview_name, preview_email = self._preview_from_payload(payload)

        # Lock existing open rows for this key
        open_states = ('pending', 'processing', 'manual', 'failed')
        existing = self.sudo().search([
            ('instance_id', '=', instance.id),
            ('external_id', '=', external_id),
            ('state', 'in', open_states),
        ], order='id desc', limit=1)

        if existing:
            # Do not overwrite newer pending data with an older event
            if (
                existing.event_timestamp
                and event_ts
                and event_ts < existing.event_timestamp
                and event_type != 'delete'
            ):
                return existing
            # Delete always wins over stale create/update
            merged_payload = payload if event_type != 'delete' else (existing.payload or payload)
            name, email = self._preview_from_payload(merged_payload)
            vals = {
                'payload': merged_payload,
                'customer_name': name or existing.customer_name,
                'customer_email': email or existing.customer_email,
                'source': source,
                'event_type': event_type,
                'state': 'pending',
                'event_timestamp': event_ts,
                'delivery_id': delivery_id or existing.delivery_id,
                'body_hash': body_hash or existing.body_hash,
                'last_error': False,
                'processed_at': False,
            }
            existing.write(vals)
            row = existing
        else:
            row = self.sudo().create({
                'instance_id': instance.id,
                'company_id': instance.company_id.id,
                'external_id': external_id,
                'payload': payload,
                'customer_name': preview_name,
                'customer_email': preview_email,
                'source': source,
                'event_type': event_type,
                'state': 'pending',
                'event_timestamp': event_ts,
                'delivery_id': delivery_id or False,
                'body_hash': body_hash or False,
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
            ('state', '=', 'pending'),
        ])

    @api.model
    def _maybe_trigger_batch(self, instance):
        if self._pending_count(instance) < WAITING_BATCH_TRIGGER:
            return False
        return self._enqueue_process_job(instance, reason='threshold')

    @api.model
    def _enqueue_process_job(self, instance, reason='scheduled'):
        """Reuse sync-job infrastructure to process waiting-list batches."""
        Job = self.env['kits.connector.sync.job'].sudo()
        existing = Job.search([
            ('instance_id', '=', instance.id),
            ('job_type', '=', 'woo_process_customer_waiting'),
            ('state', 'in', ('pending', 'running')),
        ], limit=1)
        if existing:
            return existing
        return instance.action_create_sync_job(
            'woo_process_customer_waiting',
            name=instance.env._(
                'Process customer waiting list (%(store)s / %(reason)s)',
                store=instance.name,
                reason=reason,
            ),
            payload={'reason': reason},
            priority=8,
        )

    @api.model
    def _cron_process_customer_waiting(self):
        """Five-minute cron: enqueue a process job per store with pending rows."""
        Waiting = self.sudo()
        instances = Waiting.read_group(
            [('state', '=', 'pending')],
            ['instance_id'],
            ['instance_id'],
        )
        for group in instances:
            instance_id = group['instance_id'][0] if group.get('instance_id') else False
            if not instance_id:
                continue
            instance = self.env['kits.connector.instance'].browse(instance_id)
            if instance.exists() and instance.active:
                self._enqueue_process_job(instance, reason='cron')
        return True

    # -------------------------------------------------------------------------
    # Processing
    # -------------------------------------------------------------------------

    def action_process_selected(self):
        return self._process_entries()

    def action_retry_failed(self):
        for row in self:
            if row.state not in ('failed', 'manual'):
                raise UserError(self.env._(
                    'Only failed or manual-action rows can be retried.'
                ))
            row.write({
                'state': 'pending',
                'attempt_count': 0,
                'last_error': False,
                'processed_at': False,
            })
        return self._process_entries()

    def action_open_partner(self):
        self.ensure_one()
        if not self.partner_id:
            raise UserError(self.env._('No Odoo contact is linked yet.'))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'res.partner',
            'res_id': self.partner_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_open_store(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'kits.connector.instance',
            'res_id': self.instance_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    @api.model
    def _process_pending_for_instance(self, instance, limit=WAITING_PROCESS_BATCH, job=None):
        rows = self.sudo().search([
            ('instance_id', '=', instance.id),
            ('state', '=', 'pending'),
        ], order='event_timestamp asc, id asc', limit=limit)
        return rows._process_entries(job=job)

    def _process_entries(self, job=None):
        """Process waiting rows with concurrency protection via state flip."""
        done = failed = skipped = manual = 0
        for row in self:
            # Claim the row
            claimed = self.sudo().search([
                ('id', '=', row.id),
                ('state', 'in', ('pending', 'failed', 'manual')),
            ], limit=1)
            if not claimed:
                continue
            claimed.write({
                'state': 'processing',
                'processing_job_id': job.id if job else False,
                'attempt_count': claimed.attempt_count + 1,
            })
            try:
                result = claimed._apply_one()
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    'Customer waiting list failed for %s on instance %s',
                    claimed.external_id, claimed.instance_id.id,
                )
                claimed._mark_attempt_failure(str(exc))
                if claimed.state == 'manual':
                    manual += 1
                else:
                    failed += 1
                continue

            if result == 'done':
                done += 1
            elif result == 'skipped':
                skipped += 1
            elif result == 'manual':
                manual += 1
            else:
                failed += 1
        return (
            f'Waiting list processed: done={done}, skipped={skipped}, '
            f'failed={failed}, manual={manual}'
        )

    def _mark_attempt_failure(self, message, *, permanent=False):
        self.ensure_one()
        if permanent or self.attempt_count >= (self.max_attempts or WAITING_MAX_ATTEMPTS):
            self.write({
                'state': 'manual',
                'last_error': message,
                'processed_at': fields.Datetime.now(),
            })
        else:
            self.write({
                'state': 'failed',
                'last_error': message,
                'processed_at': fields.Datetime.now(),
            })

    def _apply_one(self):
        self.ensure_one()
        instance = self.instance_id
        if self.event_type == 'delete':
            instance._woo_delete_customer_binding(
                self.external_id,
                reason=self.env._(
                    'Removed the customer binding after waiting-list delete for '
                    'WooCommerce customer %(id)s on %(store)s.',
                    id=self.external_id,
                    store=instance.display_name,
                ),
            )
            self.write({
                'state': 'done',
                'last_error': False,
                'processed_at': fields.Datetime.now(),
            })
            return 'done'

        payload = dict(self.payload or {})
        if not payload.get('id'):
            payload['id'] = int(self.external_id)
        result = instance._woo_import_one_customer(payload)
        if result == 'ambiguous':
            self.write({
                'state': 'manual',
                'last_error': self.env._(
                    'Multiple Odoo contacts share this email. Manual review required.'
                ),
                'processed_at': fields.Datetime.now(),
            })
            return 'manual'
        if result == 'skipped':
            self.write({
                'state': 'skipped',
                'last_error': False,
                'processed_at': fields.Datetime.now(),
            })
            return 'skipped'

        partner = instance._woo_find_odoo('res.partner', payload.get('id') or self.external_id)
        # Active binding only — deleted_remotely bindings stay inactive
        binding = self.env['kits.woo.customer.binding']._find(
            instance, external_id=self.external_id,
        )
        if binding and binding.active:
            partner = binding.partner_id
        self.write({
            'state': 'done',
            'partner_id': partner.id if partner else False,
            'last_error': False,
            'processed_at': fields.Datetime.now(),
        })
        return 'done'
