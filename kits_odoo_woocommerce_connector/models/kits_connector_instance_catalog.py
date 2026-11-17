# -*- coding: utf-8 -*-
"""Dedicated WooCommerce catalog sync: categories (hierarchy), tags, attributes.

Category and tag synchronization uses connector-side kits.woo.category / kits.woo.tag
as the sole source of truth. Native product.category / product.tag are not created,
updated, or bound by these workflows.
"""

import logging

from odoo import fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class KitsConnectorInstanceCatalog(models.Model):
    _inherit = 'kits.woo.instance'

    woo_last_catalog_sync = fields.Datetime(
        string='Catalog', readonly=True, copy=False,
    )

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
    # Categories (hierarchical) — connector-side only
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
            records.sort(key=lambda r: (r.get('parent') or 0, r.get('id') or 0))
            category_iter = records
        else:
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

    def _woo_resolve_local_category(self, *, woo_id, slug=False):
        """Find local category by external id, else pending slug (never steal another ID)."""
        from odoo.addons.kits_odoo_woocommerce_connector.models.kits_woo_catalog import (
            _kits_norm_external_id,
        )
        Category = self.env['kits.woo.category']
        ext = _kits_norm_external_id(woo_id)
        if not ext:
            return Category.browse()
        by_id = Category._find(self, external_id=ext)
        if by_id:
            return by_id
        if not slug:
            return Category.browse()
        by_slug = Category._find(self, slug=slug)
        if not by_slug:
            return Category.browse()
        slug_ext = _kits_norm_external_id(by_slug.external_id)
        # Only link when the slug row is still pending (or already this remote id).
        if not slug_ext or slug_ext == ext:
            return by_slug
        return Category.browse()

    def _woo_import_one_category(self, woo_cat):
        """Upsert store-specific kits.woo.category only (no native product.category)."""
        self.ensure_one()
        from odoo.addons.kits_odoo_woocommerce_connector.models.kits_woo_catalog import (
            _kits_norm_external_id,
        )
        woo_id = _kits_norm_external_id(woo_cat.get('id'))
        name = (woo_cat.get('name') or '').strip()
        if not woo_id:
            return 'skipped'
        if not name:
            raise UserError(self.env._(
                'WooCommerce category %(id)s is missing a required name.',
                id=woo_id,
            ))

        parent_woo = self.env['kits.woo.category']
        has_parent_key = 'parent' in (woo_cat or {})
        parent_woo_id = woo_cat.get('parent') or 0
        if parent_woo_id:
            parent_woo = self.env['kits.woo.category']._find(self, external_id=parent_woo_id)
            if not parent_woo:
                try:
                    remote_parent = self._woo_api().get_category(int(parent_woo_id))
                    if remote_parent.get('id'):
                        self._woo_import_one_category(remote_parent)
                        parent_woo = self.env['kits.woo.category']._find(
                            self, external_id=parent_woo_id,
                        )
                except Exception:  # noqa: BLE001
                    parent_woo = self.env['kits.woo.category']

        image = woo_cat.get('image') or {}
        image_url = image.get('src') if isinstance(image, dict) else False
        display = (woo_cat.get('display') or 'default').lower()
        if display not in ('default', 'products', 'subcategories', 'both'):
            display = 'default'

        slug = (woo_cat.get('slug') or '').strip() or False
        WooCategory = self.env['kits.woo.category'].with_context(kits_woo_skip_catalog_dirty=True)
        woo_rec = self._woo_resolve_local_category(woo_id=woo_id, slug=slug)
        woo_vals = {
            'instance_id': self.id,
            'company_id': self.company_id.id,
            'external_id': woo_id,
            'name': name,
            'slug': slug,
            'description': woo_cat.get('description') or False,
            'display_type': display,
            'image_url': image_url or False,
            'active': True,
            'exists_in_store': True,
            'sync_state': 'synced',
            'sync_error': False,
            'sync_date': fields.Datetime.now(),
        }
        # Incomplete product payloads often omit parent — do not clear hierarchy.
        if has_parent_key or parent_woo:
            woo_vals['parent_id'] = parent_woo.id if parent_woo else False
        if woo_rec:
            self._woo_write_catalog_vals(woo_rec, woo_vals)
            return 'updated'
        try:
            WooCategory.create(woo_vals)
        except ValidationError:
            # Concurrent/idempotent: another row already owns this external id.
            existing = WooCategory._find(self, external_id=woo_id)
            if not existing:
                raise
            self._woo_write_catalog_vals(existing, woo_vals)
            return 'updated'
        return 'created'

    def _woo_write_catalog_vals(self, record, vals):
        """Write import vals without dirty-marking; drop slug/name on unique clash."""
        write_vals = dict(vals)
        target = record.with_context(kits_woo_skip_catalog_dirty=True)
        try:
            target.write(write_vals)
            return
        except ValidationError:
            write_vals.pop('slug', None)
            try:
                target.write(write_vals)
                return
            except ValidationError:
                write_vals.pop('name', None)
                target.write(write_vals)

    def _woo_export_categories(self, job=None):
        """Export connector-side categories for this store (wizard / scheduled)."""
        self.ensure_one()
        payload = (job.payload if job else None) or {}
        category_ids = self._woo_parse_id_list(payload.get('category_ids'))
        mode = payload.get('category_mode') or False
        Category = self.env['kits.woo.category']
        if category_ids:
            categories = Category.browse(category_ids).exists().filtered(
                lambda c: c.instance_id == self
            )
        else:
            categories = Category.search([('instance_id', '=', self.id)])
            from odoo.addons.kits_odoo_woocommerce_connector.models.kits_woo_catalog import (
                _kits_norm_external_id,
            )
            if mode == 'export':
                categories = categories.filtered(
                    lambda c: not _kits_norm_external_id(c.external_id)
                )
            elif mode == 'update':
                categories = categories.filtered(
                    lambda c: _kits_norm_external_id(c.external_id)
                    and c.sync_state in ('not_synced', 'error')
                )
        return self._woo_export_selected_categories(categories, job=job, mode=mode)

    def _woo_export_selected_categories(self, categories, job=None, mode=None):
        self.ensure_one()
        api = self._woo_api()
        categories = categories.exists().filtered(lambda c: c.instance_id == self)
        # Parent-first: expand missing parents, then sort by parent_path
        ordered = self._woo_category_export_order(categories)
        created = updated = skipped = failed = 0
        errors = []
        exported_ids = set()
        for category in ordered:
            if category.id in exported_ids:
                continue
            try:
                result = self._woo_export_one_woo_category(
                    category, api, mode=mode, exported_ids=exported_ids,
                )
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f'{category.display_name}: {exc}')
                category.with_context(kits_woo_skip_catalog_dirty=True)._kits_mark_error(str(exc))
                self._kits_log_operation(
                    operation='woo_export_categories',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f'kits.woo.category,{category.id}',
                    job=job,
                )
        summary = (
            f'Category export finished: created={created}, updated={updated}, '
            f'skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_category_export_order(self, categories):
        """Include ancestors and sort parents before children; dedupe."""
        Category = self.env['kits.woo.category']
        needed = Category.browse()
        for category in categories:
            current = category
            while current:
                if current.instance_id != self:
                    raise UserError(self.env._(
                        'Category "%(name)s" belongs to another WooCommerce store.',
                        name=current.display_name,
                    ))
                needed |= current
                current = current.parent_id
        return needed.sorted(key=lambda c: (c.parent_path or '', c.id))

    def _woo_export_one_woo_category(self, category, api=None, mode=None, exported_ids=None):
        self.ensure_one()
        api = api or self._woo_api()
        exported_ids = exported_ids if exported_ids is not None else set()
        category.ensure_one()
        if category.instance_id != self:
            raise UserError(self.env._(
                'Category "%(name)s" belongs to another WooCommerce store.',
                name=category.display_name,
            ))
        if not category.name:
            raise UserError(self.env._('Category name is required before export.'))

        # Parents first
        if category.parent_id and category.parent_id.id not in exported_ids:
            if not (category.parent_id.external_id or '').strip() or (
                category.parent_id.sync_state in ('not_synced', 'error')
            ):
                parent_result = self._woo_export_one_woo_category(
                    category.parent_id, api, mode=None, exported_ids=exported_ids,
                )
                if parent_result == 'skipped' and not (
                    category.parent_id.external_id or ''
                ).strip():
                    raise UserError(self.env._(
                        'Cannot export "%(child)s" because parent "%(parent)s" '
                        'could not be exported.',
                        child=category.display_name,
                        parent=category.parent_id.display_name,
                    ))
            exported_ids.add(category.parent_id.id)

        linked = bool((category.external_id or '').strip())
        if mode == 'export' and linked and category.sync_state == 'synced':
            exported_ids.add(category.id)
            return 'skipped'
        if mode == 'update' and not linked:
            exported_ids.add(category.id)
            return 'skipped'
        if linked and category.sync_state == 'synced' and mode is None:
            # Selected export of unchanged exported records: skip
            if category.id not in (exported_ids or set()) and mode is None:
                # Still allow explicit selected export of synced? Spec: unchanged should not be
                # unnecessarily updated. Skip.
                exported_ids.add(category.id)
                return 'skipped'

        parent_woo_id = 0
        if category.parent_id:
            parent_ext = (category.parent_id.external_id or '').strip()
            if not parent_ext:
                raise UserError(self.env._(
                    'Cannot export "%(child)s": parent "%(parent)s" has no WooCommerce ID.',
                    child=category.display_name,
                    parent=category.parent_id.display_name,
                ))
            parent_woo_id = int(parent_ext)

        payload = {
            'name': category.name,
            'parent': parent_woo_id,
        }
        if category.slug:
            payload['slug'] = category.slug
        if category.display_type:
            payload['display'] = category.display_type
        if category.description:
            payload['description'] = category.description

        try:
            if linked:
                if mode == 'export':
                    exported_ids.add(category.id)
                    return 'skipped'
                remote = api.update_category(int(category.external_id), payload)
                new_id = remote.get('id') or category.external_id
                category.with_context(kits_woo_skip_catalog_dirty=True).write({
                    'external_id': str(new_id),
                    'sync_state': 'synced',
                    'sync_error': False,
                    'exists_in_store': True,
                    'active': True,
                    'sync_date': fields.Datetime.now(),
                })
                exported_ids.add(category.id)
                return 'updated'
            if mode == 'update':
                exported_ids.add(category.id)
                return 'skipped'
            remote = api.create_category(payload)
            if not remote.get('id'):
                raise UserError(self.env._('WooCommerce did not return a category id.'))
            category.with_context(kits_woo_skip_catalog_dirty=True).write({
                'external_id': str(remote['id']),
                'sync_state': 'synced',
                'sync_error': False,
                'exists_in_store': True,
                'active': True,
                'sync_date': fields.Datetime.now(),
                'slug': remote.get('slug') or category.slug,
            })
            exported_ids.add(category.id)
            return 'created'
        except Exception:
            raise

    # -------------------------------------------------------------------------
    # Tags — connector-side only
    # -------------------------------------------------------------------------

    def _woo_import_tags(self, job=None):
        self.ensure_one()
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

    def _woo_resolve_local_tag(self, *, woo_id, slug=False, name=False):
        """Find local tag by external id, else pending slug/name (never steal another ID)."""
        from odoo.addons.kits_odoo_woocommerce_connector.models.kits_woo_catalog import (
            _kits_norm_external_id,
        )
        Tag = self.env['kits.woo.tag']
        ext = _kits_norm_external_id(woo_id)
        if not ext:
            return Tag.browse()
        by_id = Tag._find(self, external_id=ext)
        if by_id:
            return by_id
        candidate = Tag.browse()
        if slug:
            candidate = Tag._find(self, slug=slug)
        if not candidate and name:
            candidate = Tag._find(self, name=name)
        if not candidate:
            return Tag.browse()
        cand_ext = _kits_norm_external_id(candidate.external_id)
        if not cand_ext or cand_ext == ext:
            return candidate
        return Tag.browse()

    def _woo_import_one_tag(self, woo_tag):
        """Upsert store-specific kits.woo.tag only (no native product.tag)."""
        self.ensure_one()
        from odoo.addons.kits_odoo_woocommerce_connector.models.kits_woo_catalog import (
            _kits_norm_external_id,
        )
        woo_id = _kits_norm_external_id(woo_tag.get('id'))
        name = (woo_tag.get('name') or '').strip()
        if not woo_id or not name:
            if woo_id and not name:
                raise UserError(self.env._(
                    'WooCommerce tag %(id)s is missing a required name.', id=woo_id,
                ))
            return 'skipped'

        slug = (woo_tag.get('slug') or '').strip() or False
        WooTag = self.env['kits.woo.tag'].with_context(kits_woo_skip_catalog_dirty=True)
        woo_rec = self._woo_resolve_local_tag(woo_id=woo_id, slug=slug, name=name)
        vals = {
            'instance_id': self.id,
            'company_id': self.company_id.id,
            'external_id': woo_id,
            'name': name,
            'slug': slug,
            'description': woo_tag.get('description') or False,
            'active': True,
            'exists_in_store': True,
            'sync_state': 'synced',
            'sync_error': False,
            'sync_date': fields.Datetime.now(),
        }
        if woo_rec:
            self._woo_write_catalog_vals(woo_rec, vals)
            return 'updated'
        try:
            WooTag.create(vals)
        except ValidationError:
            existing = WooTag._find(self, external_id=woo_id)
            if not existing:
                raise
            self._woo_write_catalog_vals(existing, vals)
            return 'updated'
        return 'created'

    def _woo_export_tags(self, job=None):
        self.ensure_one()
        payload = (job.payload if job else None) or {}
        mode = payload.get('tag_mode') or 'export'
        tag_ids = self._woo_parse_id_list(payload.get('tag_ids'))
        Tag = self.env['kits.woo.tag']
        if tag_ids:
            tags = Tag.browse(tag_ids).exists().filtered(lambda t: t.instance_id == self)
        else:
            tags = Tag.search([('instance_id', '=', self.id)])
            from odoo.addons.kits_odoo_woocommerce_connector.models.kits_woo_catalog import (
                _kits_norm_external_id,
            )
            if mode == 'export':
                tags = tags.filtered(
                    lambda t: not _kits_norm_external_id(t.external_id)
                )
            elif mode == 'update':
                tags = tags.filtered(
                    lambda t: _kits_norm_external_id(t.external_id)
                    and t.sync_state in ('not_synced', 'error')
                )
        return self._woo_export_selected_tags(tags, job=job, mode=mode)

    def _woo_export_selected_tags(self, tags, job=None, mode=None):
        self.ensure_one()
        api = self._woo_api()
        tags = tags.exists().filtered(lambda t: t.instance_id == self)
        created = updated = skipped = failed = 0
        errors = []
        for tag in tags:
            try:
                result = self._woo_export_one_woo_tag(tag, api, mode=mode)
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f'{tag.display_name}: {exc}')
                tag.with_context(kits_woo_skip_catalog_dirty=True)._kits_mark_error(str(exc))
                self._kits_log_operation(
                    operation='woo_export_tags',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f'kits.woo.tag,{tag.id}',
                    job=job,
                )
        summary = (
            f'Tag export finished: created={created}, updated={updated}, '
            f'skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_export_one_woo_tag(self, tag, api=None, mode=None):
        self.ensure_one()
        api = api or self._woo_api()
        tag.ensure_one()
        if tag.instance_id != self:
            raise UserError(self.env._(
                'Tag "%(name)s" belongs to another WooCommerce store.',
                name=tag.display_name,
            ))
        name = (tag.name or '').strip()
        if not name:
            raise UserError(self.env._('Tag name is required before export.'))
        linked = bool((tag.external_id or '').strip())
        if mode == 'export' and linked:
            return 'skipped'
        if mode == 'update' and not linked:
            return 'skipped'
        if linked and tag.sync_state == 'synced' and mode is None:
            return 'skipped'

        payload = {'name': name}
        if tag.slug:
            payload['slug'] = tag.slug
        if tag.description:
            payload['description'] = tag.description

        if linked:
            remote = api.update_tag(int(tag.external_id), payload)
            tag.with_context(kits_woo_skip_catalog_dirty=True).write({
                'external_id': str(remote.get('id') or tag.external_id),
                'sync_state': 'synced',
                'sync_error': False,
                'exists_in_store': True,
                'active': True,
                'sync_date': fields.Datetime.now(),
            })
            return 'updated'

        matches = [
            row for row in api.get_tags(search=name, per_page=20)
            if (row.get('name') or '').strip().casefold() == name.casefold()
        ]
        if len(matches) > 1:
            raise UserError(self.env._(
                'WooCommerce has more than one tag named "%(name)s".',
                name=name,
            ))
        if len(matches) == 1:
            remote_id = matches[0]['id']
            taken = self.env['kits.woo.tag']._find(self, external_id=remote_id)
            if taken and taken != tag:
                raise UserError(self.env._(
                    'WooCommerce tag %(id)s is already linked to %(other)s.',
                    id=remote_id,
                    other=taken.display_name,
                ))
            tag.with_context(kits_woo_skip_catalog_dirty=True).write({
                'external_id': str(remote_id),
                'sync_state': 'synced',
                'sync_error': False,
                'exists_in_store': True,
                'slug': matches[0].get('slug') or tag.slug,
                'sync_date': fields.Datetime.now(),
            })
            return 'updated'
        remote = api.create_tag(payload)
        if not remote.get('id'):
            raise UserError(self.env._('WooCommerce did not return a tag id.'))
        tag.with_context(kits_woo_skip_catalog_dirty=True).write({
            'external_id': str(remote['id']),
            'sync_state': 'synced',
            'sync_error': False,
            'exists_in_store': True,
            'slug': remote.get('slug') or tag.slug,
            'sync_date': fields.Datetime.now(),
        })
        return 'created'

    # -------------------------------------------------------------------------
    # Attributes + terms (unchanged native attribute linking for variants)
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
                result, term_errors = self._woo_import_one_attribute(woo_attr, job=job)
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
                else:
                    skipped += 1
                if term_errors:
                    failed += len(term_errors)
                    errors.extend(term_errors)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f"Attribute {woo_attr.get('id')}: {exc}")
                _logger.exception(
                    'Woo attribute import failed for %s on instance %s',
                    woo_attr.get('id'), self.id,
                )
                self._kits_log_operation(
                    operation='woo_import_attributes',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f"product.attribute,{woo_attr.get('id')}",
                    job=job,
                )
        self.woo_last_catalog_sync = fields.Datetime.now()
        summary = (
            f'Attribute import finished: created={created}, updated={updated}, '
            f'skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_import_one_attribute(self, woo_attr, job=None):
        self.ensure_one()
        woo_id = woo_attr.get('id')
        name = (woo_attr.get('name') or '').strip()
        if not woo_id:
            return 'skipped', []
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

        order_by = (woo_attr.get('order_by') or 'menu_order').lower()
        if order_by not in ('menu_order', 'name', 'name_num', 'id'):
            order_by = 'menu_order'
        display = (woo_attr.get('type') or 'select').lower()
        if display not in ('select', 'color', 'button', 'image'):
            display = 'select'

        WooAttribute = self.env['kits.woo.attribute']
        woo_attr_rec = WooAttribute._find(self, external_id=woo_id)
        attr_vals = {
            'instance_id': self.id,
            'company_id': self.company_id.id,
            'external_id': str(woo_id),
            'name': name,
            'slug': woo_attr.get('slug') or False,
            'display_type': display,
            'order_by': order_by,
            'has_archives': bool(woo_attr.get('has_archives')),
            'odoo_attribute_id': attribute.id,
            'active': True,
            'exists_in_store': True,
            'sync_state': 'synced',
            'sync_error': False,
            'sync_date': fields.Datetime.now(),
        }
        if woo_attr_rec:
            woo_attr_rec.write(attr_vals)
        else:
            woo_attr_rec = WooAttribute.create(attr_vals)
            if result != 'created':
                result = 'created'

        api = self._woo_api()
        term_errors = []
        for term in api.get_attribute_terms(woo_id):
            try:
                self._woo_import_one_attribute_term(attribute, woo_attr_rec, woo_id, term)
            except Exception as exc:  # noqa: BLE001
                term_id = term.get('id')
                message = f"Attribute {woo_id} term {term_id}: {exc}"
                term_errors.append(message)
                _logger.exception(
                    'Woo attribute term import failed for %s:%s on instance %s',
                    woo_id, term_id, self.id,
                )
                self._kits_log_operation(
                    operation='woo_import_attributes',
                    status='failure',
                    level='error',
                    message=message,
                    details=f'kits.woo.attribute.term,{woo_id}:{term_id}',
                    job=job,
                )
        return result, term_errors

    def _woo_import_one_attribute_term(self, attribute, woo_attribute, woo_attribute_id, term):
        self.ensure_one()
        term_id = term.get('id')
        name = (term.get('name') or '').strip()
        if not term_id or not name:
            return 'skipped'
        external_id = f'{woo_attribute_id}:{term_id}'
        existing = self._woo_find_odoo('product.attribute.value', external_id)
        Pav = self.env['product.attribute.value']
        if existing:
            if existing.name != name:
                existing.name = name
            pav = existing
        else:
            pav = Pav.search([
                ('attribute_id', '=', attribute.id),
                ('name', '=', name),
            ], limit=1)
            if not pav:
                pav = Pav.create({'attribute_id': attribute.id, 'name': name})
        self._woo_bind('product.attribute.value', pav.id, external_id, external_data={
            'attribute_id': woo_attribute_id,
            'term_id': term_id,
            'name': name,
        })

        WooTerm = self.env['kits.woo.attribute.term']
        woo_term = WooTerm._find(
            self, attribute=woo_attribute, external_id=term_id,
        )
        term_vals = {
            'instance_id': self.id,
            'company_id': self.company_id.id,
            'attribute_id': woo_attribute.id,
            'external_id': str(term_id),
            'name': name,
            'slug': term.get('slug') or False,
            'odoo_attribute_value_id': pav.id,
            'active': True,
            'exists_in_store': True,
            'sync_state': 'synced',
            'sync_error': False,
            'sync_date': fields.Datetime.now(),
        }
        if woo_term:
            woo_term.write(term_vals)
            return 'updated'
        WooTerm.create(term_vals)
        return 'created'
