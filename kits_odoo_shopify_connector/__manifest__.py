# -*- coding: utf-8 -*-
{
    'name': 'Kits Odoo Shopify Connector',
    'version': '20.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Connect Shopify stores and test the Admin GraphQL API',
    'description': """
Kits Odoo Shopify Connector
===========================

Connect one or more Shopify stores to Odoo using the Kits base connector.

This version configures the shop domain, Admin API access token, and API
version, and tests connectivity with the Shopify GraphQL Admin API.
    """,
    'author': 'Kits',
    'license': 'LGPL-3',
    'depends': [
        'kits_odoo_base_connector',
    ],
    'data': [
        'views/kits_connector_instance_views.xml',
        'views/kits_shopify_menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
