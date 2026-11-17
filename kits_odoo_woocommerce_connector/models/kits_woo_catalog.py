# -*- coding: utf-8 -*-
"""Store-specific WooCommerce catalog records (categories, tags, attributes, terms).

Categories and tags are the connector-side source of truth for WooCommerce catalog
sync. Native Odoo product.category / product.tag are not created or updated by
WooCommerce synchronization. Optional odoo_* links may remain for legacy data only.
"""

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError


def _kits_norm_text(value):
    return (value or '').strip()


def _kits_norm_key(value):
    return _kits_norm_text(value).casefold()


def _kits_norm_external_id(value):
    """Normalize Woo remote ids; treat False/None/'False'/'null' as empty."""
    if value is None or value is False:
        return False
    text = str(value).strip()
    if not text or text.casefold() in ('false', 'none', 'null'):
        return False
    return text


class KitsWooCatalogExportMixin(models.AbstractModel):
    """Export status helpers shared by kits.woo.category and kits.woo.tag."""

    _name = 'kits.woo.catalog.export.mixin'
    _description = 'WooCommerce Catalog Export Status Mixin'

    export_status = fields.Selection(
        selection=[
            ('pending', 'Not yet exported'),
            ('exported', 'Exported'),
            ('to_update', 'Update Required'),
            ('error', 'Error'),
        ],
        string='Export Status',
        compute='_compute_export_status',
        store=True,
        index=True,
    )

    @api.depends('external_id', 'sync_state')
    def _compute_export_status(self):
        for record in self:
            if record.sync_state == 'error':
                record.export_status = 'error'
            elif not _kits_norm_external_id(record.external_id):
                record.export_status = 'pending'
            elif record.sync_state == 'synced':
                record.export_status = 'exported'
            elif record.sync_state == 'deleted':
                record.export_status = 'error'
            else:
                record.export_status = 'to_update'

    def _kits_catalog_tracked_fields(self):
        return set()

    def _kits_mark_update_required(self):
        for record in self:
            if record.sync_state == 'deleted' or not record.active:
                continue
            if not (record.external_id or '').strip():
                record.write({
                    'sync_state': 'not_synced',
                    'sync_error': False,
                })
            else:
                record.write({
                    'sync_state': 'not_synced',
                })

    def write(self, vals):
        tracked = self._kits_catalog_tracked_fields()
        dirty = bool(tracked.intersection(vals)) and not self.env.context.get(
            'kits_woo_skip_catalog_dirty'
        )
        result = super().write(vals)
        if dirty:
            to_mark = self.filtered(
                lambda r: r.active and r.sync_state != 'deleted'
                and (r.external_id or '').strip()
            )
            if to_mark:
                super(KitsWooCatalogExportMixin, to_mark).write({
                    'sync_state': 'not_synced',
                })
        return result

    @api.model
    def _kits_assert_unique_name_slug(self, *, instance, name, slug, exclude_id=False):
        """Reject duplicate name/slug within one store (case-insensitive)."""
        if not instance:
            raise ValidationError(self.env._('A WooCommerce store is required.'))
        name_key = _kits_norm_key(name)
        slug_key = _kits_norm_key(slug)
        if not name_key:
            raise ValidationError(self.env._('Name is required.'))
        domain = [('instance_id', '=', instance.id)]
        if exclude_id:
            domain.append(('id', '!=', exclude_id))
        siblings = self.with_context(active_test=False).search(domain)
        for row in siblings:
            if _kits_norm_key(row.name) == name_key:
                raise ValidationError(self.env._(
                    'A %(model)s named "%(name)s" already exists on store "%(store)s".',
                    model=self._description,
                    name=_kits_norm_text(name),
                    store=instance.display_name,
                ))
            if slug_key and _kits_norm_key(row.slug) == slug_key:
                raise ValidationError(self.env._(
                    'A %(model)s with slug "%(slug)s" already exists on store "%(store)s".',
                    model=self._description,
                    slug=_kits_norm_text(slug),
                    store=instance.display_name,
                ))

    def action_export_selected(self):
        """Export selected connector catalog records via each store's API."""
        if not self:
            raise UserError(self.env._('Select at least one record to export.'))
        summaries = []
        for instance in self.mapped('instance_id'):
            instance._ensure_woo_ready()
            rows = self.filtered(lambda r, i=instance: r.instance_id == i)
            if self._name == 'kits.woo.category':
                summary = instance._woo_export_selected_categories(rows)
            else:
                summary = instance._woo_export_selected_tags(rows)
            summaries.append(f'{instance.display_name}: {summary}')
            instance._kits_log_operation(
                operation='woo_export_categories' if self._name == 'kits.woo.category'
                else 'woo_export_tags',
                status='success',
                level='info',
                message=summary,
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._('Export Selected'),
                'message': '\n'.join(summaries),
                'type': 'success',
                'sticky': False,
            },
        }


