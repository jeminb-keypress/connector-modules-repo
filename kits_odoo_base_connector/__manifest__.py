# -*- coding: utf-8 -*-
{
    'name': 'Kits Odoo Base Connector',
    'version': '20.0.1.10.1',
    'category': 'Productivity',
    'summary': 'Common Connector Library for marketplace and ERP integrations',
    'description': """
Kits Odoo Base Connector — Common Connector Library
===================================================

Platform-independent reusable infrastructure for store connectors
(WooCommerce, Shopify, and future platforms).

Provides:
* Abstract instance mixin (no concrete store/instance model)
* Reusable HTTP API client, exceptions, and auth hooks
* Background sync jobs with retries and scheduled processing
* Operation logging with generic instance references
* External ID bindings with generic instance references
* Waiting-list, contact, order, product, stock, and dashboard helpers
* Company-isolated security

Platform modules own their concrete instance models, credentials,
connection UI, and platform-specific business logic.
    """,
    'author': 'Kits',
    'website': '',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
    ],
    'data': [
        'security/kits_connector_security.xml',
        'security/ir.access.csv',
        'data/ir_cron_data.xml',
        'views/kits_connector_sync_job_views.xml',
        'views/kits_connector_log_views.xml',
        'views/kits_connector_binding_views.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
