# -*- coding: utf-8 -*-
"""Coupon waiting list (WooCommerce → Odoo).

Bulk import and coupon webhooks enqueue here. Processing mirrors the customer
waiting-list pattern: ≤50 rows per batch, 3 auto-retries, store isolation.
"""

import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

WAITING_BATCH_TRIGGER = 50
WAITING_MAX_ATTEMPTS = 3
WAITING_PROCESS_BATCH = 50


class KitsWooCouponWaiting(models.Model):
    _name = 'kits.woo.coupon.waiting'
    _description = 'WooCommerce Coupon Waiting List'
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
    external_id = fields.Char(string='WooCommerce Coupon ID', required=True, index=True)
    code = fields.Char(string='Coupon Code', index=True)
    payload = fields.Json(string='Coupon Payload')
    source = fields.Selection(
        selection=[
            ('bulk_import', 'Bulk Import'),
            ('webhook', 'Webhook'),
            ('manual', 'Manual'),
        ],
        required=True,
        default='bulk_import',
        index=True,
    )
    event_type = fields.Selection(
        selection=[
            ('create', 'Create'),
            ('update', 'Update'),
            ('delete', 'Delete'),
        ],
        required=True,
        default='update',
        index=True,
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
    delivery_id = fields.Char(index=True, copy=False)
    body_hash = fields.Char(copy=False)
    coupon_id = fields.Many2one('kits.woo.coupon', string='Odoo Coupon', copy=False)
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
    def _enqueue(self, instance, *, external_id, payload, source='bulk_import',
                 event_type='update', delivery_id=False, body_hash=False,
                 trigger_batch=False):
        external_id = str(external_id)
        code = ((payload or {}).get('code') or '').strip() or False
        open_row = self.search([
            ('instance_id', '=', instance.id),
            ('external_id', '=', external_id),
            ('state', 'in', ('pending', 'failed', 'manual')),
        ], limit=1)
        vals = {
            'payload': payload or {},
            'source': source,
            'event_type': event_type,
            'code': code,
            'state': 'pending',
            'last_error': False,
            'delivery_id': delivery_id or False,
            'body_hash': body_hash or False,
        }
        if open_row:
            open_row.write(vals)
            row = open_row
        else:
            processing = self.search([
                ('instance_id', '=', instance.id),
                ('external_id', '=', external_id),
                ('state', '=', 'processing'),
            ], limit=1)
            if processing:
                row = self.create({
                    'instance_id': instance.id,
                    'company_id': instance.company_id.id,
                    'external_id': external_id,
                    **vals,
                })
            else:
                row = self.create({
                    'instance_id': instance.id,
                    'company_id': instance.company_id.id,
                    'external_id': external_id,
                    **vals,
                })
        if trigger_batch:
            pending = self.search_count([
                ('instance_id', '=', instance.id),
                ('state', '=', 'pending'),
            ])
            if pending >= WAITING_BATCH_TRIGGER:
                self._enqueue_process_job(instance)
        return row

    @api.model
    def _enqueue_process_job(self, instance):
        pending = self.env['kits.connector.sync.job'].search_count([
            ('instance_id', '=', instance._kits_instance_ref()),
            ('job_type', '=', 'woo_process_coupon_waiting'),
            ('state', 'in', ('pending', 'running')),
        ])
        if pending:
            return
        instance.action_create_sync_job(
            'woo_process_coupon_waiting',
            name=instance.env._(
                'WooCommerce: Process Coupon Waiting List (%s)', instance.name,
            ),
        )

    def action_retry(self):
        for row in self:
            if row.state not in ('failed', 'manual'):
                continue
            row.write({
                'state': 'pending',
                'attempt_count': 0,
                'last_error': False,
            })
        instances = self.mapped('instance_id')
        for instance in instances:
            self._enqueue_process_job(instance)
        return True

    @api.model
    def _cron_process_coupon_waiting(self):
        instances = self.env['kits.woo.instance'].search([
            ('platform_type', '=', 'woocommerce'),
            ('active', '=', True),
        ])
        for instance in instances:
            eligible = self.search_count([
                ('instance_id', '=', instance.id),
                '|',
                ('state', '=', 'pending'),
                '&',
                ('state', '=', 'failed'),
                ('attempt_count', '<', WAITING_MAX_ATTEMPTS),
            ])
            if eligible:
                self._enqueue_process_job(instance)

    def _claim_batch(self, instance, limit=WAITING_PROCESS_BATCH):
        self.env.cr.execute(
            """
            SELECT id FROM kits_woo_coupon_waiting
            WHERE instance_id = %s
              AND (
                    state = 'pending'
                    OR (state = 'failed' AND attempt_count < max_attempts)
                  )
            ORDER BY id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (instance.id, limit),
        )
        ids = [row[0] for row in self.env.cr.fetchall()]
        rows = self.browse(ids)
        if rows:
            rows.write({'state': 'processing'})
        return rows

    def _process_entries(self, job=None):
        done = failed = skipped = 0
        for row in self:
            try:
                with self.env.cr.savepoint():
                    result = row._apply_one(job=job)
                if result == 'skipped':
                    skipped += 1
                else:
                    done += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                attempts = row.attempt_count + 1
                state = 'manual' if attempts >= row.max_attempts else 'failed'
                row.write({
                    'state': state,
                    'attempt_count': attempts,
                    'last_error': str(exc),
                })
                _logger.exception(
                    'Coupon waiting list failed for %s on instance %s',
                    row.external_id, row.instance_id.id,
                )
                row.instance_id._kits_log_operation(
                    operation='woo_process_coupon_waiting',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f'kits.woo.coupon,{row.external_id}',
                    job=job,
                )
        return done, failed, skipped

    def _apply_one(self, job=None):
        self.ensure_one()
        instance = self.instance_id
        if self.event_type == 'delete':
            summary = instance._woo_mark_webhook_deleted(
                'kits.woo.coupon', self.external_id, 'coupon.deleted',
            )
            self.write({
                'state': 'done',
                'processed_at': fields.Datetime.now(),
                'last_error': False,
                'processing_job_id': job.id if job else False,
            })
            return 'deleted' if 'deleted' in (summary or '').lower() else 'skipped'

        payload = dict(self.payload or {})
        if not payload.get('id'):
            payload['id'] = int(self.external_id)
        binding = instance._woo_binding()._kits_find_binding(
            instance, 'kits.woo.coupon', external_id=str(payload.get('id')),
        )
        if instance._woo_payload_is_stale(binding, payload):
            self.write({
                'state': 'skipped',
                'processed_at': fields.Datetime.now(),
                'last_error': False,
            })
            return 'skipped'
        result = instance._woo_import_one_coupon(payload)
        coupon = instance._woo_find_odoo('kits.woo.coupon', payload.get('id'))
        self.write({
            'state': 'done',
            'processed_at': fields.Datetime.now(),
            'last_error': False,
            'coupon_id': coupon.id if coupon else False,
            'code': (payload.get('code') or self.code or False),
            'processing_job_id': job.id if job else False,
            'attempt_count': self.attempt_count + 1,
        })
        return result
