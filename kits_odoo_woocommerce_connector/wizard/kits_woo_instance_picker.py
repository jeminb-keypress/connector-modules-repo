# -*- coding: utf-8 -*-
from odoo import fields, models
from odoo.exceptions import UserError


class KitsWooInstancePicker(models.TransientModel):
    _name = 'kits.woo.instance.picker'
    _description = 'Choose WooCommerce Store for Record Sync'

    operation = fields.Selection(
        selection=[
            ('export', 'Export to WooCommerce'),
            ('import', 'Update from WooCommerce'),
        ],
        required=True,
    )
    instance_id = fields.Many2one(
        'kits.woo.instance',
        string='WooCommerce Store',
        required=True,
        domain="[('platform_type', '=', 'woocommerce'), ('active', '=', True)]",
        check_company=True,
    )
    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        required=True,
    )
    template_ids = fields.Many2many(
        'product.template',
        string='Products',
    )

    def action_confirm(self):
        self.ensure_one()
        if not self.template_ids:
            raise UserError(self.env._('Select at least one product.'))
        if self.operation == 'export':
            return self.template_ids._woo_enqueue_export(self.instance_id)
        return self.template_ids._woo_enqueue_import(self.instance_id)
