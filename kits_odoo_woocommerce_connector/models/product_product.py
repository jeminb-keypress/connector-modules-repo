# -*- coding: utf-8 -*-
from odoo import fields, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    woo_sync_to_store = fields.Boolean(
        related='product_tmpl_id.woo_sync_to_store',
        readonly=False,
        store=False,
    )
