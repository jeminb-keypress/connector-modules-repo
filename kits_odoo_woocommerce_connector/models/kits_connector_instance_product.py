# -*- coding: utf-8 -*-
import logging

from odoo import fields, models
from odoo.fields import Command
from odoo.tools import html2plaintext

from odoo.addons.kits_odoo_base_connector.services.exceptions import ConnectorAPIError

_logger = logging.getLogger(__name__)

# Woo status → Odoo active
_WOO_INACTIVE_STATUSES = frozenset({'draft', 'pending', 'private', 'trash'})


class KitsConnectorInstanceProduct(models.Model):
    _inherit = 'kits.connector.instance'

    # =========================================================================
    # Product import
    # =========================================================================

    def _woo_import_products(self, job=None):
        """Import WooCommerce products into Odoo (create/update via bindings).

        Job payload keys (all optional):
        - ``woo_ids``: list of Woo product IDs (Specific Id Filter)
        - ``after`` / ``modified_after``: ISO datetime string for modified-after filter
        - ``template_ids``: unused on import (export only)
        """
        self.ensure_one()
        api = self._woo_api()
        payload = (job.payload if job else None) or {}
        woo_ids = self._woo_parse_id_list(payload.get('woo_ids'))
        modified_after = payload.get('modified_after') or payload.get('after')

        imported = updated = skipped = failed = 0
        errors = []

        if woo_ids:
            products_iter = self._woo_iter_products_by_ids(api, woo_ids)
        else:
            params = {}
            if modified_after:
                params['modified_after'] = str(modified_after).replace(' ', 'T')
            products_iter = api.get_products(**params)

        for woo_product in products_iter:
            try:
                result = self._woo_import_one_product(woo_product, job=job)
                if result == 'created':
                    imported += 1
                elif result == 'updated':
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001 - continue batch; collect failures
                failed += 1
                woo_id = woo_product.get('id') if isinstance(woo_product, dict) else False
                errors.append(f"Product {woo_id}: {exc}")
                _logger.exception(
                    'WooCommerce product import failed for %s on instance %s',
                    woo_id,
                    self.id,
                )
                self._kits_log_operation(
                    operation='woo_import_products',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f"woo_product_id={woo_id}",
                    job=job,
                )
                if woo_id:
                    binding = self._woo_binding()._kits_find_binding(
                        self, 'product.template', external_id=str(woo_id),
                    )
                    if binding:
                        binding._kits_mark_error(str(exc))

        self.woo_last_product_sync = fields.Datetime.now()
        summary = (
            f'Products import finished: created={imported}, updated={updated}, '
            f'skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_iter_products_by_ids(self, api, woo_ids):
        for woo_id in woo_ids:
            try:
                remote = api.get_product(woo_id)
            except Exception as exc:  # noqa: BLE001
                binding = self._woo_binding()._kits_find_binding(
                    self, 'product.template', external_id=str(woo_id),
                )
                if binding:
                    binding._kits_mark_error(str(exc))
                self._kits_log_operation(
                    operation='woo_import_products',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f'woo_product_id={woo_id}',
                )
                continue
            if not remote or not remote.get('id'):
                binding = self._woo_binding()._kits_find_binding(
                    self, 'product.template', external_id=str(woo_id),
                )
                if binding:
                    binding._kits_mark_error(
                        self.env._('WooCommerce product %(id)s is missing or inaccessible.', id=woo_id)
                    )
                continue
            yield remote

    def _woo_import_one_product(self, woo_product, job=None):
        self.ensure_one()
        woo_id = woo_product.get('id')
        if not woo_id:
            return 'skipped'

        status = (woo_product.get('status') or 'publish').lower()
        if status == 'trash':
            self._woo_mark_product_deleted_remote(
                woo_id,
                message=self.env._(
                    'WooCommerce product %(id)s is in trash. The Odoo product was kept.',
                    id=woo_id,
                ),
            )
            return 'deleted'

        if self._woo_skip_deleted_local_product(woo_id, job=job):
            return 'skipped'

        ptype = woo_product.get('type') or 'simple'
        template, result = self._woo_match_or_prepare_template(woo_product)
        if result == 'ambiguous':
            return 'ambiguous'

        category = self._woo_ensure_categories(woo_product.get('categories') or [])
        vals = self._woo_prepare_template_vals(woo_product, category)
        # Context flag avoids marking Not Synced while applying a remote pull.
        write_ctx = {'kits_woo_skip_dirty': True}
        if template:
            template.with_context(**write_ctx).write(vals)
        else:
            template = self.env['product.template'].with_context(**write_ctx).create(vals)
            result = 'created'

        tag_ids = self._woo_ensure_tags(woo_product.get('tags') or [])
        if tag_ids is not None and 'product_tag_ids' in template._fields:
            template.with_context(**write_ctx).write({'product_tag_ids': [Command.set(tag_ids)]})

        binding = self._woo_bind('product.template', template.id, woo_id, external_data={
            'sku': woo_product.get('sku'),
            'type': ptype,
            'status': woo_product.get('status'),
            'date_modified': self._woo_normalize_remote_datetime(
                woo_product.get('date_modified_gmt') or woo_product.get('date_modified')
            ) or False,
            'variation_id': False,
            'meta': self._woo_meta_snapshot(woo_product),
        })
        # Imported from this store → keep it selected for future exports to this store only
        if self not in template.woo_instance_ids:
            template.with_context(**write_ctx).write({
                'woo_instance_ids': [(4, self.id)],
            })

        self._woo_sync_template_images(template, woo_product.get('images') or [], binding=binding)

        if ptype == 'variable':
            self._woo_import_variable_product(template, woo_product)
        else:
            variant = template.product_variant_id
            if variant:
                variant_vals = {
                    'default_code': woo_product.get('sku') or variant.default_code,
                    'lst_price': float(
                        woo_product.get('regular_price') or woo_product.get('price') or 0.0
                    ),
                }
                barcode = (woo_product.get('barcode') or '').strip() or False
                if barcode and 'barcode' in variant._fields:
                    # Avoid company-wide barcode conflicts: only set when free or already ours
                    conflict = self.env['product.product'].search([
                        ('barcode', '=', barcode),
                        ('id', '!=', variant.id),
                        ('company_id', 'in', [False, self.company_id.id]),
                    ], limit=1)
                    if not conflict:
                        variant_vals['barcode'] = barcode
                cost = self._woo_extract_cost(woo_product)
                if cost is not None:
                    variant_vals['standard_price'] = cost
                variant.with_context(**write_ctx).write(variant_vals)
                self._woo_bind('product.product', variant.id, woo_id, external_data={
                    'sku': woo_product.get('sku'),
                    'parent_id': False,
                    'variation_id': False,
                    'barcode': barcode or False,
                    'date_modified': self._woo_normalize_remote_datetime(
                        woo_product.get('date_modified_gmt') or woo_product.get('date_modified')
                    ) or False,
                })
        return result

    def _woo_match_or_prepare_template(self, woo_product):
        """Match order: binding → validated SKU (unbound) → create."""
        self.ensure_one()
        woo_id = woo_product.get('id')
        existing = self._woo_find_odoo('product.template', woo_id)
        if existing:
            return existing, 'updated'

        # Simple products may already be bound at variant level
        if (woo_product.get('type') or 'simple') == 'simple':
            bound_variant = self._woo_find_odoo('product.product', woo_id)
            if bound_variant:
                return bound_variant.product_tmpl_id, 'updated'

        if self.woo_match_by_sku:
            sku = (woo_product.get('sku') or '').strip()
            if sku:
                matched = self._woo_find_unbound_template_by_sku(sku)
                if isinstance(matched, str):
                    return self.env['product.template'], 'ambiguous'
                if matched:
                    return matched, 'updated'
        return self.env['product.template'], 'created'

    def _woo_find_unbound_template_by_sku(self, sku):
        """Return the single unbound template for this SKU, or ``'ambiguous'``.

        Zero matches → empty recordset (caller creates). One template → that template
        when it is not already bound to a different Woo product on this store.
        Two or more templates → do not guess.
        """
        self.ensure_one()
        products = self.env['product.product'].search([
            ('default_code', '=', sku),
            ('company_id', 'in', [False, self.company_id.id]),
        ])
        templates = products.mapped('product_tmpl_id')
        if len(templates) > 1:
            self._woo_log_sku_ambiguity(sku, templates)
            return 'ambiguous'
        if not templates:
            return self.env['product.template']

        template = templates
        Binding = self._woo_binding()
        tmpl_bound = Binding._kits_find_binding(
            self, 'product.template', odoo_record_id=template.id,
        )
        if tmpl_bound and tmpl_bound.sync_state not in ('deleted_local',):
            self._kits_log_operation(
                operation='woo_import_products',
                status='failure',
                level='warning',
                message=self.env._(
                    'SKU %(sku)s is already linked to WooCommerce product %(woo)s on %(store)s. '
                    'Import did not create or reassign a product.',
                    sku=sku,
                    woo=tmpl_bound.external_id,
                    store=self.name,
                ),
            )
            return 'ambiguous'
        for variant in template.product_variant_ids:
            var_bound = Binding._kits_find_binding(
                self, 'product.product', odoo_record_id=variant.id,
            )
            if var_bound and var_bound.sync_state not in ('deleted_local',):
                self._kits_log_operation(
                    operation='woo_import_products',
                    status='failure',
                    level='warning',
                    message=self.env._(
                        'SKU %(sku)s is already linked to WooCommerce variation %(woo)s on %(store)s. '
                        'Import did not create or reassign a product.',
                        sku=sku,
                        woo=var_bound.external_id,
                        store=self.name,
                    ),
                )
                return 'ambiguous'
        return template

    def _woo_log_sku_ambiguity(self, sku, templates):
        names = ', '.join(templates.mapped('display_name')[:10])
        self._kits_log_operation(
            operation='woo_import_products',
            status='failure',
            level='warning',
            message=self.env._(
                'SKU %(sku)s matches %(count)s Odoo products on %(store)s. '
                'No product was created or linked. Review: %(names)s.',
                sku=sku,
                count=len(templates),
                store=self.name,
                names=names,
            ),
        )

    def _woo_skip_deleted_local_product(self, woo_id, job=None):
        """Skip automatic import when the Odoo product was deleted and must not be recreated."""
        self.ensure_one()
        Binding = self._woo_binding()
        for model in ('product.template', 'product.product'):
            binding = Binding._kits_find_binding(self, model, external_id=str(woo_id))
            if not binding or binding.sync_state != 'deleted_local':
                continue
            if binding._kits_get_odoo_record():
                return False
            if self._woo_allows_recreate(job):
                return False
            self._kits_log_operation(
                operation='woo_import_products',
                status='success',
                level='warning',
                message=self.env._(
                    'Skipped WooCommerce product %(id)s on %(store)s because the Odoo product '
                    'was deleted. Automatic sync will not recreate it.',
                    id=woo_id,
                    store=self.name,
                ),
            )
            return True
        return False

    def _woo_fetch_product_if_exists(self, api, woo_id):
        """Return the remote product only when its id matches. 404 → None."""
        try:
            remote = api.get_product(int(woo_id))
        except ConnectorAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        except Exception as exc:  # noqa: BLE001
            if self._woo_is_not_found(exc):
                return None
            raise
        if not remote or str(remote.get('id')) != str(int(woo_id)):
            return None
        return remote

    def _woo_mark_product_deleted_remote(self, woo_id, message=False):
        """Mark this store's product bindings deleted and drop only this store from Connected Instances.

        Does not archive or delete the Odoo product, and does not touch other stores.
        Safe to call more than once.
        """
        self.ensure_one()
        woo_key = str(woo_id)
        message = message or self.env._(
            'Remote WooCommerce product %(id)s was deleted on %(store)s. The Odoo product was kept.',
            id=woo_key,
            store=self.name,
        )
        Binding = self._woo_binding()
        template_binding = Binding._kits_find_binding(
            self, 'product.template', external_id=woo_key,
        )
        template = self.env['product.template']
        if template_binding:
            template_binding._kits_mark_deleted_remote(message=message)
            template = template_binding._kits_get_odoo_record()

        variant_domain = [
            ('instance_id', '=', self.id),
            ('odoo_model', '=', 'product.product'),
        ]
        if template:
            variant_domain.append(('odoo_record_id', 'in', template.with_context(active_test=False).product_variant_ids.ids or [0]))
        variants = Binding.search(variant_domain)
        if not template:
            variants = Binding.search([
                ('instance_id', '=', self.id),
                ('odoo_model', '=', 'product.product'),
                ('external_id', '=', woo_key),
            ])
            parent_matches = Binding.search([
                ('instance_id', '=', self.id),
                ('odoo_model', '=', 'product.product'),
            ]).filtered(
                lambda binding: str((binding.external_data or {}).get('parent_id') or '') == woo_key
            )
            variants = variants | parent_matches
        if variants:
            variants._kits_mark_deleted_remote(message=message)

        simple = Binding.search([
            ('instance_id', '=', self.id),
            ('odoo_model', '=', 'product.product'),
            ('external_id', '=', woo_key),
        ])
        if simple:
            simple._kits_mark_deleted_remote(message=message)

        templates = template
        for binding in variants | simple:
            record = binding._kits_get_odoo_record()
            if record and record._name == 'product.product':
                templates |= record.product_tmpl_id
            elif record and record._name == 'product.template':
                templates |= record
        for tmpl in templates:
            if self in tmpl.woo_instance_ids:
                tmpl.with_context(kits_woo_skip_dirty=True).write({
                    'woo_instance_ids': [Command.unlink(self.id)],
                })
        return True

    def _woo_prepare_template_vals(self, woo_product, category):
        name = woo_product.get('name') or f"Woo Product {woo_product.get('id')}"
        description_html = woo_product.get('description') or ''
        short_html = woo_product.get('short_description') or ''
        status = (woo_product.get('status') or 'publish').lower()
        vals = {
            'name': name,
            'type': 'consu',
            'is_storable': True,
            'description': description_html or False,
            'description_sale': html2plaintext(short_html or description_html) or False,
            'list_price': float(
                woo_product.get('regular_price') or woo_product.get('price') or 0.0
            ),
            'company_id': self.company_id.id,
            'active': status not in _WOO_INACTIVE_STATUSES,
            'sale_ok': True,
        }
        visibility = (woo_product.get('catalog_visibility') or 'visible').lower()
        if visibility in ('visible', 'catalog', 'search', 'hidden'):
            vals['woo_catalog_visibility'] = visibility
        if 'featured' in woo_product:
            vals['woo_featured'] = bool(woo_product.get('featured'))
        if category:
            vals['categ_id'] = category.id
        sku = woo_product.get('sku')
        if sku and (woo_product.get('type') or 'simple') == 'simple':
            vals['default_code'] = sku
        return vals

    def _woo_ensure_categories(self, woo_categories):
        """Map WooCommerce categories; primary (first) becomes product.categ_id."""
        self.ensure_one()
        if not woo_categories:
            return self.env['product.category']
        primary = self.env['product.category']
        for woo_cat in woo_categories:
            woo_cat_id = woo_cat.get('id')
            if not woo_cat_id:
                continue
            existing = self._woo_find_odoo('product.category', woo_cat_id)
            if existing:
                category = existing
            else:
                category = self.env['product.category'].create({
                    'name': woo_cat.get('name') or f'Woo Category {woo_cat_id}',
                })
                self._woo_bind('product.category', category.id, woo_cat_id)
            if not primary:
                primary = category
        return primary

    def _woo_ensure_tags(self, woo_tags):
        """Create/find product.tag records and return ids (or None if model missing)."""
        self.ensure_one()
        if 'product.tag' not in self.env:
            return None
        tag_ids = []
        Tag = self.env['product.tag']
        for woo_tag in woo_tags:
            name = (woo_tag.get('name') or '').strip()
            if not name:
                continue
            woo_tag_id = woo_tag.get('id')
            tag = self.env['product.tag']
            if woo_tag_id:
                tag = self._woo_find_odoo('product.tag', woo_tag_id)
            if not tag:
                tag = Tag.search([('name', '=', name)], limit=1)
            if not tag:
                tag = Tag.create({'name': name})
            if woo_tag_id:
                self._woo_bind('product.tag', tag.id, woo_tag_id)
            tag_ids.append(tag.id)
        return tag_ids

    def _woo_import_variable_product(self, template, woo_product):
        """Import attributes/variations for a variable WooCommerce product."""
        self.ensure_one()
        api = self._woo_api()
        woo_attrs = woo_product.get('attributes') or []

        attr_lines_cmds = []
        attr_value_map = {}  # (attr_name.lower(), option.lower()) -> product.attribute.value
        for woo_attr in woo_attrs:
            if not woo_attr.get('variation'):
                continue
            attr_name = (woo_attr.get('name') or '').strip()
            if not attr_name:
                continue
            attribute = self.env['product.attribute'].search([('name', '=', attr_name)], limit=1)
            if not attribute:
                attribute = self.env['product.attribute'].create({
                    'name': attr_name,
                    'create_variant': 'always',
                })
            value_ids = []
            for option in woo_attr.get('options') or []:
                option = (option or '').strip()
                if not option:
                    continue
                pav = self.env['product.attribute.value'].search([
                    ('attribute_id', '=', attribute.id),
                    ('name', '=', option),
                ], limit=1)
                if not pav:
                    pav = self.env['product.attribute.value'].create({
                        'name': option,
                        'attribute_id': attribute.id,
                    })
                value_ids.append(pav.id)
                attr_value_map[(attr_name.lower(), option.lower())] = pav
            existing_line = template.attribute_line_ids.filtered(
                lambda line, attribute=attribute: line.attribute_id == attribute
            )
            if existing_line:
                existing_line.value_ids = [Command.set(value_ids)]
            else:
                attr_lines_cmds.append(Command.create({
                    'attribute_id': attribute.id,
                    'value_ids': [Command.set(value_ids)],
                }))
        if attr_lines_cmds:
            template.write({'attribute_line_ids': attr_lines_cmds})

        template._create_variant_ids()

        for variation in api.get_variations(woo_product['id']):
            self._woo_import_one_variation(template, woo_product['id'], variation, attr_value_map)

    def _woo_import_one_variation(self, template, parent_woo_id, variation, attr_value_map):
        self.ensure_one()
        variation_id = variation.get('id')
        if not variation_id:
            return

        existing_variant = self._woo_find_odoo('product.product', variation_id)
        combination = []
        for attr in variation.get('attributes') or []:
            attr_name = (attr.get('name') or '').strip().lower()
            option = (attr.get('option') or '').strip().lower()
            pav = attr_value_map.get((attr_name, option))
            if not pav:
                continue
            ptav = template.attribute_line_ids.filtered(
                lambda line, pav=pav: line.attribute_id == pav.attribute_id
            ).product_template_value_ids.filtered(
                lambda v, pav=pav: v.product_attribute_value_id == pav
            )
            if ptav:
                combination.append(ptav[0])

        if existing_variant:
            variant = existing_variant
        else:
            combination_rs = self.env['product.template.attribute.value']
            for ptav in combination:
                combination_rs |= ptav
            variant = (
                template._get_variant_for_combination(combination_rs)
                if combination_rs else self.env['product.product']
            )
            if not variant:
                sku = (variation.get('sku') or '').strip()
                if sku and self.woo_match_by_sku:
                    matches = template.product_variant_ids.filtered(
                        lambda p, sku=sku: p.default_code == sku
                    )
                    if len(matches) > 1:
                        self._woo_log_sku_ambiguity(sku, template)
                        return
                    variant = matches
            if not variant and len(template.product_variant_ids) == 1:
                variant = template.product_variant_ids
            if not variant:
                _logger.warning(
                    'Could not match WooCommerce variation %s to an Odoo variant on template %s',
                    variation_id,
                    template.id,
                )
                return

        variant_vals = {
            'default_code': variation.get('sku') or variant.default_code,
            'lst_price': float(variation.get('regular_price') or variation.get('price') or 0.0),
        }
        barcode = (variation.get('barcode') or '').strip() or False
        if barcode and 'barcode' in variant._fields:
            conflict = self.env['product.product'].search([
                ('barcode', '=', barcode),
                ('id', '!=', variant.id),
                ('company_id', 'in', [False, self.company_id.id]),
            ], limit=1)
            if not conflict:
                variant_vals['barcode'] = barcode
        cost = self._woo_extract_cost(variation)
        if cost is not None:
            variant_vals['standard_price'] = cost
        variant.with_context(kits_woo_skip_dirty=True).write(variant_vals)

        binding = self._woo_bind('product.product', variant.id, variation_id, external_data={
            'sku': variation.get('sku'),
            'parent_id': parent_woo_id,
            'variation_id': variation_id,
            'barcode': barcode or False,
            'date_modified': self._woo_normalize_remote_datetime(
                variation.get('date_modified_gmt') or variation.get('date_modified')
            ) or False,
        })
        self._woo_sync_variant_image(variant, variation.get('image') or {}, binding=binding)

    # =========================================================================
    # Product export
    # =========================================================================

    def _woo_export_products(self, job=None):
        """Export Odoo products to WooCommerce.

        Selection rules:
        - ``payload.template_ids``: export exactly those templates (record-level / wizard).
          Still skipped when this store is not in the product's ``woo_instance_ids``
          unless ``payload.force`` is set.
        - else templates that list **this** store in ``woo_instance_ids``.
        """
        self.ensure_one()
        api = self._woo_api()
        payload = (job.payload if job else None) or {}
        ProductTemplate = self.env['product.template']
        template_ids = self._woo_parse_id_list(payload.get('template_ids'))
        expected_versions = payload.get('sync_versions') or {}
        force = bool(payload.get('force'))

        if template_ids:
            templates = ProductTemplate.browse(template_ids).exists()
            if not force:
                templates = templates.filtered(lambda t: self in t.woo_instance_ids)
        else:
            domain = [
                ('company_id', 'in', [False, self.company_id.id]),
                ('sale_ok', '=', True),
                ('woo_instance_ids', 'in', self.id),
            ]
            templates = ProductTemplate.search(domain)

        created = updated = failed = skipped = 0
        errors = []
        for template in templates:
            try:
                binding = self._woo_binding()._kits_find_binding(
                    self, 'product.template', odoo_record_id=template.id,
                )
                expected = expected_versions.get(str(template.id))
                if binding and expected is not None and binding._kits_is_stale_job(expected):
                    skipped += 1
                    continue
                result = self._woo_export_one_template(
                    template,
                    api,
                    allow_recreate=self._woo_allows_recreate(job),
                )
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
                elif result == 'skipped':
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f'Template {template.id}: {exc}')
                _logger.exception(
                    'WooCommerce product export failed for template %s', template.id,
                )
                self._kits_log_operation(
                    operation='woo_export_products',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f'product.template,{template.id}',
                    job=job,
                )
                binding = self._woo_binding()._kits_find_binding(
                    self, 'product.template', odoo_record_id=template.id,
                )
                if binding:
                    binding._kits_mark_error(str(exc))

        self.woo_last_product_sync = fields.Datetime.now()
        summary = (
            f'Products export finished: created={created}, updated={updated}, '
            f'skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        if not templates:
            summary = (
                f'{summary}\n'
                'No products were selected for this store. On each product, open the '
                'WooCommerce tab and choose this store under WooCommerce Stores, '
                'or use Export to WooCommerce / the sync wizard with specific IDs.'
            )
        return summary

    def _woo_export_one_template(self, template, api, *, allow_recreate=False):
        self.ensure_one()
        Binding = self._woo_binding()
        binding = Binding._kits_find_binding(
            self, 'product.template', odoo_record_id=template.id,
        )
        if binding and binding.sync_state in ('deleted', 'deleted_local') and not allow_recreate:
            return 'skipped'
        if binding and binding.sync_state == 'deleted_local':
            # Local deletion keeps the remote product. Do not open a second remote product.
            return 'skipped'

        is_variable = len(template.product_variant_ids) > 1 or bool(template.attribute_line_ids)
        price = self._woo_get_export_price(template.product_variant_id or template)
        payload = {
            'name': template.name,
            'type': 'variable' if is_variable else 'simple',
            'regular_price': f'{price:.2f}' if not is_variable else '',
            'description': template.description or '',
            'short_description': template.description_sale or '',
            'sku': template.default_code or '',
            'manage_stock': bool(template.is_storable) if 'is_storable' in template._fields else True,
            'status': 'publish' if template.active else 'draft',
            'catalog_visibility': template.woo_catalog_visibility or 'visible',
            'featured': bool(template.woo_featured),
        }
        if template.barcode and not is_variable:
            payload['barcode'] = template.barcode

        # Categories
        categ_binding = Binding._kits_find_binding(
            self, 'product.category', odoo_record_id=template.categ_id.id,
        ) if template.categ_id else False
        if categ_binding:
            payload['categories'] = [{'id': int(categ_binding.external_id)}]

        # Tags
        if 'product_tag_ids' in template._fields and template.product_tag_ids:
            tag_payload = []
            for tag in template.product_tag_ids:
                tag_binding = Binding._kits_find_binding(
                    self, 'product.tag', odoo_record_id=tag.id,
                )
                if tag_binding:
                    tag_payload.append({'id': int(tag_binding.external_id)})
                else:
                    tag_payload.append({'name': tag.name})
            if tag_payload:
                payload['tags'] = tag_payload

        if is_variable:
            payload['attributes'] = self._woo_export_attribute_payload(template)

        images = self._woo_prepare_export_images(template)
        if images:
            payload['images'] = images

        remote = None
        woo_id = None
        result = None
        create_new = not binding or binding.sync_state == 'deleted'
        if binding and binding.sync_state != 'deleted':
            woo_id = int(binding.external_id)
            try:
                remote = api.update_product(woo_id, payload)
                result = 'updated'
                create_new = False
            except Exception as exc:  # noqa: BLE001
                if not self._woo_is_not_found(exc):
                    raise
                if not allow_recreate:
                    self._woo_mark_product_deleted_remote(
                        woo_id,
                        message=self.env._(
                            'WooCommerce product %(id)s was not found during export. '
                            'It was not recreated.',
                            id=woo_id,
                        ),
                    )
                    return 'skipped'
                create_new = True
        elif binding and binding.sync_state == 'deleted' and allow_recreate:
            existing_remote = self._woo_fetch_product_if_exists(api, binding.external_id)
            if existing_remote:
                woo_id = int(existing_remote['id'])
                remote = api.update_product(woo_id, payload)
                result = 'updated'
                create_new = False

        if create_new:
            remote = api.create_product(payload)
            woo_id = remote.get('id')
            if not woo_id:
                raise ValueError('WooCommerce did not return a product id')
            result = 'created'

        # Always re-bind after a successful push so template sync_state becomes Synced
        # (create path used to bind only here; update path previously left Not Synced).
        binding = self._woo_bind('product.template', template.id, woo_id, external_data={
            'sku': template.default_code,
            'type': payload['type'],
        })
        if remote.get('images'):
            self._woo_remember_remote_images(binding, remote.get('images'))

        if payload['type'] == 'simple':
            variant = template.product_variant_id
            if variant:
                self._woo_bind('product.product', variant.id, woo_id, external_data={
                    'sku': variant.default_code,
                    'parent_id': False,
                })
        else:
            self._woo_export_variations(
                template, int(woo_id), api, allow_recreate=allow_recreate,
            )
        return result

    def _woo_export_attribute_payload(self, template):
        attributes = []
        position = 0
        for line in template.attribute_line_ids:
            attributes.append({
                'name': line.attribute_id.name,
                'visible': True,
                'variation': True,
                'options': line.value_ids.mapped('name'),
                'position': position,
            })
            position += 1
        return attributes

    def _woo_remember_remote_images(self, binding, remote_images):
        meta = dict(binding.external_data or {})
        image_meta = dict(meta.get('images') or {})
        for idx, img in enumerate(remote_images or []):
            woo_id = img.get('id')
            if not woo_id:
                continue
            role = 'main' if idx == 0 else 'gallery'
            image_meta[str(woo_id)] = {
                'hash': (image_meta.get(str(woo_id)) or {}).get('hash'),
                'src': img.get('src'),
                'role': role,
            }
        meta['images'] = image_meta
        binding.write({'external_data': meta})

    def _woo_export_variations(self, template, parent_woo_id, api, *, allow_recreate=False):
        self.ensure_one()
        Binding = self._woo_binding()
        for variant in template.product_variant_ids:
            price = self._woo_get_export_price(variant)
            attributes = []
            for ptav in variant.product_template_attribute_value_ids:
                attributes.append({
                    'name': ptav.attribute_id.name,
                    'option': ptav.name,
                })
            payload = {
                'regular_price': f'{price:.2f}',
                'sku': variant.default_code or '',
                'manage_stock': True,
                'attributes': attributes,
            }
            if variant.barcode:
                payload['barcode'] = variant.barcode
            image_payload = self._woo_prepare_variant_export_image(variant)
            if image_payload:
                payload['image'] = image_payload

            binding = Binding._kits_find_binding(
                self, 'product.product', odoo_record_id=variant.id,
            )
            if binding and binding.sync_state in ('deleted', 'deleted_local') and not allow_recreate:
                continue
            if binding and binding.sync_state == 'deleted_local':
                continue
            variation_data = {
                'sku': variant.default_code,
                'parent_id': parent_woo_id,
                'variation_id': False,
            }
            if binding and binding.external_data and binding.external_data.get('parent_id') and binding.sync_state != 'deleted':
                try:
                    api.update_variation(parent_woo_id, int(binding.external_id), payload)
                    variation_data['variation_id'] = binding.external_id
                    self._woo_bind(
                        'product.product', variant.id, binding.external_id,
                        external_data=variation_data,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    if not self._woo_is_not_found(exc) or not allow_recreate:
                        if self._woo_is_not_found(exc):
                            binding._kits_mark_deleted_remote(message=self.env._(
                                'WooCommerce variation %(id)s was not found. It was not recreated.',
                                id=binding.external_id,
                            ))
                        else:
                            raise
            elif binding and binding.sync_state == 'deleted' and not allow_recreate:
                continue
            remote = api.create_variation(parent_woo_id, payload)
            if remote.get('id'):
                variation_data['variation_id'] = remote['id']
                self._woo_bind(
                    'product.product', variant.id, remote['id'],
                    external_data=variation_data,
                )

    def _woo_get_export_price(self, product):
        self.ensure_one()
        if not product:
            return 0.0
        if self.pricelist_id and product._name == 'product.product':
            return self.pricelist_id._get_product_price(product, 1.0)
        if product._name == 'product.product':
            return product.lst_price
        return product.list_price
