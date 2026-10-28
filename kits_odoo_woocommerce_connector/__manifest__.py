# -*- coding: utf-8 -*-
{
    'name': 'Kits Odoo WooCommerce Connector',
    'version': '20.0.1.3.0',
    'category': 'Sales/Sales',
    'summary': 'WooCommerce store sync for products, customers, orders, and inventory',
    'description': """
Kits Odoo WooCommerce Connector
===============================

Connect one or more WooCommerce stores to Odoo using the Kits base connector
framework.

Features:
* Multi-store configuration (URL, API keys, company, warehouse, pricelist)
* WooCommerce REST API client (auth, pagination, rate limits, errors)
* Bidirectional product sync (simple + variants, multi-image gallery, tags, barcode)
* Dedicated customer import/export
* Sales order import with automatic confirmation and failure retry
* Inventory export from the configured warehouse
* Background jobs, retries, logging, and external ID bindings via base module
    """,
    'author': 'Kits',
    'license': 'LGPL-3',
    'depends': [
        'kits_odoo_base_connector',
        'sale_management',
        'sale_stock',
        'stock',
        'website_sale',
    ],
    'data': [
        'data/product_data.xml',
        'data/ir_cron_data.xml',
        'views/kits_connector_instance_views.xml',
        'views/sale_order_views.xml',
        'views/product_template_views.xml',
        'views/kits_woocommerce_menus.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
