# -*- coding: utf-8 -*-
from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    woo_instance_id = fields.Many2one(
        'kits.connector.instance',
        string='WooCommerce Store',
        copy=False,
        index=True,
        ondelete='set null',
        check_company=True,
        domain="[('platform_type', '=', 'woocommerce')]",
    )
    woo_order_id = fields.Char(
        string='WooCommerce Order ID',
        copy=False,
        index=True,
        readonly=True,
    )
    woo_order_status = fields.Char(
        string='WooCommerce Status',
        copy=False,
        readonly=True,
    )
    woo_sync_conflict = fields.Text(
        string='WooCommerce Sync Conflict',
        copy=False,
        readonly=True,
        help='Set when a remote WooCommerce order change cannot safely overwrite this Odoo order.',
    )
