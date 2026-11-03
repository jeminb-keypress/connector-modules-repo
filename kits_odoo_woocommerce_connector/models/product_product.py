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
            variants = self.env['kits.woo.variant.binding'].search([
                ('product_id', 'in', self.ids),
                ('active', '=', True),
                ('sync_state', '!=', 'deleted'),
            ])
            if variants:
                variants._kits_mark_not_synced()
            templates = self.env['kits.woo.product.binding'].search([
                ('product_tmpl_id', 'in', self.mapped('product_tmpl_id').ids),
                ('active', '=', True),
                ('sync_state', '!=', 'deleted'),
            ])
            if templates:
                templates._kits_mark_not_synced()
        return res

    def unlink(self):
        bindings = self.env['kits.woo.variant.binding'].sudo().with_context(
            active_test=False,
        ).search([('product_id', 'in', self.ids)])
        if bindings:
            bindings.unlink()
        return super().unlink()
