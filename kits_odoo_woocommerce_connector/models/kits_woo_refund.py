# -*- coding: utf-8 -*-
"""Track WooCommerce refunds and linked Odoo credit notes (idempotent)."""

from odoo import api, fields, models


class KitsWooRefund(models.Model):
    _name = 'kits.woo.refund'
    _description = 'WooCommerce Refund Sync'
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char(compute='_compute_name', store=True)
    instance_id = fields.Many2one(
        'kits.connector.instance',
        required=True,
        ondelete='cascade',
        index=True,
        domain="[('platform_type', '=', 'woocommerce')]",
        check_company=True,
    )
    company_id = fields.Many2one(
        related='instance_id.company_id',
        store=True,
        readonly=True,
    )
    sale_order_id = fields.Many2one(
        'sale.order',
        string='Sales Order',
        ondelete='cascade',
        index=True,
        check_company=True,
    )
    woo_order_id = fields.Char(required=True, index=True)
    woo_refund_id = fields.Char(required=True, index=True)
    amount = fields.Monetary(currency_field='currency_id', required=True)
    currency_id = fields.Many2one(
        'res.currency',
        required=True,
        default=lambda self: self.env.company.currency_id,
    )
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('review', 'Needs Review'),
            ('done', 'Done'),
            ('skipped', 'Skipped'),
            ('error', 'Error'),
        ],
        default='pending',
        required=True,
        index=True,
    )
    invoice_id = fields.Many2one(
        'account.move',
        string='Source Invoice',
        domain="[('move_type', '=', 'out_invoice')]",
        check_company=True,
    )
    credit_note_id = fields.Many2one(
        'account.move',
        string='Credit Note',
        domain="[('move_type', '=', 'out_refund')]",
        check_company=True,
    )
    error_message = fields.Text()
    woo_reason = fields.Char()
    line_payload = fields.Json(
        help='Raw WooCommerce refund line_items used for allocation (no secrets).',
    )
    retry_count = fields.Integer(default=0)

    _woo_refund_instance_uniq = models.Constraint(
        'UNIQUE(instance_id, woo_refund_id)',
        'Each WooCommerce refund id can be processed once per store.',
    )

    @api.depends('woo_refund_id', 'woo_order_id', 'amount')
    def _compute_name(self):
        for row in self:
            row.name = self.env._(
                'Woo refund #%(refund)s (order %(order)s) %(amount)s',
                refund=row.woo_refund_id or '?',
                order=row.woo_order_id or '?',
                amount=row.amount,
            )

    def action_retry_refund_sync(self):
        for row in self:
            if not row.sale_order_id or not row.instance_id:
                continue
            row.state = 'pending'
            row.error_message = False
            row.retry_count += 1
            row.instance_id._woo_process_refund_record(row)
        return True
