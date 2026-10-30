# -*- coding: utf-8 -*-
from odoo import fields, models

_WOO_MAPPED_VARIANT_FIELDS = frozenset({
    'default_code',
    'barcode',
    'lst_price',
    'list_price',
    'image_variant_1920',
    'active',
})


class ProductProduct(models.Model):
    _inherit = 'product.product'

    woo_sync_to_store = fields.Boolean(
        related='product_tmpl_id.woo_sync_to_store',
        readonly=True,
        store=False,
    )
    woo_instance_ids = fields.Many2many(
        related='product_tmpl_id.woo_instance_ids',
        readonly=False,
    )

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get('kits_woo_skip_dirty'):
            return res
        dirty = _WOO_MAPPED_VARIANT_FIELDS.intersection(vals)
        if dirty:
            Binding = self.env['kits.connector.binding']
            Binding._kits_mark_records_not_synced('product.product', self.ids)
            Binding._kits_mark_records_not_synced(
                'product.template',
                self.mapped('product_tmpl_id').ids,
            )
        return res

    def unlink(self):
        self.env['kits.connector.binding']._kits_mark_odoo_records_deleted_local(
            'product.product',
            self.ids,
            self.env._(
                'Odoo variant was deleted. The WooCommerce variation was left unchanged.'
            ),
        )
        return super().unlink()