class KitsWooCategory(models.Model):
    _name = 'kits.woo.category'
    _description = 'WooCommerce Product Category'
    _inherit = ['kits.woo.binding.sync', 'kits.woo.catalog.export.mixin']
    _order = 'complete_name, id'
    _check_company_auto = True
    _parent_store = True
    _parent_name = 'parent_id'
    _rec_name = 'complete_name'

    instance_id = fields.Many2one(
        'kits.woo.instance',
        string='WooCommerce Store',
        required=True,
        index=True,
        ondelete='cascade',
        domain="[('platform_type', '=', 'woocommerce')]",
    )
    company_id = fields.Many2one('res.company', required=True, index=True)
    external_id = fields.Char(
        string='WooCommerce Category ID',
        index=True,
        copy=False,
        help='Empty until the category is successfully exported or imported from WooCommerce.',
    )
    name = fields.Char(required=True)
    complete_name = fields.Char(compute='_compute_complete_name', recursive=True, store=True)
    slug = fields.Char()
    description = fields.Html(sanitize=True)
    display_type = fields.Selection(
        selection=[
            ('default', 'Default'),
            ('products', 'Products'),
            ('subcategories', 'Subcategories'),
            ('both', 'Both'),
        ],
        default='default',
    )
    image_url = fields.Char(string='Image URL')
    parent_id = fields.Many2one(
        'kits.woo.category',
        string='Parent Category',
        index=True,
        ondelete='restrict',
        domain="[('instance_id', '=', instance_id)]",
    )
    parent_path = fields.Char(index=True)
    child_ids = fields.One2many('kits.woo.category', 'parent_id', string='Child Categories')
    odoo_category_id = fields.Many2one(
        'product.category',
        string='Legacy Odoo Category',
        ondelete='set null',
        help='Optional historical link. WooCommerce sync does not create or update native categories.',
    )

    _external_uniq = models.Constraint(
        'UNIQUE(instance_id, external_id)',
        'This WooCommerce category is already registered for this store.',
    )

    def _kits_catalog_tracked_fields(self):
        return {
            'name', 'slug', 'description', 'display_type', 'image_url', 'parent_id',
        }

    @api.depends('name', 'parent_id.complete_name')
    def _compute_complete_name(self):
        for category in self:
            if category.parent_id:
                category.complete_name = f'{category.parent_id.complete_name} / {category.name}'
            else:
                category.complete_name = category.name

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('instance_id') and not vals.get('company_id'):
                instance = self.env['kits.woo.instance'].browse(vals['instance_id'])
                vals['company_id'] = instance.company_id.id
            if vals.get('name'):
                vals['name'] = _kits_norm_text(vals['name'])
            if vals.get('slug'):
                vals['slug'] = _kits_norm_text(vals['slug'])
            if 'external_id' in vals:
                vals['external_id'] = _kits_norm_external_id(vals.get('external_id'))
            instance = self.env['kits.woo.instance'].browse(vals.get('instance_id'))
            self._kits_assert_unique_name_slug(
                instance=instance,
                name=vals.get('name'),
                slug=vals.get('slug'),
            )
            vals.setdefault('sync_state', 'synced' if vals.get('external_id') else 'not_synced')
        return super().create(vals_list)

    def write(self, vals):
        vals = dict(vals)
        if vals.get('name'):
            vals['name'] = _kits_norm_text(vals['name'])
        if 'slug' in vals and vals.get('slug'):
            vals['slug'] = _kits_norm_text(vals['slug'])
        if 'external_id' in vals:
            vals['external_id'] = _kits_norm_external_id(vals.get('external_id'))
        for category in self:
            name = vals['name'] if 'name' in vals else category.name
            slug = vals['slug'] if 'slug' in vals else category.slug
            instance = (
                self.env['kits.woo.instance'].browse(vals['instance_id'])
                if vals.get('instance_id') else category.instance_id
            )
            self._kits_assert_unique_name_slug(
                instance=instance,
                name=name,
                slug=slug,
                exclude_id=category.id,
            )
        return super().write(vals)

    @api.constrains('parent_id', 'instance_id')
    def _check_parent_store(self):
        for category in self:
            if category.parent_id and category.parent_id.instance_id != category.instance_id:
                raise ValidationError(self.env._(
                    'A category parent must belong to the same WooCommerce store.',
                ))
            if category.parent_id:
                ancestor = category.parent_id
                seen = {category.id}
                while ancestor:
                    if ancestor.id in seen:
                        raise ValidationError(self.env._(
                            'Circular category hierarchy is not allowed.',
                        ))
                    seen.add(ancestor.id)
                    ancestor = ancestor.parent_id

    @api.model
    def _find(self, instance, *, external_id=False, slug=False):
        """Match a store-local category by Woo external id, then slug."""
        if not instance:
            return self.browse()
        domain = [('instance_id', '=', instance.id)]
        ext = _kits_norm_external_id(external_id)
        if ext:
            return self.with_context(active_test=False).search(
                domain + [('external_id', '=', ext)],
                limit=1,
            )
        if slug:
            slug_key = _kits_norm_key(slug)
            if not slug_key:
                return self.browse()
            # Prefer pending (no external id) when linking remote → local by slug.
            pending = self.browse()
            for row in self.with_context(active_test=False).search(domain):
                if _kits_norm_key(row.slug) != slug_key:
                    continue
                if not _kits_norm_external_id(row.external_id):
                    return row
                if not pending:
                    pending = row
            return pending
        return self.browse()

    def _kits_is_exported(self):
        self.ensure_one()
        return bool(_kits_norm_external_id(self.external_id)) and self.sync_state not in (
            'deleted', 'error',
        ) and self.active and self.exists_in_store


