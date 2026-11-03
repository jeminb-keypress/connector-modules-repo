# -*- coding: utf-8 -*-
"""Upgrade helpers for the base connector."""

# Old Connectors app menus. Leaves first, then parents, so restrict deletes succeed
# once platform modules have moved their own items off these parents.
_BASE_MENU_XMLIDS = (
    'kits_odoo_base_connector.kits_connector_menu_instances',
    'kits_odoo_base_connector.kits_connector_menu_sync_jobs',
    'kits_odoo_base_connector.kits_connector_menu_logs',
    'kits_odoo_base_connector.kits_connector_menu_bindings',
    'kits_odoo_base_connector.kits_connector_menu_config_instances',
    'kits_odoo_base_connector.kits_connector_menu_config',
    'kits_odoo_base_connector.kits_connector_menu_root',
)


def kits_remove_connector_app_menus(env):
    """Remove the generic Connectors app menus when they have no children left."""
    data_model = env['ir.model.data'].sudo()
    for xmlid in _BASE_MENU_XMLIDS:
        menu = env.ref(xmlid, raise_if_not_found=False)
        if menu and menu.child_id:
            continue
        if menu:
            menu.unlink()
        module, name = xmlid.split('.', 1)
        data_model.search([
            ('module', '=', module),
            ('name', '=', name),
            ('model', '=', 'ir.ui.menu'),
        ]).unlink()
