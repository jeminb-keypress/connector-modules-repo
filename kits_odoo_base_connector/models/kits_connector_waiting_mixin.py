# -*- coding: utf-8 -*-
"""Reusable waiting-list states, batching, and retry helpers."""

import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

KITS_WAITING_BATCH_TRIGGER = 50
KITS_WAITING_MAX_ATTEMPTS = 3
KITS_WAITING_PROCESS_BATCH = 50


class KitsConnectorWaitingMixin(models.AbstractModel):
    """Mixin for platform waiting-list / staging queue models.

    Concrete models must define ``_name``, ``instance_id`` (Many2one to their
    platform instance), and implement ``_apply_one``.
    """

    _name = 'kits.connector.waiting.mixin'
    _description = 'Kits Connector Waiting List Mixin'
    _order = 'id asc'

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
    max_attempts = fields.Integer(default=KITS_WAITING_MAX_ATTEMPTS)
    last_error = fields.Text(copy=False)
    processed_at = fields.Datetime(readonly=True, copy=False)
    action_required = fields.Boolean(
        string='Action Required',
        compute='_compute_action_required',
        search='_search_action_required',
        help='True when the row needs manual review (state = manual).',
    )

    def _compute_action_required(self):
        for row in self:
            row.action_required = row.state == 'manual'

    def _search_action_required(self, operator, value):
        if operator not in ('=', '!='):
            return NotImplemented
        wants = bool(value) if operator == '=' else not bool(value)
        return [('state', '=', 'manual')] if wants else [('state', '!=', 'manual')]

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
        self.flush_recordset()
        return self._kits_process_entries()

    def _kits_claim_row(self):
        """Atomically claim this row. Returns empty if already claimed."""
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            f"""
            UPDATE {self._table}
               SET state = 'processing',
                   attempt_count = attempt_count + 1
             WHERE id = %s
               AND state IN ('pending', 'failed', 'manual')
         RETURNING id
            """,
            (self.id,),
        )
        claimed = self.env.cr.fetchone()
        if not claimed:
            return self.browse()
        self.invalidate_recordset()
        return self

    def _kits_mark_attempt_failure(self, message, *, permanent=False):
        self.ensure_one()
        if permanent or self.attempt_count >= (self.max_attempts or KITS_WAITING_MAX_ATTEMPTS):
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

    def _kits_process_entries(self):
        """Process claimed waiting rows. Subclasses implement ``_apply_one``."""
        done = failed = skipped = manual = 0
        for row in self:
            claimed = row._kits_claim_row()
            if not claimed:
                continue
            try:
                result = claimed._apply_one()
            except Exception as exc:  # noqa: BLE001
                _logger.exception('Waiting list failed for record %s', claimed.id)
                claimed._kits_mark_attempt_failure(str(exc))
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

    def _apply_one(self):
        raise NotImplementedError

    @api.model
    def _kits_waiting_batch_limit(self):
        return KITS_WAITING_PROCESS_BATCH

    @api.model
    def _kits_waiting_max_attempts(self):
        return KITS_WAITING_MAX_ATTEMPTS
