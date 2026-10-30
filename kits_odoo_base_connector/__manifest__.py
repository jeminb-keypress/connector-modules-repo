# -*- coding: utf-8 -*-
{
    'name': 'Kits Odoo Base Connector',
    'version': '20.0.1.8.0',
    'category': 'Productivity',
    'summary': 'Reusable integration framework for marketplace and ERP connectors',
    'description': """
Kits Odoo Base Connector
========================

Shared infrastructure for platform connectors (WooCommerce, Shopify, Magento,
Amazon, QuickBooks, and others).

Provides:
* Connector instance / connection management
* Reusable HTTP API utilities with authentication hooks
* Background sync jobs with retries and scheduled processing
* Operation logging
* External ID mapping (bindings)
* Company-isolated security

Platform-specific business logic belongs in dependent modules such as
``kits_odoo_woocommerce_connector``.
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
        'views/kits_connector_instance_views.xml',
        'views/kits_connector_sync_job_views.xml',
        'views/kits_connector_log_views.xml',
        'views/kits_connector_binding_views.xml',
        'views/kits_connector_menus.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
