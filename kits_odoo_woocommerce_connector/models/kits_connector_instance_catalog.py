# -*- coding: utf-8 -*-
"""Dedicated WooCommerce catalog sync: categories (hierarchy), tags, attributes."""

import logging

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class KitsConnectorInstanceCatalog(models.Model):
    _inherit = 'kits.connector.instance'

    woo_last_catalog_sync = fields.Datetime(readonly=True, copy=False)

    def action_woo_import_categories(self):
        self._ensure_woo_ready()
        for instance in self:
            instance.action_create_sync_job(
                'woo_import_categories',
                name=self.env._('WooCommerce: Import Categories (%s)', instance.name),
            )
        return self._woo_notify(self.env._('Category import job(s) queued.'))

    def action_woo_export_categories(self):
        self._ensure_woo_ready()
        for instance in self:
            instance.action_create_sync_job(
                'woo_export_categories',
                name=self.env._('WooCommerce: Export Categories (%s)', instance.name),
            )
        return self._woo_notify(self.env._('Category export job(s) queued.'))

    def action_woo_import_tags(self):
        self._ensure_woo_ready()
        for instance in self:
            instance.action_create_sync_job(
                'woo_import_tags',
                name=self.env._('WooCommerce: Import Tags (%s)', instance.name),
            )
        return self._woo_notify(self.env._('Tag import job(s) queued.'))

    def action_woo_import_attributes(self):
        self._ensure_woo_ready()
        for instance in self:
            instance.action_create_sync_job(
                'woo_import_attributes',
                name=self.env._('WooCommerce: Import Attributes (%s)', instance.name),
            )
        return self._woo_notify(self.env._('Attribute import job(s) queued.'))

    # -------------------------------------------------------------------------
    # Categories (hierarchical)
    # -------------------------------------------------------------------------

    def _woo_import_categories(self, job=None):
        self.ensure_one()
        api = self._woo_api()
        payload = (job.payload if job else None) or {}
        woo_ids = self._woo_parse_id_list(payload.get('woo_ids'))
        created = updated = skipped = failed = 0
        errors = []

        if woo_ids:
            records = []
            for cid in woo_ids:
                try:
                    remote = api.get_category(cid)
                    if remote.get('id'):
                        records.append(remote)
                    else:
                        skipped += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    errors.append(f'Category {cid}: {exc}')
            # Parents first
            records.sort(key=lambda r: (r.get('parent') or 0, r.get('id') or 0))
            category_iter = records
        else:
            # Collect all then sort so parents bind before children
            records = list(api.get_categories(orderby='id', order='asc'))
            records.sort(key=lambda r: (r.get('parent') or 0, r.get('id') or 0))
            category_iter = records

        for woo_cat in category_iter:
            try:
                result = self._woo_import_one_category(woo_cat)
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f"Category {woo_cat.get('id')}: {exc}")
                _logger.exception(
                    'Woo category import failed for %s on instance %s',
                    woo_cat.get('id'), self.id,
                )

        self.woo_last_catalog_sync = fields.Datetime.now()
        summary = (
            f'Category import finished: created={created}, updated={updated}, '
            f'skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_import_one_category(self, woo_cat):
        self.ensure_one()
        woo_id = woo_cat.get('id')
        name = (woo_cat.get('name') or '').strip()
        if not woo_id:
            return 'skipped'
        if not name:
            raise UserError(self.env._(
                'WooCommerce category %(id)s is missing a required name.',
                id=woo_id,
            ))

        parent = self.env['product.category']
        parent_woo_id = woo_cat.get('parent') or 0
        if parent_woo_id:
            parent = self._woo_find_odoo('product.category', parent_woo_id)
            if not parent:
                # Fetch parent on demand for ID-filtered imports
                try:
                    remote_parent = self._woo_api().get_category(int(parent_woo_id))
                    if remote_parent.get('id'):
                        self._woo_import_one_category(remote_parent)
                        parent = self._woo_find_odoo('product.category', parent_woo_id)
                except Exception:  # noqa: BLE001
                    parent = self.env['product.category']

        existing = self._woo_find_odoo('product.category', woo_id)
        vals = {'name': name}
        if parent:
            vals['parent_id'] = parent.id
        if existing:
            existing.write(vals)
            self._woo_bind('product.category', existing.id, woo_id, external_data={
                'slug': woo_cat.get('slug'),
                'parent': parent_woo_id,
            })
            return 'updated'

        category = self.env['product.category'].create(vals)
        self._woo_bind('product.category', category.id, woo_id, external_data={
            'slug': woo_cat.get('slug'),
            'parent': parent_woo_id,
        })
        return 'created'

    def _woo_export_categories(self, job=None):
        self.ensure_one()
        api = self._woo_api()
        payload = (job.payload if job else None) or {}
        category_ids = self._woo_parse_id_list(payload.get('category_ids'))
        if category_ids:
            categories = self.env['product.category'].browse(category_ids).exists()
        else:
            # Export categories already bound for this instance
            bindings = self._woo_binding().search([
                ('instance_id', '=', self.id),
                ('odoo_model', '=', 'product.category'),
                ('active', '=', True),
            ])
            categories = self.env['product.category'].browse(
                bindings.mapped('odoo_record_id')
            ).exists()
            if not categories:
                categories = self.env['product.category'].search([], limit=200)

        # Parents first
        categories = categories.sorted(key=lambda c: (c.parent_path or '', c.id))
        created = updated = skipped = failed = 0
        errors = []
        for category in categories:
            try:
                result = self._woo_export_one_category(category, api)
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f'Category {category.display_name}: {exc}')
        summary = (
            f'Category export finished: created={created}, updated={updated}, '
            f'skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_export_one_category(self, category, api=None):
        self.ensure_one()
        api = api or self._woo_api()
        if not category.name:
            raise UserError(self.env._('Category name is required before export.'))
        parent_woo_id = 0
        if category.parent_id:
            parent_binding = self._woo_binding()._kits_find_binding(
                self, 'product.category', odoo_record_id=category.parent_id.id,
            )
            if parent_binding and parent_binding.sync_state != 'deleted':
                parent_woo_id = int(parent_binding.external_id)
            else:
                # Export parent first
                self._woo_export_one_category(category.parent_id, api)
                parent_binding = self._woo_binding()._kits_find_binding(
                    self, 'product.category', odoo_record_id=category.parent_id.id,
                )
                if parent_binding:
                    parent_woo_id = int(parent_binding.external_id)

        payload = {
            'name': category.name,
            'parent': parent_woo_id,
        }
        binding = self._woo_binding()._kits_find_binding(
            self, 'product.category', odoo_record_id=category.id,
        )
        if binding and binding.sync_state != 'deleted':
            remote = api.update_category(int(binding.external_id), payload)
            self._woo_bind('product.category', category.id, remote.get('id') or binding.external_id)
            return 'updated'
        remote = api.create_category(payload)
        if not remote.get('id'):
            raise UserError(self.env._('WooCommerce did not return a category id.'))
        self._woo_bind('product.category', category.id, remote['id'])
        return 'created'

    # -------------------------------------------------------------------------
    # Tags
    # -------------------------------------------------------------------------

    def _woo_import_tags(self, job=None):
        self.ensure_one()
        if 'product.tag' not in self.env:
            raise UserError(self.env._('product.tag is not available in this database.'))
        api = self._woo_api()
        payload = (job.payload if job else None) or {}
        woo_ids = self._woo_parse_id_list(payload.get('woo_ids'))
        created = updated = skipped = failed = 0
        errors = []
        if woo_ids:
            tag_iter = []
            for tid in woo_ids:
                try:
                    remote = api.get_tag(tid)
                    if remote.get('id'):
                        tag_iter.append(remote)
                    else:
                        skipped += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    errors.append(f'Tag {tid}: {exc}')
        else:
            tag_iter = api.get_tags()

        for woo_tag in tag_iter:
            try:
                result = self._woo_import_one_tag(woo_tag)
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f"Tag {woo_tag.get('id')}: {exc}")
        summary = (
            f'Tag import finished: created={created}, updated={updated}, '
            f'skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_import_one_tag(self, woo_tag):
        self.ensure_one()
        woo_id = woo_tag.get('id')
        name = (woo_tag.get('name') or '').strip()
        if not woo_id or not name:
            if woo_id and not name:
                raise UserError(self.env._(
                    'WooCommerce tag %(id)s is missing a required name.', id=woo_id,
                ))
            return 'skipped'
        existing = self._woo_find_odoo('product.tag', woo_id)
        Tag = self.env['product.tag']
        if existing:
            existing.write({'name': name})
            self._woo_bind('product.tag', existing.id, woo_id)
            return 'updated'
        tag = Tag.search([('name', '=', name)], limit=1) or Tag.create({'name': name})
        self._woo_bind('product.tag', tag.id, woo_id)
        return 'created' if tag.create_date == tag.write_date else 'updated'

    # -------------------------------------------------------------------------
    # Attributes + terms (with bindings)
    # -------------------------------------------------------------------------

    def _woo_import_attributes(self, job=None):
        self.ensure_one()
        api = self._woo_api()
        payload = (job.payload if job else None) or {}
        woo_ids = self._woo_parse_id_list(payload.get('woo_ids'))
        created = updated = skipped = failed = 0
        errors = []
        if woo_ids:
            attr_iter = []
            for aid in woo_ids:
                try:
                    remote = api.get_attribute(aid)
                    if remote.get('id'):
                        attr_iter.append(remote)
                    else:
                        skipped += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    errors.append(f'Attribute {aid}: {exc}')
        else:
            attr_iter = api.get_attributes()

        for woo_attr in attr_iter:
            try:
                result = self._woo_import_one_attribute(woo_attr)
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f"Attribute {woo_attr.get('id')}: {exc}")
        self.woo_last_catalog_sync = fields.Datetime.now()
        summary = (
            f'Attribute import finished: created={created}, updated={updated}, '
            f'skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_import_one_attribute(self, woo_attr):
        self.ensure_one()
        woo_id = woo_attr.get('id')
        name = (woo_attr.get('name') or '').strip()
        if not woo_id:
            return 'skipped'
        if not name:
            raise UserError(self.env._(
                'WooCommerce attribute %(id)s is missing a required name.', id=woo_id,
            ))
        Attribute = self.env['product.attribute']
        existing = self._woo_find_odoo('product.attribute', woo_id)
        result = 'updated'
        if existing:
            attribute = existing
            if attribute.name != name:
                attribute.name = name
        else:
            attribute = Attribute.search([('name', '=', name)], limit=1)
            if not attribute:
                attribute = Attribute.create({
                    'name': name,
                    'create_variant': 'always',
                })
                result = 'created'
            else:
                result = 'updated'
        self._woo_bind('product.attribute', attribute.id, woo_id, external_data={
            'slug': woo_attr.get('slug'),
        })

        # Terms
        api = self._woo_api()
        for term in api.get_attribute_terms(woo_id):
            self._woo_import_one_attribute_term(attribute, woo_id, term)
        return result

    def _woo_import_one_attribute_term(self, attribute, woo_attribute_id, term):
        self.ensure_one()
        term_id = term.get('id')
        name = (term.get('name') or '').strip()
        if not term_id or not name:
            return 'skipped'
        # Bind attribute values with composite external id attr:term
        external_id = f'{woo_attribute_id}:{term_id}'
        existing = self._woo_find_odoo('product.attribute.value', external_id)
        Pav = self.env['product.attribute.value']
        if existing:
            if existing.name != name:
                existing.name = name
            self._woo_bind('product.attribute.value', existing.id, external_id)
            return 'updated'
        pav = Pav.search([
            ('attribute_id', '=', attribute.id),
            ('name', '=', name),
        ], limit=1)
        if not pav:
            pav = Pav.create({'attribute_id': attribute.id, 'name': name})
        self._woo_bind('product.attribute.value', pav.id, external_id, external_data={
            'attribute_id': woo_attribute_id,
            'term_id': term_id,
        })
        return 'created'
