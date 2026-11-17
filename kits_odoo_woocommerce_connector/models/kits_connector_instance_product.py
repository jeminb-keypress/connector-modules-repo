# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tools import html2plaintext

from odoo.addons.kits_odoo_base_connector.services.exceptions import ConnectorAPIError
from odoo.addons.kits_odoo_woocommerce_connector.models.kits_woo_product_waiting import (
    WAITING_PROCESS_BATCH,
)

_logger = logging.getLogger(__name__)

# Woo status → Odoo active
_WOO_INACTIVE_STATUSES = frozenset({'draft', 'pending', 'private', 'trash'})
_WOO_SUPPORTED_PRODUCT_TYPES = frozenset({'simple', 'variable'})
_WOO_WEIGHT_UOM_XMLIDS = {
    'kg': 'uom.product_uom_kgm',
    'g': 'uom.product_uom_gram',
    'lbs': 'uom.product_uom_lb',
    'oz': 'uom.product_uom_oz',
}


class KitsConnectorInstanceProduct(models.Model):
    _inherit = 'kits.woo.instance'

    # =========================================================================
    # Product import
    # =========================================================================

    def _woo_sync_catalog_prerequisites(self, job=None):
        """Synchronize categories, tags, attributes before product import."""
        self.ensure_one()
        parts = []
        for method_name, label in (
            ('_woo_import_categories', 'categories'),
            ('_woo_import_tags', 'tags'),
            ('_woo_import_attributes', 'attributes'),
        ):
            method = getattr(self, method_name, None)
            if not method:
                continue
            try:
                # Never reuse the product job payload (woo_ids are product ids).
                parts.append(f'{label}: {method(job=None)}')
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    'Catalog prerequisite %s failed for Woo store %s', label, self.id,
                )
                self._kits_log_operation(
                    operation='woo_import_products',
                    status='failure',
                    level='warning',
                    message=self.env._(
                        'Catalog sync (%(label)s) failed before product import: %(error)s',
                        label=label,
                        error=exc,
                    ),
                    job=job,
                )
                raise UserError(self.env._(
                    'Cannot import products until %(label)s are synchronized: %(error)s',
                    label=label,
                    error=exc,
                )) from exc
        return '; '.join(parts)

    def _woo_prepare_product_payload_for_queue(self, woo_product, api):
        """Attach variations for variable products. Raises if variation fetch fails."""
        self.ensure_one()
        product = dict(woo_product or {})
        woo_id = product.get('id')
        ptype = (product.get('type') or 'simple').lower()
        if ptype == 'variable':
            attributes = [
                attr for attr in (product.get('attributes') or [])
                if attr.get('variation')
            ]
            options = [
                option
                for attr in attributes
                for option in (attr.get('options') or [])
                if option
            ]
            if not attributes or not options:
                raise UserError(self.env._(
                    'WooCommerce variable product %(id)s has no option values. '
                    'Import was not applied.',
                    id=woo_id,
                ))
            try:
                variations = list(api.get_variations(woo_id))
            except Exception as exc:  # noqa: BLE001
                raise UserError(self.env._(
                    'Failed to fetch variations for WooCommerce product %(id)s: %(error)s',
                    id=woo_id,
                    error=exc,
                )) from exc
            if not variations:
                raise UserError(self.env._(
                    'WooCommerce variable product %(id)s has no variations. Import was not applied.',
                    id=woo_id,
                ))
            product['variations'] = variations
        return product

    def _woo_import_products(self, job=None):
        """Fetch products, enqueue the product waiting list, then process a batch.

        Job payload keys (all optional):
        - ``woo_ids``: list of Woo product IDs (Specific Id Filter)
        - ``after`` / ``modified_after``: ISO datetime string for modified-after filter
        - ``skip_existing`` / ``product_mode``
        - ``advance_checkpoint``: when True, update ``woo_last_product_import`` after queue
        - ``source``: waiting-list source (bulk_import|specific|scheduled)
        """
        self.ensure_one()
        api = self._woo_api()
        payload = (job.payload if job else None) or {}
        woo_ids = self._woo_parse_id_list(payload.get('woo_ids'))
        modified_after = payload.get('modified_after') or payload.get('after')
        source = payload.get('source') or ('specific' if woo_ids else 'bulk_import')
        advance_checkpoint = bool(payload.get('advance_checkpoint'))
        fetch_started = fields.Datetime.now()

        self._woo_sync_catalog_prerequisites(job=job)

        queued = skipped = failed = 0
        errors = []
        queued_rows = self.env['kits.woo.product.waiting']
        Waiting = self.env['kits.woo.product.waiting']

        if woo_ids:
            products_iter = self._woo_iter_products_by_ids(api, woo_ids)
        else:
            params = {}
            if modified_after:
                params['modified_after'] = str(modified_after).replace(' ', 'T')
            products_iter = api.get_products(**params)

        for woo_product in products_iter:
            if self._woo_skip_product_import(woo_product, payload):
                skipped += 1
                continue
            woo_id = woo_product.get('id') if isinstance(woo_product, dict) else False
            try:
                prepared = self._woo_prepare_product_payload_for_queue(woo_product, api)
                row = Waiting._enqueue(
                    self,
                    external_id=prepared.get('id'),
                    payload=prepared,
                    source=source,
                    event_type='update',
                    trigger_batch=False,
                    skip_existing=bool(payload.get('skip_existing')),
                    product_mode=payload.get('product_mode') or False,
                    sync_images_now=False,
                )
                queued_rows |= row
                queued += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f'Product {woo_id}: {exc}')
                _logger.exception(
                    'WooCommerce product queue failed for %s on instance %s',
                    woo_id, self.id,
                )
                self._kits_log_operation(
                    operation='woo_import_products',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f'woo_product_id={woo_id}',
                    job=job,
                )

        if advance_checkpoint and failed == 0:
            self.woo_last_product_import = fetch_started

        # Process only rows from this fetch so leftover failed retries stay on cron.
        process_summary = queued_rows[:WAITING_PROCESS_BATCH]._process_entries(job=job)
        queued_rows.invalidate_recordset()
        for row in queued_rows:
            if row.state == 'failed' and row.last_error:
                failed += 1
                errors.append(f'Product {row.external_id}: {row.last_error}')
        remaining = Waiting.sudo().search_count([
            ('instance_id', '=', self.id),
            '|',
            ('state', '=', 'draft'),
            '&',
            ('state', '=', 'failed'),
            ('attempt_count', '<', 3),
        ])
        if remaining:
            Waiting._enqueue_process_job(self, reason='continue')
        self.woo_last_product_sync = fields.Datetime.now()
        summary = (
            f'Products import finished: queued={queued}, skipped={skipped}, '
            f'failed={failed}; {process_summary}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    @api.model
    def _cron_woo_auto_import_products(self):
        """Daily: enqueue incremental product import for stores with auto import on."""
        instances = self.search([
            ('platform_type', '=', 'woocommerce'),
            ('active', '=', True),
            ('woo_auto_import_products', '=', True),
        ])
        for instance in instances:
            pending = self.env['kits.connector.sync.job'].search_count([
                ('instance_id', '=', instance._kits_instance_ref()),
                ('job_type', '=', 'woo_auto_import_products'),
                ('state', 'in', ('pending', 'running')),
            ])
            if pending:
                continue
            instance.action_create_sync_job(
                'woo_auto_import_products',
                name=self.env._(
                    'WooCommerce: Automatic Product Import (%s)', instance.name,
                ),
                payload={},
                priority=12,
            )
        return True

    def _woo_auto_import_products(self, job=None):
        """Incremental product import using the last successful import checkpoint."""
        self.ensure_one()
        checkpoint = self.woo_last_product_import
        if not checkpoint:
            checkpoint = fields.Datetime.now() - timedelta(days=30)
        if job is None:
            class _Job:
                id = False
                payload = {}
            job = _Job()
        payload = dict(job.payload or {})
        payload.update({
            'modified_after': fields.Datetime.to_string(checkpoint),
            'product_mode': 'new_and_updated',
            'advance_checkpoint': True,
            'source': 'scheduled',
        })
        job.payload = payload
        return self._woo_import_products(job=job)

    def _woo_iter_products_by_ids(self, api, woo_ids):
        for woo_id in woo_ids:
            try:
                remote = api.get_product(woo_id)
            except Exception as exc:  # noqa: BLE001
                if self._woo_is_not_found(exc):
                    self._woo_mark_product_deleted_remote(woo_id, message=str(exc))
                else:
                    binding = self._woo_record_binding(
                        'product.template', external_id=str(woo_id),
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
                self._woo_mark_product_deleted_remote(
                    woo_id,
                    message=self.env._(
                        'WooCommerce product %(id)s is missing or inaccessible.',
                        id=woo_id,
                    ),
                )
                continue
            yield remote

    def _woo_skip_product_import(self, woo_product, payload):
        """Honor Import Products modes without changing the match rules."""
        self.ensure_one()
        woo_id = woo_product.get('id') if isinstance(woo_product, dict) else False
        if not woo_id:
            return False
        binding = self.env['kits.woo.product.binding']._find(self, external_id=str(woo_id))
        if not binding:
            return False
        if payload.get('skip_existing'):
            return True
        if payload.get('product_mode') == 'new_and_updated':
            remote = self._woo_parse_datetime(
                woo_product.get('date_modified_gmt') or woo_product.get('date_modified')
            )
            if binding.store_write_date and remote and remote <= binding.store_write_date:
                return True
        return False

    def _woo_import_one_product(self, woo_product, job=None, waiting=None, sync_images=None):
        """Match or create an Odoo product, then store Woo-only details on bindings.

        Validation runs before any product or binding write. A failure rolls back
        with the caller's savepoint.

        ``sync_images``:
        - True: download images immediately (order import)
        - False: skip images here (waiting list defers them)
        - None: immediate when not from waiting / waiting.sync_images_now; else defer
        """
        self.ensure_one()
        woo_id = woo_product.get('id')
        if not woo_id:
            raise UserError(self.env._(
                'Skipped a WooCommerce product with no id on %(store)s.',
                store=self.display_name,
            ))

        ptype = (woo_product.get('type') or 'simple').lower()
        if ptype not in _WOO_SUPPORTED_PRODUCT_TYPES:
            raise UserError(self.env._(
                'Unsupported WooCommerce product type "%(type)s" for product %(id)s. '
                'Only simple and variable products are imported.',
                type=ptype,
                id=woo_id,
            ))

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

        rows = self._woo_validate_import_variants(woo_product)
        matched_template = self.env['product.template']
        resolved = []
        for row in rows:
            product, how = self._woo_match_import_variant(row)
            if product:
                matched_template |= product.product_tmpl_id
            resolved.append((row, product, how))
        if len(matched_template) > 1:
            raise UserError(self.env._(
                'WooCommerce product %(id)s on %(store)s matches more than one Odoo product. '
                'Nothing was linked.',
                id=woo_id,
                store=self.display_name,
            ))

        created = False
        write_ctx = {'kits_woo_skip_dirty': True}
        if matched_template:
            template = matched_template
            for row, product, how in resolved:
                if product and product.product_tmpl_id != template:
                    raise UserError(self.env._(
                        'Variation %(variant)s of WooCommerce product %(id)s does not belong to product %(product)s.',
                        variant=row['external_id'],
                        id=woo_id,
                        product=template.display_name,
                    ))
                if not product:
                    on_template = template.product_variant_ids.filtered(
                        lambda variant, sku=row['sku']: (variant.default_code or '') == sku
                    )
                    if len(on_template) != 1:
                        raise UserError(self.env._(
                            'SKU %(sku)s from WooCommerce product %(id)s is not on %(product)s. '
                            'Import stopped before creating a partial link.',
                            sku=row['sku'],
                            id=woo_id,
                            product=template.display_name,
                        ))
            self._woo_apply_allowed_product_updates(template, woo_product)
        elif not self.woo_create_products:
            skus = ', '.join(row['sku'] for row in rows) or self.env._('(missing)')
            raise UserError(self.env._(
                'No Odoo product matches SKU %(sku)s for WooCommerce product %(id)s on %(store)s. '
                'Automatically Create Products is turned off, so nothing was created.',
                sku=skus,
                id=woo_id,
                store=self.display_name,
            ))
        else:
            template = self._woo_create_product_from_remote(woo_product, rows)
            created = True
            if ptype == 'variable':
                self._woo_import_variable_product(template, woo_product)
                self._woo_assign_created_variant_skus(template, woo_product, rows)

        categories = self._woo_category_records(woo_product.get('categories') or [])
        tag_ids = self._woo_ensure_tags(woo_product.get('tags') or []) or []
        product_binding = self._woo_upsert_product_binding(
            template, woo_product, categories, tag_ids, len(rows),
        )
        if self not in template.woo_instance_ids:
            template.with_context(**write_ctx).write({'woo_instance_ids': [(4, self.id)]})

        linked_rows = []
        for row in rows:
            variant = template.product_variant_ids.filtered(
                lambda variant, sku=row['sku']: (variant.default_code or '') == sku
            )[:1]
            if not variant and len(template.product_variant_ids) == 1 and len(rows) == 1:
                variant = template.product_variant_id
                if created and not variant.default_code:
                    variant.with_context(**write_ctx).write({'default_code': row['sku']})
            if not variant:
                if created:
                    template.with_context(**write_ctx).unlink()
                    raise UserError(self.env._(
                        'Could not link any variants for newly created WooCommerce product '
                        '%(id)s on %(store)s. The Odoo product was removed.',
                        id=woo_id,
                        store=self.display_name,
                    ))
                raise UserError(self.env._(
                    'Could not link SKU %(sku)s for WooCommerce product %(id)s on %(store)s.',
                    sku=row['sku'],
                    id=woo_id,
                    store=self.display_name,
                ))
            self._woo_apply_allowed_variant_updates(variant, row, created=created)
            self._woo_upsert_variant_binding(product_binding, variant, row)
            linked_rows.append((row, variant))

        if created and not linked_rows:
            template.with_context(**write_ctx).unlink()
            raise UserError(self.env._(
                'Newly created WooCommerce product %(id)s on %(store)s produced no '
                'matched variants. The Odoo product was removed.',
                id=woo_id,
                store=self.display_name,
            ))

        self._woo_apply_extra_field_mappings(template, woo_product, linked_rows)

        if sync_images is None:
            if waiting is not None:
                sync_images = bool(waiting.sync_images_now)
            else:
                sync_images = True
        if sync_images and self.woo_import_images:
            self._woo_sync_template_images(
                template, woo_product.get('images') or [], binding=product_binding,
            )
            for row, variant in linked_rows:
                image = (row.get('payload') or {}).get('image') or {}
                if not image and len(rows) == 1:
                    continue
                variant_binding = self.env['kits.woo.variant.binding']._find(
                    self, external_id=row['external_id'],
                )
                if variant and image:
                    self._woo_sync_variant_image(variant, image, binding=variant_binding)
        return 'created' if created else 'updated'

    def _woo_validate_import_variants(self, woo_product):
        """Return variant rows or raise before any Odoo product is written."""
        self.ensure_one()
        woo_id = woo_product.get('id')
        ptype = woo_product.get('type') or 'simple'
        if ptype == 'variable':
            attributes = [
                attr for attr in (woo_product.get('attributes') or [])
                if attr.get('variation')
            ]
            options = [
                option for attr in attributes for option in (attr.get('options') or []) if option
            ]
            if not attributes or not options:
                raise UserError(self.env._(
                    'WooCommerce variable product %(id)s has no option values. '
                    'Import was not applied.',
                    id=woo_id,
                ))
            variations = woo_product.get('variations')
            if variations is None:
                variations = list(self._woo_api().get_variations(woo_id))
            if not variations:
                raise UserError(self.env._(
                    'WooCommerce variable product %(id)s has no variations. Import was not applied.',
                    id=woo_id,
                ))
            rows = []
            seen = []
            for variation in variations:
                if not variation.get('id'):
                    raise UserError(self.env._(
                        'WooCommerce product %(id)s has a variation with no id. Import was not applied.',
                        id=woo_id,
                    ))
                sku = (variation.get('sku') or '').strip()
                if not sku:
                    raise UserError(self.env._(
                        'Variation %(variant)s of WooCommerce product %(id)s has no SKU. '
                        'Import was not applied.',
                        variant=variation.get('id'),
                        id=woo_id,
                    ))
                option_values = [
                    (attr.get('option') or '').strip()
                    for attr in (variation.get('attributes') or [])
                ]
                if not any(option_values):
                    raise UserError(self.env._(
                        'Variation %(variant)s of WooCommerce product %(id)s has no option values. '
                        'Import was not applied.',
                        variant=variation.get('id'),
                        id=woo_id,
                    ))
                key = sku.casefold()
                if key in seen:
                    raise UserError(self.env._(
                        'SKU %(sku)s is repeated on WooCommerce product %(id)s. Import was not applied.',
                        sku=sku,
                        id=woo_id,
                    ))
                seen.append(key)
                rows.append({
                    'external_id': str(variation['id']),
                    'sku': sku,
                    'payload': variation,
                    'parent_id': str(woo_id),
                })
            return rows

        sku = (woo_product.get('sku') or '').strip()
        if not sku:
            _logger.info(
                'WooCommerce product %s on store %s has no SKU.',
                woo_id, self.id,
            )
            raise UserError(self.env._(
                'WooCommerce product %(id)s has no SKU. Import was not applied.',
                id=woo_id,
            ))
        return [{
            'external_id': str(woo_id),
            'sku': sku,
            'payload': woo_product,
            'parent_id': False,
        }]

    def _woo_match_import_variant(self, row):
        """Return (product.product, how). Raises when several Odoo SKUs match."""
        self.ensure_one()
        Variant = self.env['kits.woo.variant.binding']
        by_external = Variant._find(self, external_id=row['external_id'])
        if by_external and by_external.product_id:
            return by_external.product_id, 'external_id'
        product_binding = self.env['kits.woo.product.binding']._find(
            self, external_id=row['external_id'],
        )
        if product_binding and product_binding.product_tmpl_id.product_variant_id:
            return product_binding.product_tmpl_id.product_variant_id, 'product_external_id'
        if self.woo_match_by_sku:
            by_sku = Variant._find(self, sku=row['sku'])
            if by_sku and by_sku.product_id:
                return by_sku.product_id, 'store_sku'
            products = self.env['product.product'].search([
                ('default_code', '=', row['sku']),
                ('company_id', 'in', [False, self.company_id.id]),
            ])
            if len(products) > 1:
                self._woo_log_sku_ambiguity(row['sku'], products.mapped('product_tmpl_id'))
                raise UserError(self.env._(
                    'SKU %(sku)s matches %(count)s Odoo variants. '
                    'WooCommerce product import was not applied.',
                    sku=row['sku'],
                    count=len(products),
                ))
            if products:
                return products, 'odoo_sku'
        return self.env['product.product'], 'none'

    def _woo_product_is_virtual(self, woo_product, rows):
        if bool(woo_product.get('virtual')):
            return True
        if (woo_product.get('type') or 'simple') == 'variable' and rows:
            return all(bool((row.get('payload') or {}).get('virtual')) for row in rows)
        return False

    def _woo_use_sales_description(self):
        raw = (
            self.env['ir.config_parameter'].sudo().get_str('kits_woo.use_sales_description')
            or ''
        ).strip().lower()
        return raw in ('1', 'true', 'yes')

    def _woo_create_product_from_remote(self, woo_product, rows):
        self.ensure_one()
        ptype = woo_product.get('type') or 'simple'
        # Woo categories are connector-side only; native categ_id uses store default.
        self._woo_ensure_woo_categories(woo_product.get('categories') or [])
        category = self.woo_default_product_categ_id
        is_virtual = self._woo_product_is_virtual(woo_product, rows)
        long_html = woo_product.get('description') or ''
        short_html = woo_product.get('short_description') or ''
        vals = {
            'name': woo_product.get('name') or f"Woo Product {woo_product.get('id')}",
            'company_id': self.company_id.id,
            'sale_ok': True,
            'active': True,
        }
        if is_virtual:
            vals['type'] = 'service'
            vals['is_storable'] = False
        else:
            vals['type'] = 'consu'
            vals['is_storable'] = True
        if self._woo_use_sales_description():
            vals['description_sale'] = long_html or False
            vals['description'] = short_html or False
        else:
            vals['description'] = long_html or False
            vals['description_sale'] = html2plaintext(short_html or long_html) or False
        if ptype != 'variable' and rows:
            vals['default_code'] = rows[0]['sku']
        if self.woo_sync_price:
            vals['list_price'] = self._woo_remote_price(woo_product)
        if self.woo_sync_weight:
            weight = self._woo_convert_weight(self._woo_remote_weight(woo_product))
            if weight is not None:
                vals['weight'] = weight
        if category:
            vals['categ_id'] = category.id
        template = self.env['product.template'].with_context(kits_woo_skip_dirty=True).create(vals)
        if self.woo_sync_price:
            self._woo_sync_pricelist_prices(template.product_variant_id, woo_product)
        return template

    def _woo_apply_allowed_product_updates(self, template, woo_product):
        vals = {}
        if self.woo_sync_price:
            vals['list_price'] = self._woo_remote_price(woo_product)
        if self.woo_sync_weight:
            weight = self._woo_convert_weight(self._woo_remote_weight(woo_product))
            if weight is not None:
                vals['weight'] = weight
        if vals:
            template.with_context(kits_woo_skip_dirty=True).write(vals)
        if self.woo_sync_price:
            self._woo_sync_pricelist_prices(template.product_variant_id, woo_product)

    def _woo_apply_allowed_variant_updates(self, variant, row, created=False):
        vals = {}
        payload = row.get('payload') or {}
        if created and not variant.default_code:
            vals['default_code'] = row['sku']
        if created:
            cost = self._woo_extract_cost(payload)
            if cost is not None:
                vals['standard_price'] = cost
        if self.woo_sync_price:
            vals['lst_price'] = self._woo_remote_price(payload)
        if self.woo_sync_weight:
            weight = self._woo_convert_weight(self._woo_remote_weight(payload))
            if weight is not None:
                vals['weight'] = weight
        if vals:
            variant.with_context(kits_woo_skip_dirty=True).write(vals)
        if self.woo_sync_price:
            self._woo_sync_pricelist_prices(variant, payload)

    def _woo_assign_created_variant_skus(self, template, woo_product, rows):
        self.ensure_one()
        attr_value_map = {}
        for line in template.attribute_line_ids:
            for ptav in line.product_template_value_ids:
                attr_value_map[(
                    line.attribute_id.name.strip().lower(),
                    ptav.name.strip().lower(),
                )] = ptav
        for row in rows:
            combination = self.env['product.template.attribute.value']
            for attr in (row.get('payload') or {}).get('attributes') or []:
                key = (
                    (attr.get('name') or '').strip().lower(),
                    (attr.get('option') or '').strip().lower(),
                )
                ptav = attr_value_map.get(key)
                if ptav:
                    combination |= ptav
            variant = template._get_variant_for_combination(combination) if combination else self.env['product.product']
            if variant and not variant.default_code:
                variant.with_context(kits_woo_skip_dirty=True).write({'default_code': row['sku']})

    @staticmethod
    def _woo_remote_price(payload):
        try:
            return float(payload.get('regular_price') or payload.get('price') or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _woo_remote_sale_price(payload):
        raw = (payload or {}).get('sale_price')
        if raw in (None, '', False):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _woo_remote_weight(payload):
        raw = (payload or {}).get('weight')
        if raw in (None, '', False):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value < 0:
            return None
        return value

    def _woo_odoo_weight_uom(self):
        """Return the UoM used for product.weight in this database."""
        param = (
            self.env['ir.config_parameter'].sudo().get_str('kits_woo.odoo_weight_unit') or ''
        ).strip().lower()
        if param in ('lbs', 'lb'):
            return self.env.ref('uom.product_uom_lb', raise_if_not_found=False) or self.env['uom.uom']
        if param in ('kg', 'g', 'oz'):
            xmlid = _WOO_WEIGHT_UOM_XMLIDS.get(param)
            return self.env.ref(xmlid, raise_if_not_found=False) or self.env['uom.uom']
        # Fall back to Odoo product setting (0=kg, 1=lbs).
        return self.env['product.template']._get_weight_uom_id_from_ir_config_parameter()

    def _woo_source_weight_uom(self):
        unit = (self.woo_weight_unit or 'kg').lower()
        xmlid = _WOO_WEIGHT_UOM_XMLIDS.get(unit, 'uom.product_uom_kgm')
        return self.env.ref(xmlid, raise_if_not_found=False) or self.env['uom.uom']

    def _woo_convert_weight(self, raw_weight):
        """Convert a WooCommerce weight into the configured Odoo weight unit."""
        if raw_weight in (None, False):
            return None
        try:
            value = float(raw_weight)
        except (TypeError, ValueError):
            return None
        if value == 0.0:
            return 0.0
        source = self._woo_source_weight_uom()
        target = self._woo_odoo_weight_uom()
        if not source or not target or source == target:
            return value
        try:
            return source._compute_quantity(value, target, round=False)
        except Exception:  # noqa: BLE001
            _logger.debug('Weight conversion failed; using raw value', exc_info=True)
            return value

    def _woo_convert_price_to_pricelist(self, amount, pricelist):
        """Convert a Woo/company-currency amount into the pricelist currency."""
        if amount is None:
            return None
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return None
        company_currency = self.company_id.currency_id
        if not pricelist or not pricelist.currency_id or pricelist.currency_id == company_currency:
            return amount
        return company_currency._convert(
            amount,
            pricelist.currency_id,
            self.company_id,
            fields.Date.context_today(self),
        )

    def _woo_upsert_pricelist_item(self, pricelist, product, price, date_start=False, date_end=False):
        self.ensure_one()
        if not pricelist or not product:
            return self.env['product.pricelist.item']
        Item = self.env['product.pricelist.item']
        domain = [
            ('pricelist_id', '=', pricelist.id),
            ('product_id', '=', product.id),
            ('compute_price', '=', 'fixed'),
            ('min_quantity', '=', 0),
        ]
        item = Item.search(domain, limit=1)
        vals = {
            'pricelist_id': pricelist.id,
            'product_id': product.id,
            'product_tmpl_id': product.product_tmpl_id.id,
            'compute_price': 'fixed',
            'fixed_price': price,
            'date_start': date_start or False,
            'date_end': date_end or False,
        }
        if item:
            item.write(vals)
            return item
        return Item.create(vals)

    def _woo_sync_pricelist_prices(self, product, payload):
        """Write regular/sale prices onto configured import pricelists."""
        self.ensure_one()
        if not self.woo_sync_price or not product:
            return
        payload = payload or {}
        regular = self._woo_remote_price(payload)
        if self.woo_regular_pricelist_id:
            converted = self._woo_convert_price_to_pricelist(
                regular, self.woo_regular_pricelist_id,
            )
            self._woo_upsert_pricelist_item(
                self.woo_regular_pricelist_id, product, converted,
            )
        sale = self._woo_remote_sale_price(payload)
        if self.woo_sale_pricelist_id and sale is not None:
            converted_sale = self._woo_convert_price_to_pricelist(
                sale, self.woo_sale_pricelist_id,
            )
            date_start = self._woo_parse_datetime(
                payload.get('date_on_sale_from_gmt') or payload.get('date_on_sale_from')
            )
            date_end = self._woo_parse_datetime(
                payload.get('date_on_sale_to_gmt') or payload.get('date_on_sale_to')
            )
            self._woo_upsert_pricelist_item(
                self.woo_sale_pricelist_id,
                product,
                converted_sale,
                date_start=date_start,
                date_end=date_end,
            )

    def _woo_apply_extra_field_mappings(self, template, woo_product, linked_rows):
        mappings = self.woo_product_field_mapping_ids.filtered('active')
        if not mappings:
            return
        mappings._apply_to_records(
            template,
            template.product_variant_ids,
            woo_product,
            linked_rows,
        )

    def _woo_category_records(self, woo_categories):
        """Ensure connector-side categories exist; return empty native recordset.

        Native product.category is no longer used for WooCommerce catalog sync.
        """
        self._woo_ensure_woo_categories(woo_categories)
        return self.env['product.category']

    def _woo_upsert_product_binding(self, template, woo_product, categories, tag_ids, variant_count):
        self.ensure_one()
        ProductBinding = self.env['kits.woo.product.binding']
        external_id = str(woo_product.get('id'))
        ptype = woo_product.get('type') or 'simple'
        if ptype not in ('simple', 'variable', 'grouped', 'external', 'bundle'):
            ptype = 'simple'
        images = [
            {'id': img.get('id'), 'src': img.get('src'), 'name': img.get('name')}
            for img in (woo_product.get('images') or [])
            if isinstance(img, dict) and img.get('src')
        ]
        modified = self._woo_parse_datetime(
            woo_product.get('date_modified_gmt') or woo_product.get('date_modified')
        )
        woo_cats = self._woo_ensure_woo_categories(woo_product.get('categories') or [])
        woo_tags = self._woo_ensure_woo_tags(woo_product.get('tags') or [])
        lang = self._woo_store_lang_code()
        remote_name = (woo_product.get('name') or '').strip() or template.with_context(lang=lang).name
        vals = {
            'instance_id': self.id,
            'company_id': self.company_id.id,
            'product_tmpl_id': template.id,
            'external_id': external_id,
            'title': remote_name,
            'description_short': woo_product.get('short_description') or False,
            'description': woo_product.get('description') or False,
            'published': (woo_product.get('status') or 'publish').lower() == 'publish',
            'woo_category_ids': [Command.set(woo_cats.ids)],
            'woo_tag_ids': [Command.set(woo_tags.ids)],
            'image_urls': images,
            'exists_in_store': True,
            'store_write_date': modified,
            'active': True,
            'sync_state': 'synced',
            'sync_error': False,
            'sync_date': fields.Datetime.now(),
            'external_data': {
                'sku': woo_product.get('sku'),
                'type': ptype,
                'date_modified': self._woo_normalize_remote_datetime(
                    woo_product.get('date_modified_gmt') or woo_product.get('date_modified')
                ) or False,
            },
        }
        binding = ProductBinding._find(self, external_id=external_id) or ProductBinding._find(
            self, odoo_record_id=template.id,
        )
        write_ctx = {
            'lang': lang,
            'kits_woo_skip_presentation_dirty': True,
            'kits_woo_skip_dirty': True,
        }
        if binding:
            self._woo_warn_unexported_presentation_overwrite(binding, remote_name)
            vals['sync_version'] = binding.sync_version + 1
            merged = dict(binding.external_data or {})
            merged.update(vals['external_data'])
            vals['external_data'] = merged
            binding.with_context(**write_ctx).write(vals)
            return binding
        vals['sync_version'] = 1
        return ProductBinding.with_context(**write_ctx).create(vals)

    def _woo_warn_unexported_presentation_overwrite(self, binding, remote_name):
        """Log when import replaces a local store title that was not yet exported."""
        self.ensure_one()
        if not binding or binding.sync_state != 'not_synced':
            return
        lang = self._woo_store_lang_code()
        local_title = (binding.with_context(lang=lang).title or '').strip()
        incoming = (remote_name or '').strip()
        if not local_title or not incoming or local_title == incoming:
            return
        message = self.env._(
            'Import on %(store)s will replace unexported store title '
            '"%(local)s" with WooCommerce title "%(remote)s" for product %(product)s.',
            store=self.display_name,
            local=local_title,
            remote=incoming,
            product=binding.product_tmpl_id.display_name,
        )
        _logger.warning(message)
        self._kits_log_operation(
            operation='woo_import_products',
            status='success',
            level='warning',
            message=message,
            details=f'kits.woo.product.binding,{binding.id}',
        )

    def _woo_upsert_variant_binding(self, product_binding, variant, row):
        self.ensure_one()
        payload = row.get('payload') or {}
        image = payload.get('image') or {}
        images = [{'src': image.get('src'), 'id': image.get('id')}] if isinstance(image, dict) and image.get('src') else []
        if not images and row['external_id'] == product_binding.external_id:
            images = product_binding.image_urls or []
        combination = ', '.join(
            f"{(attr.get('name') or '').strip()}: {(attr.get('option') or '').strip()}"
            for attr in (payload.get('attributes') or [])
            if (attr.get('name') or attr.get('option'))
        ) or False
        lang = self._woo_store_lang_code()
        remote_title = (
            (payload.get('name') or '').strip()
            or html2plaintext(payload.get('description') or '').strip()
            or variant.with_context(lang=lang).display_name
        )
        vals = {
            'product_binding_id': product_binding.id,
            'instance_id': self.id,
            'company_id': self.company_id.id,
            'product_id': variant.id,
            'external_id': row['external_id'],
            'sku': row['sku'],
            'title': remote_title,
            'attribute_combination': combination,
            'regular_price': payload.get('regular_price') or False,
            'sale_price': payload.get('sale_price') or False,
            'product_url': payload.get('permalink') or False,
            'exists_in_store': True,
            'manage_stock': bool(payload.get('manage_stock')),
            'image_urls': images,
            'store_create_date': self._woo_parse_datetime(
                payload.get('date_created_gmt') or payload.get('date_created')
            ),
            'store_write_date': self._woo_parse_datetime(
                payload.get('date_modified_gmt') or payload.get('date_modified')
            ),
            'active': True,
            'sync_state': 'synced',
            'sync_error': False,
            'sync_date': fields.Datetime.now(),
            'external_data': {
                'sku': row['sku'],
                'parent_id': row.get('parent_id') or False,
                'date_modified': self._woo_normalize_remote_datetime(
                    payload.get('date_modified_gmt') or payload.get('date_modified')
                ) or False,
            },
        }
        Variant = self.env['kits.woo.variant.binding']
        binding = Variant._find(self, external_id=row['external_id']) or Variant._find(
            self, odoo_record_id=variant.id,
        )
        write_ctx = {
            'lang': lang,
            'kits_woo_skip_presentation_dirty': True,
            'kits_woo_skip_dirty': True,
        }
        if binding:
            vals['sync_version'] = binding.sync_version + 1
            merged = dict(binding.external_data or {})
            merged.update(vals['external_data'])
            vals['external_data'] = merged
            binding.with_context(**write_ctx).write(vals)
            return binding
        vals['sync_version'] = 1
        return Variant.with_context(**write_ctx).create(vals)

    def _woo_archive_variant_remote(self, woo_id, message=False):
        self.ensure_one()
        binding = self.env['kits.woo.variant.binding']._find(self, external_id=str(woo_id))
        if not binding:
            return False
        binding._kits_mark_deleted_remote(message=message)
        return True

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
        """Archive this store's product and variant bindings. Keep the Odoo product."""
        self.ensure_one()
        woo_key = str(woo_id)
        message = message or self.env._(
            'Remote WooCommerce product %(id)s was deleted on %(store)s. The Odoo product was kept.',
            id=woo_key,
            store=self.name,
        )
        template_binding = self.env['kits.woo.product.binding']._find(self, external_id=woo_key)
        templates = self.env['product.template']
        if template_binding:
            variants = template_binding.with_context(active_test=False).variant_binding_ids
            if variants:
                variants._kits_mark_deleted_remote(message=message)
            template_binding._kits_mark_deleted_remote(message=message)
            templates = template_binding.product_tmpl_id
        else:
            self._woo_archive_variant_remote(woo_key, message=message)
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
        """Compatibility shim: ensure connector categories; never return native cats.

        Product ``categ_id`` uses the store default native category only.
        """
        self._woo_ensure_woo_categories(woo_categories)
        return self.env['product.category']

    def _woo_ensure_tags(self, woo_tags):
        """Compatibility shim: ensure connector tags; do not touch native tags."""
        self._woo_ensure_woo_tags(woo_tags)
        return []

    def _woo_ensure_woo_categories(self, woo_categories):
        """Resolve connector categories for this store: external id → slug → create."""
        self.ensure_one()
        Category = self.env['kits.woo.category']
        found = Category.browse()
        for woo_cat in woo_categories or []:
            if not isinstance(woo_cat, dict):
                continue
            woo_cat_id = woo_cat.get('id')
            slug = (woo_cat.get('slug') or '').strip() or False
            name = (woo_cat.get('name') or '').strip()
            if not woo_cat_id and not slug and not name:
                continue
            try:
                if woo_cat_id:
                    self._woo_import_one_category(woo_cat)
                else:
                    # Incomplete product payload: match/create locally without remote id.
                    row = Category._find(self, slug=slug) if slug else Category.browse()
                    if not row and name:
                        # Avoid cross-store duplicates; create pending store-local row.
                        row = Category.with_context(kits_woo_skip_catalog_dirty=True).create({
                            'instance_id': self.id,
                            'company_id': self.company_id.id,
                            'name': name,
                            'slug': slug or False,
                            'exists_in_store': False,
                            'sync_state': 'not_synced',
                        })
                    if row:
                        found |= row
                    continue
            except Exception:  # noqa: BLE001
                _logger.debug(
                    'Category ensure failed for %s', woo_cat_id or slug or name, exc_info=True,
                )
            row = Category._find(self, external_id=woo_cat_id) if woo_cat_id else Category.browse()
            if not row and slug:
                row = Category._find(self, slug=slug)
            if row:
                found |= row
        return found

    def _woo_ensure_woo_tags(self, woo_tags):
        """Resolve connector tags for this store: external id → slug → name → create."""
        self.ensure_one()
        Tag = self.env['kits.woo.tag']
        found = Tag.browse()
        for woo_tag in woo_tags or []:
            if not isinstance(woo_tag, dict):
                continue
            name = (woo_tag.get('name') or '').strip()
            slug = (woo_tag.get('slug') or '').strip() or False
            woo_tag_id = woo_tag.get('id')
            if not name and not woo_tag_id and not slug:
                continue
            try:
                if woo_tag_id and name:
                    self._woo_import_one_tag(woo_tag)
                elif not woo_tag_id:
                    row = (
                        (Tag._find(self, slug=slug) if slug else Tag.browse())
                        or (Tag._find(self, name=name) if name else Tag.browse())
                    )
                    if not row and name:
                        row = Tag.with_context(kits_woo_skip_catalog_dirty=True).create({
                            'instance_id': self.id,
                            'company_id': self.company_id.id,
                            'name': name,
                            'slug': slug or False,
                            'exists_in_store': False,
                            'sync_state': 'not_synced',
                        })
                    if row:
                        found |= row
                    continue
            except Exception:  # noqa: BLE001
                _logger.debug(
                    'Tag ensure failed for %s', woo_tag_id or slug or name, exc_info=True,
                )
            row = Tag._find(self, external_id=woo_tag_id) if woo_tag_id else Tag.browse()
            if not row and slug:
                row = Tag._find(self, slug=slug)
            if not row and name:
                row = Tag._find(self, name=name)
            if row:
                found |= row
        return found

    def _woo_import_variable_product(self, template, woo_product):
        """Create attribute lines for a newly created variable product."""
        self.ensure_one()
        woo_attrs = woo_product.get('attributes') or []

        attr_lines_cmds = []
        attr_value_map = {}  # (attr_name.lower(), option.lower()) -> product.attribute.value
        for woo_attr in woo_attrs:
            if not woo_attr.get('variation'):
                continue
            attr_name = (woo_attr.get('name') or '').strip()
            if not attr_name:
                continue
            woo_attr_id = woo_attr.get('id')
            attribute = False
            if woo_attr_id:
                attribute = self._woo_find_odoo('product.attribute', woo_attr_id)
            if not attribute:
                attribute = self.env['product.attribute'].search(
                    [('name', '=', attr_name)], limit=1,
                )
            if not attribute:
                attribute = self.env['product.attribute'].create({
                    'name': attr_name,
                    'create_variant': 'always',
                })
            if woo_attr_id:
                self._woo_bind(
                    'product.attribute', attribute.id, woo_attr_id,
                    external_data={'slug': woo_attr.get('slug'), 'name': attr_name},
                )
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
                # Product payloads expose option names only (no term ids).
                # Catalog attribute import binds terms with attrId:termId later.
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
            template.with_context(kits_woo_skip_dirty=True).write({
                'attribute_line_ids': attr_lines_cmds,
            })

        template._create_variant_ids()

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
    # Store-specific presentation (names / descriptions)
    # =========================================================================

    def _woo_store_lang_code(self):
        """Language code used for store-specific translated presentation fields."""
        self.ensure_one()
        if self.lang_id and self.lang_id.code:
            return self.lang_id.code
        return self.env.lang or 'en_US'

    def _woo_store_product_title(self, binding, template):
        """Title sent to WooCommerce. Never blank; falls back to native name."""
        self.ensure_one()
        lang = self._woo_store_lang_code()
        if binding:
            store_title = (binding.with_context(lang=lang).title or '').strip()
            if store_title:
                return store_title
        native = (template.with_context(lang=lang).name or template.name or '').strip()
        return native or self.env._('Product')

    def _woo_store_product_descriptions(self, binding, template):
        """Long and short descriptions for the store language."""
        self.ensure_one()
        lang = self._woo_store_lang_code()
        template_l = template.with_context(lang=lang)
        binding_l = binding.with_context(lang=lang) if binding else False
        description = ''
        short = ''
        if binding_l:
            description = binding_l.description or ''
            short = binding_l.description_short or ''
        if not description:
            description = template_l.description or ''
        if not short:
            short = template_l.description_sale or ''
        return description or '', short or ''

    def _woo_html_is_empty(self, value):
        if not value:
            return True
        return not html2plaintext(value).strip()

    def _woo_map_native_category_to_store(self, native_categ):
        """Create/reuse connector categories for a native category chain (no native writes)."""
        self.ensure_one()
        Category = self.env['kits.woo.category']
        if not native_categ:
            return Category
        chain = []
        current = native_categ
        while current:
            chain.append(current)
            current = current.parent_id
        chain.reverse()
        parent_woo = Category
        leaf = Category
        for native in chain:
            existing = Category.with_context(active_test=False).search([
                ('instance_id', '=', self.id),
                ('odoo_category_id', '=', native.id),
            ], limit=1)
            if not existing:
                domain = [
                    ('instance_id', '=', self.id),
                    ('name', '=ilike', native.name),
                ]
                if parent_woo:
                    domain.append(('parent_id', '=', parent_woo.id))
                else:
                    domain.append(('parent_id', '=', False))
                existing = Category.with_context(active_test=False).search(domain, limit=1)
            if not existing:
                existing = Category.with_context(kits_woo_skip_catalog_dirty=True).create({
                    'instance_id': self.id,
                    'company_id': self.company_id.id,
                    'name': native.name,
                    'parent_id': parent_woo.id if parent_woo else False,
                    'odoo_category_id': native.id,
                    'external_id': False,
                    'exists_in_store': False,
                    'sync_state': 'not_synced',
                    'active': True,
                })
            elif not existing.odoo_category_id:
                existing.with_context(kits_woo_skip_catalog_dirty=True).write({
                    'odoo_category_id': native.id,
                })
            parent_woo = existing
            leaf = existing
        return leaf

    def _woo_prepare_product_binding_for_store(self, template):
        """Create or reuse the store product note and initialize empty presentation fields.

        Does not overwrite customized store titles/descriptions. Categories are only
        assigned when a matching connector category is already exported.
        """
        self.ensure_one()
        ProductBinding = self.env['kits.woo.product.binding']
        binding = ProductBinding._find(self, odoo_record_id=template.id)
        lang = self._woo_store_lang_code()
        template_l = template.with_context(lang=lang)
        write_ctx = {
            'lang': lang,
            'kits_woo_skip_presentation_dirty': True,
            'kits_woo_skip_dirty': True,
        }
        updates = {}
        if binding:
            binding_l = binding.with_context(lang=lang)
            if not (binding_l.title or '').strip():
                updates['title'] = template_l.name
            if self._woo_html_is_empty(binding_l.description) and template_l.description:
                updates['description'] = template_l.description
            if self._woo_html_is_empty(binding_l.description_short) and template_l.description_sale:
                updates['description_short'] = template_l.description_sale
            if updates:
                binding.with_context(**write_ctx).write(updates)
        else:
            binding = ProductBinding.with_context(**write_ctx).create({
                'instance_id': self.id,
                'company_id': self.company_id.id,
                'product_tmpl_id': template.id,
                'external_id': False,
                'title': template_l.name,
                'description': template_l.description or False,
                'description_short': template_l.description_sale or False,
                'exists_in_store': False,
                'sync_state': 'not_synced',
                'active': True,
                'published': bool(template.active),
            })

        if not binding.woo_category_ids and template.categ_id:
            leaf = self._woo_map_native_category_to_store(template.categ_id)
            if leaf and leaf._kits_is_exported():
                binding.with_context(**write_ctx).write({
                    'woo_category_ids': [Command.set([leaf.id])],
                })

        for variant in template.product_variant_ids:
            self._woo_prepare_variant_binding_title(binding, variant)
        return binding

    def _woo_prepare_variant_binding_title(self, product_binding, variant):
        """Initialize store variant title from the native variant when empty."""
        self.ensure_one()
        if not product_binding or not product_binding._kits_has_remote_id():
            return self.env['kits.woo.variant.binding']
        Variant = self.env['kits.woo.variant.binding']
        binding = Variant._find(self, odoo_record_id=variant.id)
        if not binding:
            return Variant
        lang = self._woo_store_lang_code()
        if (binding.with_context(lang=lang).title or '').strip():
            return binding
        binding.with_context(
            lang=lang,
            kits_woo_skip_presentation_dirty=True,
            kits_woo_skip_dirty=True,
        ).write({'title': variant.with_context(lang=lang).display_name})
        return binding

    _WOO_UPDATE_GROUPS = frozenset({
        'basic_details', 'price', 'images', 'status', 'inventory', 'attributes',
    })

    def _woo_normalize_update_groups(self, update_groups):
        """Return None for full export, or a frozenset of group names."""
        if not update_groups:
            return None
        if isinstance(update_groups, str):
            update_groups = [update_groups]
        groups = frozenset(update_groups) & self._WOO_UPDATE_GROUPS
        return groups or None

    def _woo_update_group_includes(self, groups, group_name):
        return groups is None or group_name in groups

    def _woo_with_catalog_store_lock(self):
        """Serialize catalog preparation for this store (prevents duplicate remotes)."""
        self.ensure_one()
        self.env.cr.execute(
            'SELECT id FROM kits_woo_instance WHERE id = %s FOR UPDATE',
            (self.id,),
        )

    def _woo_collect_assigned_catalog(self, templates):
        """Return (categories_with_parents, tags) assigned on bindings for templates."""
        self.ensure_one()
        Category = self.env['kits.woo.category']
        Tag = self.env['kits.woo.tag']
        bindings = self.env['kits.woo.product.binding'].with_context(active_test=False).search([
            ('instance_id', '=', self.id),
            ('product_tmpl_id', 'in', templates.ids),
        ])
        categories = Category.browse()
        tags = Tag.browse()
        for binding in bindings:
            categories |= binding.woo_category_ids
            tags |= binding.woo_tag_ids
        # Expand parents (store-local only)
        needed = Category.browse()
        for category in categories:
            current = category
            while current:
                if current.instance_id != self:
                    raise UserError(self.env._(
                        'Category "%(name)s" belongs to another WooCommerce store '
                        '("%(store)s"). Reassign it on this store\'s product note.',
                        name=current.display_name,
                        store=current.instance_id.display_name,
                    ))
                needed |= current
                current = current.parent_id
        for tag in tags:
            if tag.instance_id != self:
                raise UserError(self.env._(
                    'Tag "%(name)s" belongs to another WooCommerce store '
                    '("%(store)s"). Reassign it on this store\'s product note.',
                    name=tag.display_name,
                    store=tag.instance_id.display_name,
                ))
        return needed, tags

    def _woo_prepare_catalog_for_product_export(self, templates, job=None, update_groups=None):
        """Once-per-store prep: remote sync, export missing assigned cats/tags, validate.

        Only assigned categories/tags (plus category parents) for ``templates`` are
        exported. Unrelated pending catalog records are left untouched.
        """
        self.ensure_one()
        groups = self._woo_normalize_update_groups(update_groups)
        if groups is not None and 'basic_details' not in groups:
            return 'Catalog prep skipped (update groups exclude basic_details)'

        self._woo_with_catalog_store_lock()
        parts = []
        try:
            # Never reuse product job payload (template_ids ≠ category woo ids).
            parts.append(f'categories sync: {self._woo_import_categories(job=None)}')
            parts.append(f'tags sync: {self._woo_import_tags(job=None)}')
        except Exception as exc:  # noqa: BLE001
            _logger.exception(
                'Catalog remote sync failed before product export on store %s', self.id,
            )
            raise UserError(self.env._(
                'Cannot export products to "%(store)s" until categories and tags '
                'are synchronized from WooCommerce: %(error)s. '
                'Retry category/tag import, then export products again.',
                store=self.display_name,
                error=exc,
            )) from exc

        # Re-read assignments after remote sync (external ids may have been linked).
        categories, tags = self._woo_collect_assigned_catalog(templates)
        missing_cats = categories.filtered(lambda c: not c._kits_is_exported())
        missing_tags = tags.filtered(lambda t: not t._kits_is_exported())
        if missing_cats:
            parts.append(
                f'assigned categories export: '
                f'{self._woo_export_selected_categories(missing_cats, job=job)}'
            )
        if missing_tags:
            parts.append(
                f'assigned tags export: '
                f'{self._woo_export_selected_tags(missing_tags, job=job)}'
            )
        # Per-product validation blocks only affected products; do not fail the
        # whole batch here when some assigned catalog rows remain unresolved.
        categories.invalidate_recordset()
        tags.invalidate_recordset()
        still_bad_cats = categories.filtered(lambda c: not c._kits_is_exported())
        still_bad_tags = tags.filtered(lambda t: not t._kits_is_exported())
        if still_bad_cats or still_bad_tags:
            names = still_bad_cats.mapped('display_name') + still_bad_tags.mapped('display_name')
            parts.append('unresolved assigned catalog: ' + ', '.join(names))
        return '; '.join(parts)

    def _woo_validate_binding_catalog_for_export(self, binding, template):
        """Raise if any assigned cat/tag is invalid; never silently drop assignments."""
        self.ensure_one()
        from odoo.addons.kits_odoo_woocommerce_connector.models.kits_woo_catalog import (
            _kits_norm_external_id,
        )
        if not binding:
            return [], []
        invalid = []
        cat_payload = []
        for category in binding.woo_category_ids:
            if category.instance_id != self:
                invalid.append(self.env._(
                    'Category "%(name)s" belongs to another store.',
                    name=category.display_name,
                ))
                continue
            # Parents must be exported too
            missing_parent = False
            ancestor = category
            while ancestor:
                if ancestor.instance_id != self:
                    invalid.append(self.env._(
                        'Required parent "%(parent)s" of category "%(child)s" '
                        'belongs to another store.',
                        parent=ancestor.display_name,
                        child=category.display_name,
                    ))
                    missing_parent = True
                    break
                if not ancestor._kits_is_exported():
                    if ancestor == category:
                        # Walk parents to name any missing ancestor in the message.
                        missing_ancestor = ancestor.parent_id
                        while missing_ancestor and missing_ancestor._kits_is_exported():
                            missing_ancestor = missing_ancestor.parent_id
                        if missing_ancestor and not missing_ancestor._kits_is_exported():
                            invalid.append(self.env._(
                                'Category "%(child)s" is missing its WooCommerce ID. '
                                'Required parent "%(parent)s" is also not exported.',
                                child=category.display_name,
                                parent=missing_ancestor.display_name,
                            ))
                        else:
                            invalid.append(self.env._(
                                'Category "%(name)s" is missing its WooCommerce ID.',
                                name=category.display_name,
                            ))
                    else:
                        invalid.append(self.env._(
                            'Category "%(child)s" requires parent "%(parent)s", '
                            'which is missing its WooCommerce ID.',
                            child=category.display_name,
                            parent=ancestor.display_name,
                        ))
                    missing_parent = True
                    break
                ancestor = ancestor.parent_id
            if missing_parent:
                continue
            ext = _kits_norm_external_id(category.external_id)
            try:
                cat_payload.append({'id': int(ext)})
            except (TypeError, ValueError):
                invalid.append(self.env._(
                    'Category "%(name)s" has an invalid WooCommerce ID "%(ext)s".',
                    name=category.display_name,
                    ext=category.external_id,
                ))

        tag_payload = []
        for tag in binding.woo_tag_ids:
            if tag.instance_id != self:
                invalid.append(self.env._(
                    'Tag "%(name)s" belongs to another store.',
                    name=tag.display_name,
                ))
                continue
            if not tag._kits_is_exported():
                invalid.append(self.env._(
                    'Tag "%(name)s" is missing its WooCommerce ID.',
                    name=tag.display_name,
                ))
                continue
            ext = _kits_norm_external_id(tag.external_id)
            try:
                tag_payload.append({'id': int(ext)})
            except (TypeError, ValueError):
                invalid.append(self.env._(
                    'Tag "%(name)s" has an invalid WooCommerce ID "%(ext)s".',
                    name=tag.display_name,
                    ext=tag.external_id,
                ))

        if invalid:
            sku = (template.default_code or '').strip()
            product_label = template.display_name
            if sku and sku not in product_label:
                product_label = f'{product_label} (SKU: {sku})'
            raise UserError(self.env._(
                'Product %(product)s cannot be exported to %(store)s. %(reasons)s '
                'Retry category/tag synchronization, then export the product again.',
                product=product_label,
                store=self.display_name,
                reasons=' '.join(invalid),
            ))
        return cat_payload, tag_payload

    def _woo_build_product_export_payload(self, template, binding=None, update_groups=None):
        """Build WooCommerce product payload from the store-specific product note."""
        self.ensure_one()
        groups = self._woo_normalize_update_groups(update_groups)
        binding = binding or self.env['kits.woo.product.binding']._find(
            self, odoo_record_id=template.id,
        )
        is_variable = self._woo_template_is_variable(template)
        payload = {}

        if self._woo_update_group_includes(groups, 'basic_details'):
            description, short = self._woo_store_product_descriptions(binding, template)
            payload.update({
                'name': self._woo_store_product_title(binding, template),
                'description': description,
                'short_description': short,
                'sku': template.default_code or '',
            })
            if template.barcode and not is_variable:
                payload['barcode'] = template.barcode

        if self._woo_update_group_includes(groups, 'status'):
            status = 'publish'
            if binding and binding.published is False:
                status = 'draft'
            elif not template.active:
                status = 'draft'
            payload['status'] = status
            payload['catalog_visibility'] = template.woo_catalog_visibility or 'visible'
            payload['featured'] = bool(template.woo_featured)

        if groups is None or 'basic_details' in groups or 'attributes' in groups:
            payload['type'] = 'variable' if is_variable else 'simple'

        if self._woo_update_group_includes(groups, 'price'):
            if not is_variable:
                price = self._woo_get_export_price(template.product_variant_id or template)
                payload['regular_price'] = f'{price:.2f}'
            elif groups is None:
                payload['regular_price'] = ''

        if self._woo_update_group_includes(groups, 'inventory'):
            payload['manage_stock'] = (
                bool(template.is_storable) if 'is_storable' in template._fields else True
            )

        if binding and self._woo_update_group_includes(groups, 'basic_details'):
            cat_payload, tag_payload = self._woo_validate_binding_catalog_for_export(
                binding, template,
            )
            # Always send keys when assignments exist so remotes are not left stale
            # by a partial local list — validation already blocked invalids.
            if binding.woo_category_ids:
                payload['categories'] = cat_payload
            if binding.woo_tag_ids:
                payload['tags'] = tag_payload

        if is_variable and self._woo_update_group_includes(groups, 'attributes'):
            payload['attributes'] = self._woo_export_attribute_payload(template)

        if self._woo_update_group_includes(groups, 'images'):
            images = self._woo_prepare_export_images(template)
            if images:
                payload['images'] = images

        return payload

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

        Optional ``payload.update_groups`` limits fields (basic_details, price,
        images, status, inventory, attributes). Omit for a full export.
        """
        self.ensure_one()
        api = self._woo_api()
        payload = (job.payload if job else None) or {}
        ProductTemplate = self.env['product.template']
        template_ids = self._woo_parse_id_list(payload.get('template_ids'))
        expected_versions = payload.get('sync_versions') or {}
        force = bool(payload.get('force'))
        update_groups = self._woo_normalize_update_groups(payload.get('update_groups'))

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

        # Once per store/job: sync remotes, export missing assigned cats/tags (+ parents).
        if templates:
            for template in templates:
                self._woo_prepare_product_binding_for_store(template)
            try:
                self._woo_prepare_catalog_for_product_export(
                    templates, job=job, update_groups=update_groups,
                )
            except UserError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise UserError(self.env._(
                    'Catalog preparation failed for store "%(store)s": %(error)s',
                    store=self.display_name,
                    error=exc,
                )) from exc

        created = updated = failed = skipped = 0
        errors = []
        for template in templates:
            try:
                self._woo_prepare_product_binding_for_store(template)
                binding = self._woo_record_binding(
                    'product.template', odoo_record_id=template.id,
                )
                expected = expected_versions.get(str(template.id))
                if binding and expected is not None and binding._kits_is_stale_job(expected):
                    skipped += 1
                    continue
                result = self._woo_export_one_template(
                    template,
                    api,
                    allow_recreate=self._woo_allows_recreate(job),
                    update_groups=update_groups,
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
                binding = self._woo_record_binding(
                    'product.template', odoo_record_id=template.id,
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

    def _woo_template_is_variable(self, template):
        """Export as Woo variable only when Odoo has real selectable variants.

        Attribute lines alone are not enough. ``no_variant`` / incomplete lines leave
        a single template variant with no attribute values; posting empty variations
        makes WooCommerce return HTTP 400.
        """
        variants = template.product_variant_ids
        if len(variants) <= 1:
            return False
        return any(variant.product_template_attribute_value_ids for variant in variants)

    def _woo_export_one_template(self, template, api, *, allow_recreate=False, update_groups=None):
        self.ensure_one()
        binding = self._woo_prepare_product_binding_for_store(template)
        if binding and binding.sync_state in ('deleted', 'deleted_local') and not allow_recreate:
            return 'skipped'
        if binding and binding.sync_state == 'deleted_local':
            # Local deletion keeps the remote product. Do not open a second remote product.
            return 'skipped'

        groups = self._woo_normalize_update_groups(update_groups)
        payload = self._woo_build_product_export_payload(
            template, binding=binding, update_groups=groups,
        )
        if not payload:
            return 'skipped'
        # Create requires a complete payload; expand to full when first publishing.
        has_remote = bool(binding and binding._kits_has_remote_id() and binding.sync_state != 'deleted')
        if not has_remote and groups is not None:
            payload = self._woo_build_product_export_payload(template, binding=binding)
        product_type = payload.get('type') or (
            'variable' if self._woo_template_is_variable(template) else 'simple'
        )
        payload.setdefault('type', product_type)

        remote = None
        woo_id = None
        result = None
        create_new = not has_remote
        if has_remote:
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
            'type': product_type,
            'title': payload.get('name') or False,
        })
        if remote and remote.get('images'):
            self._woo_remember_remote_images(binding, remote.get('images'))

        if product_type == 'simple':
            variant = template.product_variant_id
            if variant:
                self._woo_bind('product.product', variant.id, woo_id, external_data={
                    'sku': variant.default_code,
                    'parent_id': False,
                })
        elif groups is None or 'attributes' in groups or 'price' in groups or 'inventory' in groups:
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

            binding = self._woo_record_binding(
                'product.product', odoo_record_id=variant.id,
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
