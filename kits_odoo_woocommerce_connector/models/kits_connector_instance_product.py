# -*- coding: utf-8 -*-
import logging

from odoo import fields, models
from odoo.fields import Command
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)

# Woo status → Odoo active
_WOO_INACTIVE_STATUSES = frozenset({'draft', 'pending', 'private', 'trash'})


class KitsConnectorInstanceProduct(models.Model):
    _inherit = 'kits.connector.instance'

    # =========================================================================
    # Product import
    # =========================================================================

    def _woo_import_products(self, job=None):
        """Import WooCommerce products into Odoo (create/update via bindings)."""
        self.ensure_one()
        api = self._woo_api()
        imported = updated = skipped = failed = 0
        errors = []

        # Import all statuses; map draft/private to inactive Odoo products
        for woo_product in api.get_products():
            try:
                result = self._woo_import_one_product(woo_product)
                if result == 'created':
                    imported += 1
                elif result == 'updated':
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001 - continue batch; collect failures
                failed += 1
                errors.append(f"Product {woo_product.get('id')}: {exc}")
                _logger.exception(
                    'WooCommerce product import failed for %s on instance %s',
                    woo_product.get('id'),
                    self.id,
                )
                self._kits_log_operation(
                    operation='woo_import_products',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f"woo_product_id={woo_product.get('id')}",
                    job=job,
                )

        self.woo_last_product_sync = fields.Datetime.now()
        summary = (
            f'Products import finished: created={imported}, updated={updated}, '
            f'skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_import_one_product(self, woo_product):
        self.ensure_one()
        woo_id = woo_product.get('id')
        if not woo_id:
            return 'skipped'

        ptype = woo_product.get('type') or 'simple'
        template, result = self._woo_match_or_prepare_template(woo_product)

        category = self._woo_ensure_categories(woo_product.get('categories') or [])
        vals = self._woo_prepare_template_vals(woo_product, category)
        if template:
            template.write(vals)
        else:
            template = self.env['product.template'].create(vals)
            result = 'created'

        tag_ids = self._woo_ensure_tags(woo_product.get('tags') or [])
        if tag_ids is not None and 'product_tag_ids' in template._fields:
            template.write({'product_tag_ids': [Command.set(tag_ids)]})

        binding = self._woo_bind('product.template', template.id, woo_id, external_data={
            'sku': woo_product.get('sku'),
            'type': ptype,
            'status': woo_product.get('status'),
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
                variant.write(variant_vals)
                self._woo_bind('product.product', variant.id, woo_id, external_data={
                    'sku': woo_product.get('sku'),
                    'parent_id': False,
                    'barcode': barcode or False,
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
                if matched:
                    return matched, 'updated'
        return self.env['product.template'], 'created'

    def _woo_find_unbound_template_by_sku(self, sku):
        """Find a company product with this SKU that is not bound on this instance."""
        self.ensure_one()
        Product = self.env['product.product']
        candidates = Product.search([
            ('default_code', '=', sku),
            ('company_id', 'in', [False, self.company_id.id]),
        ])
        Binding = self._woo_binding()
        for product in candidates:
            tmpl_bound = Binding._kits_find_binding(
                self, 'product.template', odoo_record_id=product.product_tmpl_id.id,
            )
            var_bound = Binding._kits_find_binding(
                self, 'product.product', odoo_record_id=product.id,
            )
            if tmpl_bound or var_bound:
                continue
            return product.product_tmpl_id
        return self.env['product.template']

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
                    variant = template.product_variant_ids.filtered(
                        lambda p, sku=sku: p.default_code == sku
                    )[:1]
                    # Also allow unbound SKU match within template only
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
        variant.write(variant_vals)

        binding = self._woo_bind('product.product', variant.id, variation_id, external_data={
            'sku': variation.get('sku'),
            'parent_id': parent_woo_id,
            'barcode': barcode or False,
        })
        self._woo_sync_variant_image(variant, variation.get('image') or {}, binding=binding)

    # =========================================================================
    # Product export
    # =========================================================================

    def _woo_export_products(self, job=None):
        """Export Odoo products marked for WooCommerce (or unbound templates) to Woo."""
        self.ensure_one()
        api = self._woo_api()
        domain = [
            ('company_id', 'in', [False, self.company_id.id]),
            ('sale_ok', '=', True),
            ('active', '=', True),
        ]
        ProductTemplate = self.env['product.template']
        if 'woo_sync_to_store' in ProductTemplate._fields:
            flagged = ProductTemplate.search(domain + [('woo_sync_to_store', '=', True)])
            templates = flagged or ProductTemplate.search(domain, limit=200)
        else:
            templates = ProductTemplate.search(domain, limit=200)

        created = updated = failed = 0
        errors = []
        for template in templates:
            try:
                result = self._woo_export_one_template(template, api)
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
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

        self.woo_last_product_sync = fields.Datetime.now()
        summary = f'Products export finished: created={created}, updated={updated}, failed={failed}'
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_export_one_template(self, template, api):
        self.ensure_one()
        Binding = self._woo_binding()
        binding = Binding._kits_find_binding(
            self, 'product.template', odoo_record_id=template.id,
        )
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

        if binding:
            woo_id = int(binding.external_id)
            remote = api.update_product(woo_id, payload)
            result = 'updated'
        else:
            remote = api.create_product(payload)
            woo_id = remote.get('id')
            if not woo_id:
                raise ValueError('WooCommerce did not return a product id')
            self._woo_bind('product.template', template.id, woo_id, external_data={
                'sku': template.default_code,
                'type': payload['type'],
            })
            result = 'created'
            # Capture remote image ids from create response when present
            if remote.get('images'):
                binding = Binding._kits_find_binding(
                    self, 'product.template', odoo_record_id=template.id,
                )
                if binding:
                    self._woo_remember_remote_images(binding, remote.get('images'))

        if payload['type'] == 'simple':
            variant = template.product_variant_id
            if variant:
                self._woo_bind('product.product', variant.id, woo_id, external_data={
                    'sku': variant.default_code,
                    'parent_id': False,
                })
        else:
            self._woo_export_variations(template, int(woo_id), api)
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

    def _woo_export_variations(self, template, parent_woo_id, api):
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
            if binding and binding.external_data and binding.external_data.get('parent_id'):
                api.update_variation(parent_woo_id, int(binding.external_id), payload)
            elif binding and str(binding.external_id) == str(parent_woo_id):
                remote = api.create_variation(parent_woo_id, payload)
                if remote.get('id'):
                    self._woo_bind('product.product', variant.id, remote['id'], external_data={
                        'sku': variant.default_code,
                        'parent_id': parent_woo_id,
                    })
            else:
                remote = api.create_variation(parent_woo_id, payload)
                if remote.get('id'):
                    self._woo_bind('product.product', variant.id, remote['id'], external_data={
                        'sku': variant.default_code,
                        'parent_id': parent_woo_id,
                    })

    def _woo_get_export_price(self, product):
        self.ensure_one()
        if not product:
            return 0.0
        if self.pricelist_id and product._name == 'product.product':
            return self.pricelist_id._get_product_price(product, 1.0)
        if product._name == 'product.product':
            return product.lst_price
        return product.list_price
