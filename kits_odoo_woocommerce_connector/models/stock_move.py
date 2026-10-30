# -*- coding: utf-8 -*-
import logging

from odoo import models

_logger = logging.getLogger(__name__)


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _action_done(self, cancel_backorder=False):
        """After Odoo applies stock moves, enqueue targeted Woo inventory exports.

        Odoo remains the quantity source of truth. Imported Woo orders only confirm
        sale orders; they do not invent stock moves here. Deliveries, returns, and
        adjustments that change ``free_qty`` trigger a per-product export job.
        """
        moves = super()._action_done(cancel_backorder=cancel_backorder)
        self._woo_schedule_inventory_export_for_moves(moves)
        return moves

    def _woo_schedule_inventory_export_for_moves(self, moves):
        products = moves.mapped('product_id').filtered(lambda p: p.is_storable)
        if not products:
            return
        companies = products.mapped('company_id') | moves.mapped('company_id')
        Instance = self.env['kits.connector.instance']
        domain = [
            ('platform_type', '=', 'woocommerce'),
            ('active', '=', True),
            ('woo_auto_export_stock', '=', True),
        ]
        if companies:
            domain.append(('company_id', 'in', companies.ids or [self.env.company.id]))
        for instance in Instance.search(domain):
            company_products = products.filtered(
                lambda p, company=instance.company_id: not p.company_id or p.company_id == company
            )
            if not company_products:
                continue
            try:
                instance._woo_enqueue_inventory_export(
                    company_products,
                    reason='stock_move_done',
                )
            except Exception:  # noqa: BLE001
                _logger.exception(
                    'WooCommerce inventory enqueue failed for instance %s; other stores continue',
                    instance.id,
                )