class KitsWooTag(models.Model):
    _name = 'kits.woo.tag'
    _description = 'WooCommerce Product Tag'
    _inherit = ['kits.woo.binding.sync', 'kits.woo.catalog.export.mixin']
    _order = 'name, id'
    _check_company_auto = True

    instance_id = fields.Many2one(
        'kits.woo.instance',
        string='WooCommerce Store',
        required=True,
        index=True,
        ondelete='cascade',
        domain="[('platform_type', '=', 'woocommerce')]",
    )
    company_id = fields.Many2one('res.company', required=True, index=True)
    external_id = fields.Char(
        string='WooCommerce Tag ID',
        index=True,
        copy=False,
        help='Empty until the tag is successfully exported or imported from WooCommerce.',
    )
    name = fields.Char(required=True, index=True)
    slug = fields.Char()
    description = fields.Html(sanitize=True)
    odoo_tag_id = fields.Many2one(
        'product.tag',
        string='Legacy Odoo Tag',
        ondelete='set null',
        help='Optional historical link. WooCommerce sync does not create or update native tags.',
    )

    _external_uniq = models.Constraint(
        'UNIQUE(instance_id, external_id)',
        'This WooCommerce tag is already registered for this store.',
    )

    def _kits_catalog_tracked_fields(self):
        return {'name', 'slug', 'description'}

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('instance_id') and not vals.get('company_id'):
                instance = self.env['kits.woo.instance'].browse(vals['instance_id'])
                vals['company_id'] = instance.company_id.id
            if vals.get('name'):
                vals['name'] = _kits_norm_text(vals['name'])
            if vals.get('slug'):
                vals['slug'] = _kits_norm_text(vals['slug'])
            if 'external_id' in vals:
                vals['external_id'] = _kits_norm_external_id(vals.get('external_id'))
            instance = self.env['kits.woo.instance'].browse(vals.get('instance_id'))
            self._kits_assert_unique_name_slug(
                instance=instance,
                name=vals.get('name'),
                slug=vals.get('slug'),
            )
            vals.setdefault('sync_state', 'synced' if vals.get('external_id') else 'not_synced')
        return super().create(vals_list)

    def write(self, vals):
        vals = dict(vals)
        if vals.get('name'):
            vals['name'] = _kits_norm_text(vals['name'])
        if 'slug' in vals and vals.get('slug'):
            vals['slug'] = _kits_norm_text(vals['slug'])
        if 'external_id' in vals:
            vals['external_id'] = _kits_norm_external_id(vals.get('external_id'))
        for tag in self:
            name = vals['name'] if 'name' in vals else tag.name
            slug = vals['slug'] if 'slug' in vals else tag.slug
            instance = (
                self.env['kits.woo.instance'].browse(vals['instance_id'])
                if vals.get('instance_id') else tag.instance_id
            )
            self._kits_assert_unique_name_slug(
                instance=instance,
                name=name,
                slug=slug,
                exclude_id=tag.id,
            )
        return super().write(vals)

    @api.model
    def _find(self, instance, *, external_id=False, name=False, slug=False):
        """Match a store-local tag by Woo external id, then slug, then name."""
        if not instance:
            return self.browse()
        domain = [('instance_id', '=', instance.id)]
        ext = _kits_norm_external_id(external_id)
        if ext:
            return self.with_context(active_test=False).search(
                domain + [('external_id', '=', ext)],
                limit=1,
            )
        if slug:
            slug_key = _kits_norm_key(slug)
            if slug_key:
                pending = self.browse()
                for row in self.with_context(active_test=False).search(domain):
                    if _kits_norm_key(row.slug) != slug_key:
                        continue
                    if not _kits_norm_external_id(row.external_id):
                        return row
                    if not pending:
                        pending = row
                if pending:
                    return pending
        if name:
            name_key = _kits_norm_key(name)
            pending = self.browse()
            for row in self.with_context(active_test=False).search(domain):
                if _kits_norm_key(row.name) != name_key:
                    continue
                if not _kits_norm_external_id(row.external_id):
                    return row
                if not pending:
                    pending = row
            return pending
        return self.browse()

    def _kits_is_exported(self):
        self.ensure_one()
        return bool(_kits_norm_external_id(self.external_id)) and self.sync_state not in (
            'deleted', 'error',
        ) and self.active and self.exists_in_store


