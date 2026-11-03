# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    from odoo.addons.kits_odoo_base_connector.hooks import kits_remove_connector_app_menus
    kits_remove_connector_app_menus(env)
