# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

_PRODUCT_TYPES = [
    ('simple', 'Simple'),
    ('variable', 'Variable'),
    ('grouped', 'Grouped'),
    ('external', 'External'),
    ('bundle', 'Bundle'),
]


class KitsWooProductBinding(models.Model):
    """Store-specific WooCommerce product, linked to one Odoo product template."""

    _name = 'kits.woo.product.binding'
    _description = 'WooCommerce Product Binding'
    _inherit = 'kits.woo.binding.sync'
    _order = 'id desc'
    _check_company_auto = True

    instance_id = fields.Many2one(
        'kits.connector.instance',
        string='WooCommerce Store',
        required=True,
        index=True,
        ondelete='cascade',
        domain="[('platform_type', '=', 'woocommerce')]",
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        index=True,
    )
    product_tmpl_id = fields.Many2one(
        'product.template',
        string='Odoo Product',
        required=True,
        index=True,
        ondelete='cascade',
        check_company=True,
    )
    external_id = fields.Char(
        string='External Product ID',
        required=True,
        index=True,
    )
    title = fields.Char(string='Store Title')
    description_short = fields.Html(string='Store Short Description', sanitize=True)
    description = fields.Html(string='Store Description', sanitize=True)
    product_type = fields.Selection(selection=_PRODUCT_TYPES, string='Product Type', required=True, default='simple')
    published = fields.Boolean(string='Published')
    taxable = fields.Boolean(string='Taxable')
    virtual = fields.Boolean(string='Virtual')
    category_ids = fields.Many2many(
        'product.category',
        'kits_woo_product_binding_category_rel',
        'binding_id',
        'category_id',
        string='Store Categories',
    )
    tag_ids = fields.Many2many(
        'product.tag',
        'kits_woo_product_binding_tag_rel',
        'binding_id',
        'tag_id',
        string='Store Tags',
    )
    image_urls = fields.Json(string='Store Images')
    variant_count = fields.Integer(string='Total Variant Count')
    linked_variant_count = fields.Integer(
        string='Linked Variant Count',
        compute='_compute_linked_variant_count',
    )
    variant_binding_ids = fields.One2many(
        'kits.woo.variant.binding',
        'product_binding_id',
        string='Variant Bindings',
    )
    store_create_date = fields.Datetime(string='Store Creation Date')
    store_write_date = fields.Datetime(string='Store Modification Date')
    odoo_record_id = fields.Integer(
        string='Odoo Record',
        related='product_tmpl_id.id',
        store=True,
        index=True,
    )

    _external_uniq = models.Constraint(
        'UNIQUE(instance_id, external_id)',
        'This WooCommerce product is already linked for this store.',
    )
    _template_uniq = models.Constraint(
        'UNIQUE(instance_id, product_tmpl_id)',
        'This Odoo product is already linked for this WooCommerce store.',
    )

    @api.depends('variant_binding_ids', 'variant_binding_ids.product_id', 'variant_binding_ids.active')
    def _compute_linked_variant_count(self):
        for binding in self:
            binding.linked_variant_count = len(binding.variant_binding_ids.filtered(lambda row: row.product_id))

    @api.depends('title', 'external_id', 'product_tmpl_id')
    def _compute_display_name(self):
        for binding in self:
            label = binding.title or binding.product_tmpl_id.display_name or binding.external_id
            binding.display_name = f'{label} ↔ {binding.external_id}'

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('instance_id') and not vals.get('company_id'):
                instance = self.env['kits.connector.instance'].browse(vals['instance_id'])
                vals['company_id'] = instance.company_id.id
            if vals.get('external_id'):
                vals['external_id'] = str(vals['external_id'])
        return super().create(vals_list)

    @api.constrains('external_id')
    def _check_external_id(self):
        for binding in self:
            if not (binding.external_id or '').strip():
                raise ValidationError(self.env._('External Product ID is required.'))

    @api.model
    def _find(self, instance, *, external_id=False, odoo_record_id=False):
        if not instance:
            raise UserError(self.env._('A WooCommerce store is required.'))
        domain = [('instance_id', '=', instance.id)]
        if external_id:
            domain.append(('external_id', '=', str(external_id)))
        elif odoo_record_id:
            domain.append(('product_tmpl_id', '=', int(odoo_record_id)))
        else:
            raise UserError(self.env._('Provide an external product id or an Odoo product.'))
        return self.with_context(active_test=False).search(domain, limit=1)

    def _kits_get_odoo_record(self):
        self.ensure_one()
        return self.product_tmpl_id.exists()

    def action_open_variants(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Variant Bindings'),
            'res_model': 'kits.woo.variant.binding',
            'view_mode': 'list,form',
            'domain': [('product_binding_id', '=', self.id)],
            'context': {'active_test': False, 'default_product_binding_id': self.id},
        }


