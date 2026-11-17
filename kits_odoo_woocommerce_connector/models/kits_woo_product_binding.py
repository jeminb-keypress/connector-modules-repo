# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

_PRESENTATION_FIELDS = frozenset({
    'title', 'description', 'description_short',
})


class KitsWooProductBinding(models.Model):
    """Store-specific WooCommerce product, linked to one Odoo product template."""

    _name = 'kits.woo.product.binding'
    _description = 'WooCommerce Product Binding'
    _inherit = 'kits.woo.binding.sync'
    _order = 'id desc'
    _check_company_auto = True

    instance_id = fields.Many2one(
        'kits.woo.instance',
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
    product_tmpl_name = fields.Char(
        related='product_tmpl_id.name',
        string='Odoo Product Name',
        readonly=True,
    )
    external_id = fields.Char(
        string='External Product ID',
        index=True,
        copy=False,
        help='Empty until the product is successfully exported or imported from WooCommerce.',
    )
    title = fields.Char(
        string='Store Title',
        translate=True,
        help='Store-specific product name sent to WooCommerce. Independent per store.',
    )
    description_short = fields.Html(
        string='Store Short Description',
        sanitize=True,
        translate=True,
    )
    description = fields.Html(
        string='Store Description',
        sanitize=True,
        translate=True,
    )
    published = fields.Boolean(string='Published')
    woo_category_ids = fields.Many2many(
        'kits.woo.category',
        'kits_woo_product_binding_woo_category_rel',
        'binding_id',
        'woo_category_id',
        string='Store Categories',
        domain="[('instance_id', '=', instance_id), ('active', '=', True), "
               "('sync_state', 'not in', ('deleted',))]",
        help='Same-store connector categories. Pending records are exported '
             'automatically before product export (parents first).',
    )
    woo_tag_ids = fields.Many2many(
        'kits.woo.tag',
        'kits_woo_product_binding_woo_tag_rel',
        'binding_id',
        'woo_tag_id',
        string='Store Tags',
        domain="[('instance_id', '=', instance_id), ('active', '=', True), "
               "('sync_state', 'not in', ('deleted',))]",
        help='Same-store connector tags. Pending records are exported '
             'automatically before product export.',
    )
    image_urls = fields.Json(string='Store Images')
    linked_variant_count = fields.Integer(
        string='Linked Variant Count',
        compute='_compute_linked_variant_count',
    )
    variant_binding_ids = fields.One2many(
        'kits.woo.variant.binding',
        'product_binding_id',
        string='Variant Bindings',
    )
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
            label = binding.title or binding.product_tmpl_id.display_name or binding.external_id or '?'
            remote = binding.external_id or self.env._('pending')
            binding.display_name = f'{label} ↔ {remote}'

    @api.model_create_multi
    def create(self, vals_list):
        records = []
        for vals in vals_list:
            if vals.get('instance_id') and not vals.get('company_id'):
                instance = self.env['kits.woo.instance'].browse(vals['instance_id'])
                vals['company_id'] = instance.company_id.id
            if vals.get('external_id'):
                vals['external_id'] = str(vals['external_id'])
            elif 'external_id' in vals and not vals['external_id']:
                vals['external_id'] = False
            records.append(vals)
        bindings = super().create(records)
        bindings._kits_validate_woo_catalog_assignments()
        return bindings

    def write(self, vals):
        if vals.get('external_id'):
            vals = dict(vals, external_id=str(vals['external_id']))
        dirty_presentation = (
            _PRESENTATION_FIELDS.intersection(vals)
            and not self.env.context.get('kits_woo_skip_presentation_dirty')
            and not self.env.context.get('kits_woo_skip_dirty')
        )
        result = super().write(vals)
        if dirty_presentation:
            self.filtered(
                lambda b: b.active and b.sync_state not in ('deleted', 'deleted_local')
            )._kits_mark_not_synced()
        if any(key in vals for key in (
            'woo_category_ids', 'woo_tag_ids', 'instance_id',
        )):
            self._kits_validate_woo_catalog_assignments()
            if vals.get('instance_id'):
                self._kits_clear_cross_store_catalog_assignments()
        return result

    @api.onchange('instance_id')
    def _onchange_instance_clear_catalog(self):
        for binding in self:
            invalid_cats = binding.woo_category_ids.filtered(
                lambda c: c.instance_id != binding.instance_id
            )
            invalid_tags = binding.woo_tag_ids.filtered(
                lambda t: t.instance_id != binding.instance_id
            )
            if invalid_cats:
                binding.woo_category_ids = binding.woo_category_ids - invalid_cats
            if invalid_tags:
                binding.woo_tag_ids = binding.woo_tag_ids - invalid_tags

    def _kits_clear_cross_store_catalog_assignments(self):
        for binding in self:
            valid_cats = binding.woo_category_ids.filtered(
                lambda c: c.instance_id == binding.instance_id
            )
            valid_tags = binding.woo_tag_ids.filtered(
                lambda t: t.instance_id == binding.instance_id
            )
            updates = {}
            if valid_cats != binding.woo_category_ids:
                updates['woo_category_ids'] = [(6, 0, valid_cats.ids)]
            if valid_tags != binding.woo_tag_ids:
                updates['woo_tag_ids'] = [(6, 0, valid_tags.ids)]
            if updates:
                super(KitsWooProductBinding, binding).write(updates)

    def _kits_validate_woo_catalog_assignments(self):
        """Allow pending same-store cats/tags; reject cross-store and deleted."""
        for binding in self:
            for category in binding.woo_category_ids:
                if category.instance_id != binding.instance_id:
                    raise ValidationError(self.env._(
                        'Category "%(name)s" belongs to another WooCommerce store.',
                        name=category.display_name,
                    ))
                if category.sync_state == 'deleted' or not category.active:
                    raise ValidationError(self.env._(
                        'Category "%(name)s" is deleted or archived and cannot be '
                        'assigned to a product.',
                        name=category.display_name,
                    ))
            for tag in binding.woo_tag_ids:
                if tag.instance_id != binding.instance_id:
                    raise ValidationError(self.env._(
                        'Tag "%(name)s" belongs to another WooCommerce store.',
                        name=tag.display_name,
                    ))
                if tag.sync_state == 'deleted' or not tag.active:
                    raise ValidationError(self.env._(
                        'Tag "%(name)s" is deleted or archived and cannot be '
                        'assigned to a product.',
                        name=tag.display_name,
                    ))

    @api.constrains('external_id', 'exists_in_store', 'sync_state')
    def _check_external_id(self):
        for binding in self:
            has_remote = bool((binding.external_id or '').strip())
            if binding.exists_in_store and binding.sync_state != 'deleted' and not has_remote:
                raise ValidationError(self.env._(
                    'External Product ID is required for products that exist in the store.'
                ))

    @api.constrains('woo_category_ids', 'woo_tag_ids', 'instance_id')
    def _check_woo_catalog_assignments(self):
        self._kits_validate_woo_catalog_assignments()

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

    def _kits_has_remote_id(self):
        self.ensure_one()
        return bool((self.external_id or '').strip())

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
        'kits.woo.instance',
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
    title = fields.Char(
        string='Store Variant Title',
        translate=True,
        help='Store-specific variation label kept on the connector note. '
             'WooCommerce REST variations do not accept an independent name field; '
             'export continues to use the parent product name plus attributes.',
    )
    attribute_combination = fields.Char(
        string='Attribute Combination',
        help='WooCommerce option labels for this variation (e.g. Color: Red, Size: M).',
    )
    regular_price = fields.Char(string='Regular Price')
    sale_price = fields.Char(string='Sale Price')
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

    def write(self, vals):
        if vals.get('external_id'):
            vals = dict(vals, external_id=str(vals['external_id']))
        dirty_title = (
            'title' in vals
            and not self.env.context.get('kits_woo_skip_presentation_dirty')
            and not self.env.context.get('kits_woo_skip_dirty')
        )
        result = super().write(vals)
        if dirty_title:
            self.filtered(
                lambda b: b.active and b.sync_state not in ('deleted', 'deleted_local')
            )._kits_mark_not_synced()
        return result

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
