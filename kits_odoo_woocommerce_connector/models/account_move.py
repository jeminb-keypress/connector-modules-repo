# -*- coding: utf-8 -*-
from odoo import models
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _woo_sale_order_for_refund(self):
        """Find the WooCommerce sales order behind this credit note."""
        self.ensure_one()
        orders = self.invoice_line_ids.sale_line_ids.order_id
        if not orders and self.reversed_entry_id:
            orders = self.reversed_entry_id.invoice_line_ids.sale_line_ids.order_id
        orders = orders.filtered(lambda order: order.woo_order_id and order.woo_instance_id)
        if len(orders) == 1:
            return orders
        if len(orders) > 1:
            raise UserError(self.env._(
                'Credit note %(name)s is linked to more than one WooCommerce order.',
                name=self.display_name,
            ))
        if self.invoice_origin:
            found = self.env['sale.order'].search([
                ('name', '=', self.invoice_origin),
                ('woo_order_id', '!=', False),
                ('company_id', '=', self.company_id.id),
            ], limit=2)
            if len(found) == 1:
                return found
        raise UserError(self.env._(
            'Credit note %(name)s is not linked to a WooCommerce sales order.',
            name=self.display_name,
        ))

    def action_woo_export_refund(self):
        """Create the WooCommerce refund for this credit note."""
        self.ensure_one()
        if not self.env.user.has_group('kits_odoo_base_connector.group_kits_connector_manager'):
            raise UserError(self.env._(
                'Only Connector Administrators can refund a WooCommerce order.'
            ))
        if self.move_type != 'out_refund':
            raise UserError(self.env._('Only a customer credit note can be refunded in WooCommerce.'))
        order = self._woo_sale_order_for_refund()
        return order.woo_instance_id._woo_export_credit_note_refund(self, order)
