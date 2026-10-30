# -*- coding: utf-8 -*-
"""Safe WooCommerce refund → credit note synchronization."""

import logging

from odoo import fields, models
from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tools.float_utils import float_compare, float_is_zero, float_round

_logger = logging.getLogger(__name__)


class KitsConnectorInstanceRefund(models.Model):
    _inherit = 'kits.connector.instance'

    woo_auto_post_credit_notes = fields.Boolean(
        string='Auto-Post Refund Credit Notes',
        default=False,
        help='When enabled, successfully allocated credit notes are posted. '
             'Payment reconciliation is never invented automatically.',
    )
    woo_refund_ids = fields.One2many(
        'kits.woo.refund',
        'instance_id',
        string='Woo Refunds',
    )

    def _woo_sync_order_refunds(self, order, woo_order, job=None):
        """Sync Woo refunds into tracked credit notes; never invent invoices/payments/stock."""
        self.ensure_one()
        refunds = list(woo_order.get('refunds') or [])
        detailed = []
        if woo_order.get('id'):
            try:
                detailed = list(self._woo_api().get_order_refunds(int(woo_order['id'])))
            except Exception as exc:  # noqa: BLE001
                _logger.info(
                    'Could not fetch refund details for Woo order %s: %s',
                    woo_order.get('id'), exc,
                )
        # Prefer detailed refund payloads (have line_items / amount)
        by_id = {}
        for row in refunds:
            if row.get('id'):
                by_id[str(row['id'])] = dict(row)
        for row in detailed:
            if row.get('id'):
                by_id[str(row['id'])] = {**by_id.get(str(row['id']), {}), **row}
        if not by_id:
            return

        for refund in by_id.values():
            try:
                self._woo_upsert_and_process_refund(order, woo_order, refund, job=job)
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    'Refund sync failed for Woo refund %s on order %s',
                    refund.get('id'), order.id,
                )
                msg = self.env._(
                    'Woo refund #%(id)s sync failed: %(error)s',
                    id=refund.get('id'),
                    error=str(exc),
                )
                order.woo_sync_conflict = msg
                pending = self.env['kits.woo.refund'].search([
                    ('instance_id', '=', self.id),
                    ('woo_refund_id', '=', str(refund.get('id'))),
                ], limit=1)
                if pending and pending.state not in ('done', 'skipped'):
                    pending.write({'state': 'error', 'error_message': msg})

    def _woo_upsert_and_process_refund(self, order, woo_order, refund, job=None):
        self.ensure_one()
        refund_id = refund.get('id')
        if not refund_id:
            return
        Refund = self.env['kits.woo.refund']
        existing = Refund.search([
            ('instance_id', '=', self.id),
            ('woo_refund_id', '=', str(refund_id)),
        ], limit=1)
        amount = abs(float(refund.get('amount') or refund.get('total') or 0.0))
        currency = order.currency_id
        currency_code = (woo_order.get('currency') or '').upper()
        if currency_code:
            found = self.env['res.currency'].search([('name', '=', currency_code)], limit=1)
            if found:
                currency = found
        vals = {
            'instance_id': self.id,
            'sale_order_id': order.id,
            'woo_order_id': str(woo_order.get('id') or order.woo_order_id or ''),
            'woo_refund_id': str(refund_id),
            'amount': amount,
            'currency_id': currency.id,
            'woo_reason': refund.get('reason') or False,
            'line_payload': refund.get('line_items') or [],
        }
        if existing:
            # Idempotent: already done with credit note
            if existing.state == 'done' and existing.credit_note_id:
                # Also ensure legacy binding exists
                self._woo_bind(
                    'account.move',
                    existing.credit_note_id.id,
                    f'refund:{refund_id}',
                    external_data={'woo_order_id': woo_order.get('id'), 'amount': amount},
                )
                return existing
            if existing.state == 'done' and not existing.credit_note_id:
                existing.state = 'pending'
            existing.write({k: v for k, v in vals.items() if k not in ('instance_id', 'woo_refund_id')})
            record = existing
        else:
            # Legacy binding path from previous versions
            legacy = self._woo_find_odoo('account.move', f'refund:{refund_id}')
            if legacy:
                vals.update({
                    'state': 'done',
                    'credit_note_id': legacy.id,
                    'invoice_id': legacy.reversed_entry_id.id if legacy.reversed_entry_id else False,
                })
                return Refund.create(vals)
            record = Refund.create(vals)

        return self._woo_process_refund_record(record, job=job)

    def _woo_process_refund_record(self, record, job=None):
        self.ensure_one()
        record.ensure_one()
        if record.state == 'done' and record.credit_note_id:
            return record

        order = record.sale_order_id
        if not order:
            record.write({
                'state': 'error',
                'error_message': self.env._('Refund has no linked sales order.'),
            })
            return record

        if float_is_zero(record.amount, precision_rounding=record.currency_id.rounding):
            record.write({
                'state': 'skipped',
                'error_message': self.env._('Refund amount is zero.'),
            })
            return record

        if record.currency_id != order.currency_id:
            record.write({
                'state': 'review',
                'error_message': self.env._(
                    'Currency mismatch: refund %(refund_cur)s vs order %(order_cur)s. '
                    'Manual review required.',
                    refund_cur=record.currency_id.name,
                    order_cur=order.currency_id.name,
                ),
            })
            return record

        invoices = order.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted'
        ).sorted(lambda m: (m.invoice_date or m.date or fields.Date.today(), m.id))
        if not invoices:
            draft_inv = order.invoice_ids.filtered(
                lambda m: m.move_type == 'out_invoice' and m.state == 'draft'
            )
            if draft_inv:
                record.write({
                    'state': 'review',
                    'error_message': self.env._(
                        'Order has draft invoice(s) only. Post the invoice before '
                        'creating a credit note, or process manually.'
                    ),
                })
            else:
                record.write({
                    'state': 'skipped',
                    'error_message': self.env._(
                        'No posted Odoo invoice exists for this order. '
                        'A WooCommerce refund does not invent an invoice or payment.'
                    ),
                })
                order.woo_sync_conflict = record.error_message
            return record

        # Remaining refundable = sum of invoice residuals minus pending/done credit notes
        already = self.env['kits.woo.refund'].search([
            ('instance_id', '=', self.id),
            ('sale_order_id', '=', order.id),
            ('id', '!=', record.id),
            ('state', 'in', ('done', 'pending')),
            ('credit_note_id', '!=', False),
        ])
        # Also count this record's amount against residuals
        refundable = sum(invoices.mapped('amount_residual'))
        # amount_residual shrinks when credit notes are posted/reconciled; for draft CNs
        # add back unpaid residual carefully via invoice amounts
        if float_compare(
            record.amount, refundable + 0.0001,
            precision_rounding=record.currency_id.rounding,
        ) > 0 and float_compare(
            record.amount, sum(invoices.mapped('amount_total')),
            precision_rounding=record.currency_id.rounding,
        ) > 0:
            # Soft check: if residual already reduced by prior CNs, compare to total refunded
            prior_amt = sum(already.mapped('amount'))
            max_total = sum(invoices.mapped('amount_total'))
            if float_compare(
                prior_amt + record.amount, max_total,
                precision_rounding=record.currency_id.rounding,
            ) > 0:
                record.write({
                    'state': 'review',
                    'error_message': self.env._(
                        'Refund amount %(amount)s exceeds remaining refundable '
                        'on posted invoices (max total %(max)s, prior synced %(prior)s).',
                        amount=record.amount,
                        max=max_total,
                        prior=prior_amt,
                    ),
                })
                return record

        allocation = self._woo_allocate_refund_to_invoices(record, invoices)
        if not allocation:
            record.write({
                'state': 'review',
                'error_message': self.env._(
                    'Could not allocate refund %(amount)s to invoice lines safely. '
                    'Manual credit note required.',
                    amount=record.amount,
                ),
            })
            return record

        credit_notes = self.env['account.move']
        primary_invoice = self.env['account.move']
        for invoice, portion in allocation:
            primary_invoice = primary_invoice or invoice
            credit = self._woo_create_partial_credit_note(invoice, portion, record)
            if credit:
                credit_notes |= credit

        if not credit_notes:
            record.write({
                'state': 'error',
                'error_message': self.env._('Credit note creation returned no moves.'),
            })
            return record

        # Bind first credit note for legacy external id compatibility
        main = credit_notes[0]
        self._woo_bind(
            'account.move',
            main.id,
            f'refund:{record.woo_refund_id}',
            external_data={
                'woo_order_id': record.woo_order_id,
                'amount': record.amount,
                'credit_note_ids': credit_notes.ids,
            },
        )
        if self.woo_auto_post_credit_notes:
            for move in credit_notes.filtered(lambda m: m.state == 'draft'):
                move.action_post()

        record.write({
            'state': 'done',
            'invoice_id': primary_invoice.id,
            'credit_note_id': main.id,
            'error_message': False,
        })
        self._kits_log_operation(
            operation='woo_import_orders',
            status='success',
            message=self.env._(
                'Synced Woo refund %(refund)s → credit note(s) %(moves)s',
                refund=record.woo_refund_id,
                moves=', '.join(
                    name for name in credit_notes.mapped('name') if name
                ) or ', '.join(str(move_id) for move_id in credit_notes.ids),
            ),
            job=job,
        )
        return record

    def _woo_allocate_refund_to_invoices(self, record, invoices):
        """Return list of (invoice, amount) portions. Empty if unsafe."""
        self.ensure_one()
        remaining = record.amount
        rounding = record.currency_id.rounding
        allocation = []
        line_payload = record.line_payload or []

        # Prefer product-matched allocation when Woo refund lines are present
        if line_payload:
            # Group refund qty/amount by product via order lines → invoices
            for invoice in invoices:
                if float_is_zero(remaining, precision_rounding=rounding):
                    break
                portion = 0.0
                for rline in line_payload:
                    qty = abs(float(rline.get('quantity') or 0.0))
                    total = abs(float(rline.get('total') or 0.0))
                    if float_is_zero(total, precision_rounding=rounding) and float_is_zero(
                        qty, precision_rounding=rounding,
                    ):
                        continue
                    product = self._woo_match_order_product(rline)
                    if not product:
                        continue
                    inv_lines = invoice.invoice_line_ids.filtered(
                        lambda l, product=product: l.product_id == product and l.quantity
                    )
                    if not inv_lines:
                        continue
                    portion += total if total else 0.0
                portion = min(portion, remaining, invoice.amount_residual or invoice.amount_total)
                if portion > 0:
                    allocation.append((invoice, float_round(portion, precision_rounding=rounding)))
                    remaining = float_round(remaining - portion, precision_rounding=rounding)

        # Fallback: allocate by invoice residual order
        if float_compare(remaining, 0.0, precision_rounding=rounding) > 0:
            for invoice in invoices:
                if float_is_zero(remaining, precision_rounding=rounding):
                    break
                available = invoice.amount_residual
                if float_is_zero(available, precision_rounding=rounding):
                    available = invoice.amount_total
                take = min(remaining, available)
                if float_compare(take, 0.0, precision_rounding=rounding) > 0:
                    # Merge with existing portion for same invoice
                    merged = False
                    new_alloc = []
                    for inv, amt in allocation:
                        if inv.id == invoice.id:
                            new_alloc.append((inv, float_round(amt + take, precision_rounding=rounding)))
                            merged = True
                        else:
                            new_alloc.append((inv, amt))
                    if not merged:
                        new_alloc.append((invoice, float_round(take, precision_rounding=rounding)))
                    allocation = new_alloc
                    remaining = float_round(remaining - take, precision_rounding=rounding)

        if float_compare(remaining, 0.0, precision_rounding=rounding) > 0:
            # Could not fully allocate — leave for review rather than over-refund
            return []
        return allocation

    def _woo_create_partial_credit_note(self, invoice, amount, record):
        """Create a draft credit note for ``amount`` against ``invoice`` without stock returns."""
        self.ensure_one()
        invoice.ensure_one()
        if float_is_zero(amount, precision_rounding=record.currency_id.rounding):
            return self.env['account.move']

        # Full residual → standard reversal wizard (draft)
        if float_compare(
            amount, invoice.amount_residual or invoice.amount_total,
            precision_rounding=record.currency_id.rounding,
        ) >= 0:
            reversal = self.env['account.move.reversal'].with_context(
                active_model='account.move',
                active_ids=invoice.ids,
            ).create({
                'company_id': invoice.company_id.id,
                'move_ids': [Command.set(invoice.ids)],
                'reason': self.env._(
                    'WooCommerce refund #%(id)s', id=record.woo_refund_id,
                ),
                'date': fields.Date.context_today(self),
                'journal_id': invoice.journal_id.id,
            })
            reversal.refund_moves()
            credit = reversal.new_move_ids[:1]
            return credit

        # Partial: reverse then scale product lines so tax-inclusive total ≈ amount
        reversal = self.env['account.move.reversal'].with_context(
            active_model='account.move',
            active_ids=invoice.ids,
        ).create({
            'company_id': invoice.company_id.id,
            'move_ids': [Command.set(invoice.ids)],
            'reason': self.env._(
                'WooCommerce refund #%(id)s (partial %(amount)s)',
                id=record.woo_refund_id,
                amount=amount,
            ),
            'date': fields.Date.context_today(self),
            'journal_id': invoice.journal_id.id,
        })
        reversal.refund_moves()
        credit = reversal.new_move_ids[:1]
        if not credit:
            return self.env['account.move']
        if credit.state != 'draft':
            # Do not mutate posted moves; leave for review
            record.error_message = self.env._(
                'Partial refund produced a non-draft credit note; left for manual adjustment.'
            )
            return credit

        product_lines = credit.invoice_line_ids.filtered(
            lambda l: l.display_type == 'product' and not float_is_zero(
                l.price_total, precision_rounding=record.currency_id.rounding,
            )
        )
        current_total = abs(credit.amount_total)
        if float_is_zero(current_total, precision_rounding=record.currency_id.rounding):
            return credit
        ratio = amount / current_total
        if float_compare(ratio, 1.0, precision_rounding=record.currency_id.rounding) >= 0:
            return credit
        for line in product_lines:
            new_qty = line.quantity * ratio
            if line.product_uom_id and hasattr(line.product_uom_id, 'round'):
                new_qty = line.product_uom_id.round(new_qty)
            else:
                new_qty = float_round(new_qty, precision_digits=2)
            line.write({'quantity': new_qty})
        # If still over due to rounding, trim the largest line unit price slightly
        if float_compare(
            abs(credit.amount_total), amount,
            precision_rounding=record.currency_id.rounding,
        ) > 0 and product_lines:
            delta = abs(credit.amount_total) - amount
            target = product_lines.sorted(lambda l: abs(l.price_total), reverse=True)[0]
            if target.quantity:
                adj = delta / target.quantity
                target.price_unit = float_round(
                    target.price_unit - adj,
                    precision_rounding=record.currency_id.rounding,
                )
        return credit
