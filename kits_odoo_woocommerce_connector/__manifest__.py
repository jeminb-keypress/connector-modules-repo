# -*- coding: utf-8 -*-
{
    'name': 'Kits Odoo WooCommerce Connector',
    'version': '20.0.1.11.0',
    'category': 'Sales/Sales',
    'summary': 'WooCommerce store sync for products, customers, orders, coupons, and inventory',
    'description': """
Kits Odoo WooCommerce Connector
===============================

Connect one or more WooCommerce stores to Odoo using the Kits base connector
framework.

Features:
* Multi-store configuration (URL, API keys, company, warehouse, pricelist)
* Catalog sync: categories (hierarchy), tags, attributes/values, products/variants
* Coupons import/export with per-instance mappings
* Payment gateway and shipping method mappings for order import
* Order import with cancel/refund-safe handling and tracking export
* Secure webhooks for customers, products, orders, and coupons (HMAC)
* Inventory export with Odoo as stock source of truth
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
        'delivery',
        'payment',
        'account',
    ],
    'data': [
        'data/product_data.xml',
        'data/ir_cron_data.xml',
        'security/ir.access.csv',
        'views/kits_connector_instance_views.xml',
        'views/kits_woo_coupon_views.xml',
        'views/kits_woo_refund_views.xml',
        'views/sale_order_views.xml',
        'views/product_template_views.xml',
        'views/res_partner_views.xml',
        'views/kits_woo_sync_wizard_views.xml',
        'views/kits_woocommerce_menus.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
    'post_init_hook': 'post_init_hook',
}
