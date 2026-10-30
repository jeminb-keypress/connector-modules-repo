# -*- coding: utf-8 -*-
from odoo import api, fields, models


class KitsWooPaymentGatewayMap(models.Model):
    _name = 'kits.woo.payment.gateway.map'
    _description = 'WooCommerce Payment Gateway Mapping'
    _order = 'woo_gateway_id, id'
    _check_company_auto = True

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
    woo_gateway_id = fields.Char(
        string='Woo Gateway ID',
        required=True,
        help='WooCommerce payment gateway id, e.g. bacs, cod, stripe.',
    )
    woo_title = fields.Char(string='Woo Title', readonly=True)
    woo_enabled = fields.Boolean(string='Enabled on Woo', readonly=True)
    payment_method_id = fields.Many2one(
        'payment.method',
        string='Odoo Payment Method',
        help='Optional mapping for reporting. Orders never invent account payments.',
    )
    active = fields.Boolean(default=True)
    notes = fields.Char()

    _gateway_instance_uniq = models.Constraint(
        'UNIQUE(instance_id, woo_gateway_id)',
        'Each WooCommerce payment gateway can be mapped once per store.',
    )

    @api.depends('woo_gateway_id', 'woo_title')
    def _compute_display_name(self):
        for row in self:
            label = row.woo_title or row.woo_gateway_id or self.env._('Gateway')
            row.display_name = f'{label} ({row.woo_gateway_id})' if row.woo_gateway_id else label
