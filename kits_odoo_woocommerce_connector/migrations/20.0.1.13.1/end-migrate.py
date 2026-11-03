# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Drop the generic WooCommerce Bindings menu. Dedicated binding menus remain."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    data = env['ir.model.data'].search([
        ('module', '=', 'kits_odoo_woocommerce_connector'),
        ('name', 'in', ['kits_woo_menu_bindings', 'kits_woo_binding_action']),
    ])
    for row in data:
        record = env[row.model].browse(row.res_id).exists()
        if record:
            record.unlink()
    data.unlink()