class KitsWooVariantBinding(models.Model):
    """One WooCommerce option combination, linked to one Odoo variant and its parent product binding."""

    _name = 'kits.woo.variant.binding'
    _description = 'WooCommerce Variant Binding'
    _inherit = 'kits.woo.binding.sync'
    _order = 'id desc'
    _check_company_auto = True

    product_binding_id = fields.Many2one(
        'kits.woo.product.binding',
        string='Product Binding',
        required=True,
        index=True,
        ondelete='cascade',
    )
    instance_id = fields.Many2one(
        'kits.connector.instance',
        string='WooCommerce Store',
        required=True,
        index=True,
        ondelete='cascade',
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        index=True,
    )
    product_id = fields.Many2one(
        'product.product',
        string='Odoo Variant',
        required=True,
        index=True,
        ondelete='cascade',
        check_company=True,
    )
    external_id = fields.Char(
        string='External Variant ID',
        required=True,
        index=True,
    )
    sku = fields.Char(string='SKU', index=True)
    title = fields.Char(string='Store Variant Title')
    product_url = fields.Char(string='Product Page URL')
    manage_stock = fields.Boolean(string='Manage Stock')
    qty_sync_rule = fields.Selection(
        selection=[
            ('odoo', 'Odoo quantity'),
            ('fixed', 'Fixed quantity'),
            ('percentage', 'Percentage'),
        ],
        string='Quantity Synchronization',
        default='odoo',
        required=True,
    )
    qty_sync_value = fields.Float(string='Quantity Rule Value')
    image_urls = fields.Json(string='Variant Images')
    store_create_date = fields.Datetime(string='Store Creation Date')
    store_write_date = fields.Datetime(string='Store Modification Date')
    odoo_record_id = fields.Integer(
        string='Odoo Record',
        related='product_id.id',
        store=True,
        index=True,
    )

    _external_uniq = models.Constraint(
        'UNIQUE(instance_id, external_id)',
        'This WooCommerce variant is already linked for this store.',
    )
    _variant_uniq = models.Constraint(
        'UNIQUE(instance_id, product_id)',
        'This Odoo variant is already linked for this WooCommerce store.',
    )

    @api.depends('sku', 'external_id', 'product_id')
    def _compute_display_name(self):
        for binding in self:
            label = binding.sku or binding.product_id.display_name or binding.external_id
            binding.display_name = f'{label} ↔ {binding.external_id}'

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            parent = self.env['kits.woo.product.binding'].browse(vals.get('product_binding_id'))
            if parent:
                vals.setdefault('instance_id', parent.instance_id.id)
                vals.setdefault('company_id', parent.company_id.id)
            if vals.get('external_id'):
                vals['external_id'] = str(vals['external_id'])
        return super().create(vals_list)

    @api.constrains('instance_id', 'product_binding_id', 'external_id', 'product_id')
    def _check_parent_store(self):
        for binding in self:
            parent = binding.product_binding_id
            if parent and binding.instance_id != parent.instance_id:
                raise ValidationError(self.env._(
                    'A variant binding must belong to a product binding of the same store.'
                ))
            if parent and binding.product_id and binding.product_id.product_tmpl_id != parent.product_tmpl_id:
                raise ValidationError(self.env._(
                    'The Odoo variant must belong to the product template of its parent binding.'
                ))
            if not (binding.external_id or '').strip():
                raise ValidationError(self.env._('External Variant ID is required.'))

    @api.model
    def _find(self, instance, *, external_id=False, odoo_record_id=False, sku=False):
        if not instance:
            raise UserError(self.env._('A WooCommerce store is required.'))
        domain = [('instance_id', '=', instance.id)]
        if external_id:
            domain.append(('external_id', '=', str(external_id)))
        elif sku:
            domain.append(('sku', '=', sku))
        elif odoo_record_id:
            domain.append(('product_id', '=', int(odoo_record_id)))
        else:
            raise UserError(self.env._('Provide an external variant id, SKU, or Odoo variant.'))
        return self.with_context(active_test=False).search(domain, limit=1)

    def _kits_get_odoo_record(self):
        self.ensure_one()
        return self.product_id.exists()

    def _woo_export_quantity(self, free_qty):
        """Quantity sent to WooCommerce. Odoo stock is never replaced by a Woo quantity."""
        self.ensure_one()
        qty = float(free_qty or 0.0)
        if self.qty_sync_rule == 'fixed':
            qty = float(self.qty_sync_value or 0.0)
        elif self.qty_sync_rule == 'percentage':
            qty = qty * float(self.qty_sync_value or 0.0) / 100.0
        if abs(qty - int(qty)) < 1e-9:
            return int(qty)
        return qty
