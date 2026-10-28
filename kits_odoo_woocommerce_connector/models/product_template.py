# -*- coding: utf-8 -*-
from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    woo_sync_to_store = fields.Boolean(
        string='Sync to WooCommerce',
        help='When set, this product is included in WooCommerce product export jobs.',
        default=False,
        copy=False,
    )
