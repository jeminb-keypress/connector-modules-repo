# -*- coding: utf-8 -*-
from odoo import fields, models
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    woo_instance_id = fields.Many2one(
        'kits.connector.instance',
        string='WooCommerce Store',
        copy=False,
        index=True,
        ondelete='set null',
        check_company=True,
        domain="[('platform_type', '=', 'woocommerce')]",
    )
    woo_order_id = fields.Char(
        string='WooCommerce Order ID',
        copy=False,
        index=True,
        readonly=True,
    )
    woo_order_status = fields.Char(
        string='WooCommerce Status',
        copy=False,
        readonly=True,
    )
    woo_payment_method = fields.Char(
        string='Woo Payment Method',
        copy=False,
        readonly=True,
        help='WooCommerce payment_method code from the remote order.',
    )
    woo_payment_method_title = fields.Char(
        string='Woo Payment Title',
        copy=False,
        readonly=True,
    )
    woo_payment_method_id = fields.Many2one(
        'payment.method',
        string='Mapped Payment Method',
        copy=False,
        readonly=True,
        help='Filled from store payment gateway mapping when configured. '
             'Does not create or register an accounting payment.',
    )
    woo_tracking_number = fields.Char(
        string='Tracking Number',
        copy=False,
        help='Tracking reference to export to WooCommerce (also reads done pickings when empty).',
    )
    woo_tracking_url = fields.Char(
        string='Tracking URL',
        copy=False,
    )
    woo_sync_conflict = fields.Text(
        string='WooCommerce Sync Conflict',
        copy=False,
        readonly=True,
        help='Set when a remote WooCommerce order change cannot safely overwrite this Odoo order.',
    )

    def action_woo_export_tracking(self):
        """Push tracking info to the linked WooCommerce order."""
        self.ensure_one()
        if not self.woo_instance_id or not self.woo_order_id:
            raise UserError(self.env._(
                'This sales order is not linked to a WooCommerce order.'
            ))
        return self.woo_instance_id._woo_export_order_tracking(self)

    def action_woo_cancel_order(self):
        """Cancel the linked WooCommerce order without cancelling this sales order."""
        self.ensure_one()
        if not self.env.user.has_group('kits_odoo_base_connector.group_kits_connector_manager'):
            raise UserError(self.env._(
                'Only Connector Administrators can cancel a WooCommerce order.'
            ))
        if not self.woo_instance_id or not self.woo_order_id:
            raise UserError(self.env._('This sales order is not linked to a WooCommerce order.'))
        state_before = self.state
        result = self.woo_instance_id._woo_cancel_remote_order(self)
        if self.state != state_before:
            raise UserError(self.env._(
                'The Odoo order state changed during a WooCommerce cancellation. This is not expected.'
            ))
        return result

    def action_woo_export_shipment_status(self):
        """Mark the linked WooCommerce order completed when the Odoo delivery is done."""
        self.ensure_one()
        if not self.woo_instance_id or not self.woo_order_id:
            raise UserError(self.env._('This sales order is not linked to a WooCommerce order.'))
        if not self.woo_instance_id._woo_order_ready_to_complete(self):
            raise UserError(self.env._(
                'Order %(order)s is not ready. Complete every outgoing delivery first, '
                'and leave the WooCommerce status open.',
                order=self.display_name,
            ))
        remote = self.woo_instance_id._woo_api().update_order(
            int(self.woo_order_id), {'status': 'completed'},
        )
        self.woo_order_status = remote.get('status') or 'completed'
        self.woo_instance_id._kits_log_operation(
            operation='woo_export_shipments',
            status='success',
            level='info',
            message=self.env._(
                'Set WooCommerce order %(woo)s to completed from %(order)s.',
                woo=self.woo_order_id,
                order=self.name,
            ),
            details=f'sale.order,{self.id}',
        )
        return self.woo_instance_id._woo_notify(self.env._(
            'WooCommerce order %(woo)s is now %(status)s.',
            woo=self.woo_order_id,
            status=self.woo_order_status,
        ))
