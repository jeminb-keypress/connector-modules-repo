# -*- coding: utf-8 -*-
import logging

from odoo import fields, models
from odoo.exceptions import UserError

from odoo.addons.kits_odoo_base_connector.services.exceptions import ConnectorAPIError

_logger = logging.getLogger(__name__)


class KitsConnectorInstanceInventory(models.Model):
    _inherit = 'kits.connector.instance'

    def _woo_export_inventory(self, job=None):
        """Push Odoo free quantities (warehouse-scoped) to WooCommerce stock fields.

        Job payload keys (optional):
        - ``product_ids``: limit to these ``product.product`` IDs (event-driven / targeted)
        - ``sync_versions``: ``{product_id: version}`` to skip stale jobs
        - ``full``: when true, export all active product bindings (manual bulk / cron catch-up)
        """
        self.ensure_one()
        warehouse = self._woo_get_warehouse()
        if not warehouse:
            raise UserError(self.env._(
                'Warehouse is required on "%(name)s" to export inventory.',
                name=self.name,
            ))

        api = self._woo_api()
        Binding = self._woo_binding()
        payload = (job.payload if job else None) or {}
        product_ids = self._woo_parse_id_list(payload.get('product_ids'))
        expected_versions = payload.get('sync_versions') or {}
        force_full = bool(payload.get('full')) or not product_ids

        domain = [
            ('instance_id', '=', self.id),
            ('odoo_model', '=', 'product.product'),
            ('active', '=', True),
            ('sync_state', 'not in', ('deleted', 'deleted_local')),
        ]
        if product_ids and not force_full:
            domain.append(('odoo_record_id', 'in', product_ids))
        elif force_full and not product_ids:
            # Cron recovery: prefer not_synced/error bindings; manual full still exports all.
            if payload.get('recovery_only'):
                domain.append(('sync_state', 'in', ('not_synced', 'error')))

        bindings = Binding.search(domain)
        updated = skipped = failed = 0
        errors = []
        products = self.env['product.product'].browse(
            bindings.mapped('odoo_record_id')
        ).exists()
        products = products.with_context(warehouse_id=warehouse.id)
        binding_by_product = {b.odoo_record_id: b for b in bindings}

        for product in products:
            binding = binding_by_product.get(product.id)
            if not binding:
                skipped += 1
                continue
            expected = expected_versions.get(str(product.id))
            if expected is not None and binding._kits_is_stale_job(expected):
                skipped += 1
                continue
            try:
                qty = float(product.free_qty or 0.0)
                stock_qty = int(qty) if abs(qty - int(qty)) < 1e-9 else qty
                payload_body = {
                    'manage_stock': True,
                    'stock_quantity': stock_qty,
                    'stock_status': 'instock' if qty > 0 else 'outofstock',
                }
                external_data = dict(binding.external_data or {})
                parent_id = external_data.get('parent_id')
                woo_id = int(binding.external_id)
                if parent_id:
                    api.update_variation(int(parent_id), woo_id, payload_body)
                else:
                    api.update_product(woo_id, payload_body)
                external_data['last_stock_qty'] = stock_qty
                binding.write({
                    'external_data': external_data,
                    'sync_date': fields.Datetime.now(),
                    'sync_state': 'synced',
                    'sync_error': False,
                    'sync_version': binding.sync_version + 1,
                })
                updated += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                message = str(exc)
                status = getattr(exc, 'status_code', None)
                if isinstance(exc, ConnectorAPIError) and status == 404:
                    message = self.env._(
                        'Remote WooCommerce product/variant %(id)s is missing. '
                        'Stock export did not recreate it.',
                        id=binding.external_id,
                    )
                    parent_id = (binding.external_data or {}).get('parent_id')
                    if parent_id:
                        binding._kits_mark_deleted_remote(message=message)
                    else:
                        self._woo_mark_product_deleted_remote(
                            binding.external_id, message=message,
                        )
                    errors.append(f'Product {product.id} / Woo {binding.external_id}: {message}')
                    self._kits_log_operation(
                        operation='woo_export_inventory',
                        status='failure',
                        level='error',
                        message=message,
                        details=f'product.product,{product.id}',
                        job=job,
                    )
                    continue
                errors.append(f'Product {product.id} / Woo {binding.external_id}: {message}')
                _logger.exception(
                    'WooCommerce inventory export failed for product %s', product.id,
                )
                binding._kits_mark_error(message)
                self._kits_log_operation(
                    operation='woo_export_inventory',
                    status='failure',
                    level='error',
                    message=message,
                    details=f'product.product,{product.id}',
                    job=job,
                )

        # Bindings pointing at deleted Odoo products
        for binding in bindings:
            if binding.odoo_record_id not in products.ids:
                skipped += 1
                binding._kits_mark_error(
                    self.env._('Bound Odoo product %(id)s is missing.', id=binding.odoo_record_id)
                )

        self.woo_last_stock_sync = fields.Datetime.now()
        summary = (
            f'Inventory export finished: updated={updated}, skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_enqueue_inventory_export(self, products, *, reason='stock_change'):
        """Enqueue a targeted inventory export for bound products (deduplicated)."""
        self.ensure_one()
        if not self._is_woocommerce() or not self.active or not self.woo_auto_export_stock:
            return self.env['kits.connector.sync.job']
        if not self._woo_get_warehouse():
            return self.env['kits.connector.sync.job']

        products = products.exists()
        if not products:
            return self.env['kits.connector.sync.job']

        Binding = self._woo_binding()
        bindings = Binding.search([
            ('instance_id', '=', self.id),
            ('odoo_model', '=', 'product.product'),
            ('odoo_record_id', 'in', products.ids),
            ('active', '=', True),
            ('sync_state', 'not in', ('deleted', 'deleted_local')),
        ])
        if not bindings:
            return self.env['kits.connector.sync.job']

        # Mark not synced and bump versions so older jobs become stale.
        bindings._kits_mark_not_synced()
        sync_versions = {str(b.odoo_record_id): b.sync_version for b in bindings}
        product_ids = sorted({b.odoo_record_id for b in bindings})

        Job = self.env['kits.connector.sync.job']
        pending = Job.search([
            ('instance_id', '=', self.id),
            ('job_type', '=', 'woo_export_inventory'),
            ('state', 'in', ('pending', 'draft')),
        ])
        for job in pending:
            payload = dict(job.payload or {})
            if payload.get('full') or payload.get('recovery_only'):
                continue
            existing_ids = set(self._woo_parse_id_list(payload.get('product_ids')))
            merged = sorted(existing_ids | set(product_ids))
            versions = dict(payload.get('sync_versions') or {})
            versions.update(sync_versions)
            job.write({
                'payload': {
                    'product_ids': merged,
                    'sync_versions': versions,
                    'reason': reason,
                },
                'name': self.env._(
                    'WooCommerce: Inventory Export (%(store)s) — %(count)s products',
                    store=self.name,
                    count=len(merged),
                ),
            })
            if job.state == 'draft':
                job.action_enqueue()
            return job

        return self.action_create_sync_job(
            'woo_export_inventory',
            name=self.env._(
                'WooCommerce: Inventory Export (%(store)s) — %(count)s products',
                store=self.name,
                count=len(product_ids),
            ),
            payload={
                'product_ids': product_ids,
                'sync_versions': sync_versions,
                'reason': reason,
            },
            priority=5,
        )
