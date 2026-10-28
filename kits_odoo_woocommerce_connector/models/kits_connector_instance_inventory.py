# -*- coding: utf-8 -*-
import logging

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class KitsConnectorInstanceInventory(models.Model):
    _inherit = 'kits.connector.instance'

    def _woo_export_inventory(self, job=None):
        """Push Odoo free quantities (warehouse-scoped) to WooCommerce stock fields."""
        self.ensure_one()
        warehouse = self._woo_get_warehouse()
        if not warehouse:
            raise UserError(self.env._(
                'Warehouse is required on "%(name)s" to export inventory.',
                name=self.name,
            ))

        api = self._woo_api()
        Binding = self._woo_binding()
        bindings = Binding.search([
            ('instance_id', '=', self.id),
            ('odoo_model', '=', 'product.product'),
            ('active', '=', True),
        ])

        updated = skipped = failed = 0
        errors = []
        products = self.env['product.product'].browse(
            bindings.mapped('odoo_record_id')
        ).exists()
        # Odoo stock quantity computes honor warehouse_id context (id or record)
        products = products.with_context(warehouse_id=warehouse.id)

        binding_by_product = {b.odoo_record_id: b for b in bindings}

        for product in products:
            binding = binding_by_product.get(product.id)
            if not binding:
                skipped += 1
                continue
            try:
                qty = float(product.free_qty or 0.0)
                # WooCommerce accepts integers; keep fractional only when needed
                stock_qty = int(qty) if abs(qty - int(qty)) < 1e-9 else qty
                payload = {
                    'manage_stock': True,
                    'stock_quantity': stock_qty,
                    'stock_status': 'instock' if qty > 0 else 'outofstock',
                }
                external_data = binding.external_data or {}
                parent_id = external_data.get('parent_id')
                woo_id = int(binding.external_id)
                if parent_id:
                    api.update_variation(int(parent_id), woo_id, payload)
                else:
                    api.update_product(woo_id, payload)
                binding.sync_date = fields.Datetime.now()
                updated += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f'Product {product.id} / Woo {binding.external_id}: {exc}')
                _logger.exception(
                    'WooCommerce inventory export failed for product %s', product.id,
                )
                self._kits_log_operation(
                    operation='woo_export_inventory',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f'product.product,{product.id}',
                    job=job,
                )

        self.woo_last_stock_sync = fields.Datetime.now()
        summary = (
            f'Inventory export finished: updated={updated}, skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary
