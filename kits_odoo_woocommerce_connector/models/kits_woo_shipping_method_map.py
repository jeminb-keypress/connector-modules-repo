# -*- coding: utf-8 -*-
from odoo import api, fields, models


class KitsWooShippingMethodMap(models.Model):
    _name = 'kits.woo.shipping.method.map'
    _description = 'WooCommerce Shipping Method Mapping'
    _order = 'woo_method_id, id'
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
    woo_method_id = fields.Char(
        string='Woo Method ID',
        required=True,
        help='WooCommerce shipping method_id from shipping_lines (e.g. flat_rate, free_shipping).',
    )
    woo_method_title = fields.Char(string='Woo Title', readonly=True)
    carrier_id = fields.Many2one(
        'delivery.carrier',
        string='Odoo Delivery Method',
        check_company=True,
        help='Optional. When set, imported orders using this Woo method get this carrier.',
    )
    delivery_product_id = fields.Many2one(
        'product.product',
        string='Delivery Product Override',
        check_company=True,
        domain="[('type', '=', 'service')]",
        help='Optional product used for the shipping SO line instead of the default WOO_SHIPPING product.',
    )
    active = fields.Boolean(default=True)
    notes = fields.Char()

    _method_instance_uniq = models.Constraint(
        'UNIQUE(instance_id, woo_method_id)',
        'Each WooCommerce shipping method can be mapped once per store.',
    )

    @api.depends('woo_method_id', 'woo_method_title')
    def _compute_display_name(self):
        for row in self:
            label = row.woo_method_title or row.woo_method_id or self.env._('Shipping')
            row.display_name = (
                f'{label} ({row.woo_method_id})' if row.woo_method_id else label
            )
