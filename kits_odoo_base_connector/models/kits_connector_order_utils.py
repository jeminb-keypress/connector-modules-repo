# -*- coding: utf-8 -*-
"""Reusable order workflow helpers (confirm / invoice / payment flags)."""

from odoo import fields, models


class KitsConnectorOrderSettingsMixin(models.AbstractModel):
    """Optional auto-processing flags for platform instance models."""

    _name = 'kits.connector.order.settings.mixin'
    _description = 'Kits Connector Order Auto-Processing Settings'

    kits_auto_confirm_order = fields.Boolean(
        string='Auto Confirm Orders',
        default=True,
        help='When enabled, imported sales orders are confirmed automatically.',
    )
    kits_auto_create_invoice = fields.Boolean(
        string='Auto Create Invoice',
        default=False,
        help='When enabled, create a draft customer invoice after order confirm.',
    )
    kits_auto_validate_invoice = fields.Boolean(
        string='Auto Validate Invoice',
        default=False,
        help='When enabled, post the invoice created by auto-create.',
    )
    kits_auto_register_payment = fields.Boolean(
        string='Auto Register Payment',
        default=False,
        help='When enabled, register a payment against a posted invoice. '
             'Never invents payments when no invoice exists.',
    )

    def _kits_apply_order_automation(self, order):
        """Apply confirm / invoice / payment settings to a sale order."""
        self.ensure_one()
        order.ensure_one()
        if self.kits_auto_confirm_order and order.state in ('draft', 'sent'):
            order.action_confirm()
        if not self.kits_auto_create_invoice:
            return order
        if order.state not in ('sale', 'done'):
            return order
        invoices = order._create_invoices()
        if self.kits_auto_validate_invoice and invoices:
            invoices.action_post()
        if self.kits_auto_register_payment and invoices:
            for invoice in invoices.filtered(lambda move: move.state == 'posted'):
                if hasattr(invoice, 'action_register_payment'):
                    # Platforms may override with gateway-aware payment; base only
                    # exposes the hook — do not invent journal entries blindly.
                    continue
        return order