class KitsWooAttribute(models.Model):
    _name = 'kits.woo.attribute'
    _description = 'WooCommerce Product Attribute'
    _inherit = 'kits.woo.binding.sync'
    _order = 'name, id'
    _check_company_auto = True

    instance_id = fields.Many2one(
        'kits.woo.instance',
        string='WooCommerce Store',
        required=True,
        index=True,
        ondelete='cascade',
        domain="[('platform_type', '=', 'woocommerce')]",
    )
    company_id = fields.Many2one('res.company', required=True, index=True)
    external_id = fields.Char(string='WooCommerce Attribute ID', required=True, index=True)
    name = fields.Char(required=True)
    slug = fields.Char()
    display_type = fields.Selection(
        selection=[
            ('select', 'Dropdown'),
            ('color', 'Color'),
            ('button', 'Button'),
            ('image', 'Image'),
        ],
        default='select',
        string='Display Type',
    )
    order_by = fields.Selection(
        selection=[
            ('menu_order', 'Custom ordering'),
            ('name', 'Name'),
            ('name_num', 'Name (numeric)'),
            ('id', 'Term ID'),
        ],
        default='menu_order',
        string='Default Sort Order',
    )
    has_archives = fields.Boolean(string='Enable Archives', default=False)
    odoo_attribute_id = fields.Many2one(
        'product.attribute',
        string='Odoo Attribute',
        ondelete='restrict',
        required=True,
        help='Native attribute used for variant matching.',
    )
    term_ids = fields.One2many('kits.woo.attribute.term', 'attribute_id', string='Terms')

    _external_uniq = models.Constraint(
        'UNIQUE(instance_id, external_id)',
        'This WooCommerce attribute is already registered for this store.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('instance_id') and not vals.get('company_id'):
                instance = self.env['kits.woo.instance'].browse(vals['instance_id'])
                vals['company_id'] = instance.company_id.id
            if vals.get('external_id') is not None:
                vals['external_id'] = str(vals['external_id'])
        return super().create(vals_list)

    @api.model
    def _find(self, instance, *, external_id=False, name=False):
        if not instance:
            return self.browse()
        domain = [('instance_id', '=', instance.id)]
        if external_id:
            domain.append(('external_id', '=', str(external_id)))
        elif name:
            domain.append(('name', '=', (name or '').strip()))
        else:
            return self.browse()
        return self.with_context(active_test=False).search(domain, limit=1)


class KitsWooAttributeTerm(models.Model):
    _name = 'kits.woo.attribute.term'
    _description = 'WooCommerce Product Attribute Term'
    _inherit = 'kits.woo.binding.sync'
    _order = 'name, id'
    _check_company_auto = True

    instance_id = fields.Many2one(
        'kits.woo.instance',
        string='WooCommerce Store',
        required=True,
        index=True,
        ondelete='cascade',
        domain="[('platform_type', '=', 'woocommerce')]",
    )
    company_id = fields.Many2one('res.company', required=True, index=True)
    attribute_id = fields.Many2one(
        'kits.woo.attribute',
        string='WooCommerce Attribute',
        required=True,
        index=True,
        ondelete='cascade',
        domain="[('instance_id', '=', instance_id)]",
    )
    external_id = fields.Char(string='WooCommerce Term ID', required=True, index=True)
    name = fields.Char(required=True)
    slug = fields.Char()
    odoo_attribute_value_id = fields.Many2one(
        'product.attribute.value',
        string='Odoo Attribute Value',
        ondelete='restrict',
        required=True,
        help='Native attribute value used for variant combinations.',
    )

    _external_uniq = models.Constraint(
        'UNIQUE(instance_id, attribute_id, external_id)',
        'This WooCommerce attribute term is already registered for this store.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            attribute = self.env['kits.woo.attribute'].browse(vals.get('attribute_id'))
            if attribute:
                vals.setdefault('instance_id', attribute.instance_id.id)
                vals.setdefault('company_id', attribute.company_id.id)
            if vals.get('instance_id') and not vals.get('company_id'):
                instance = self.env['kits.woo.instance'].browse(vals['instance_id'])
                vals['company_id'] = instance.company_id.id
            if vals.get('external_id') is not None:
                vals['external_id'] = str(vals['external_id'])
        return super().create(vals_list)

    @api.constrains('attribute_id', 'instance_id', 'odoo_attribute_value_id')
    def _check_attribute_consistency(self):
        for term in self:
            if term.attribute_id and term.attribute_id.instance_id != term.instance_id:
                raise ValidationError(self.env._(
                    'An attribute term must belong to an attribute of the same store.',
                ))
            if (
                term.odoo_attribute_value_id
                and term.attribute_id.odoo_attribute_id
                and term.odoo_attribute_value_id.attribute_id != term.attribute_id.odoo_attribute_id
            ):
                raise ValidationError(self.env._(
                    'The Odoo attribute value must belong to the linked Odoo attribute.',
                ))

    @api.model
    def _find(self, instance, *, attribute=False, external_id=False, name=False):
        if not instance:
            return self.browse()
        domain = [('instance_id', '=', instance.id)]
        if attribute:
            domain.append(('attribute_id', '=', attribute.id))
        if external_id:
            domain.append(('external_id', '=', str(external_id)))
        elif name:
            domain.append(('name', '=', (name or '').strip()))
        else:
            return self.browse()
        return self.with_context(active_test=False).search(domain, limit=1)
