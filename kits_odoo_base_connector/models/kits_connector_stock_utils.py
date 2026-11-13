# -*- coding: utf-8 -*-
"""Reusable stock quantity and delivery helpers."""

from odoo import models
from odoo.exceptions import UserError


class KitsConnectorStockUtils(models.AbstractModel):
    _name = 'kits.connector.stock.utils'
    _description = 'Kits Connector Stock Utilities'

    def _kits_get_warehouse(self, warehouse=None, company=None):
        """Resolve a warehouse with company fallback."""
        company = company or self.env.company
        if warehouse:
            return warehouse
        Warehouse = self.env['stock.warehouse'].with_company(company)
        return Warehouse.search([('company_id', '=', company.id)], limit=1)

    def _kits_require_warehouse(self, warehouse=None, company=None):
        warehouse = self._kits_get_warehouse(warehouse=warehouse, company=company)
        if not warehouse:
            raise UserError(self.env._(
                'A warehouse is required for stock operations on this connector store.'
            ))
        return warehouse

    def _kits_product_qty_available(self, product, warehouse=None, company=None):
        """Free quantity for ``product`` in the warehouse stock location."""
        warehouse = self._kits_require_warehouse(warehouse=warehouse, company=company)
        location = warehouse.lot_stock_id
        product = product.with_context(location=location.id)
        qty = product.qty_available
        try:
            return int(qty)
        except (TypeError, ValueError):
            return int(float(qty or 0))

    def _kits_find_delivery_carrier(self, name, *, company=None):
        """Match a delivery.carrier by name (case-insensitive), company-scoped."""
        Carrier = self.env['delivery.carrier']
        name = (name or '').strip()
        if not name or 'delivery.carrier' not in self.env:
            return Carrier
        company = company or self.env.company
        return Carrier.with_company(company).search([
            ('name', '=ilike', name),
            ('company_id', 'in', [False, company.id]),
        ], limit=1)
