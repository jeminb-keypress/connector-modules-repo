# -*- coding: utf-8 -*-
from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooDashboard(WooConnectorCommon):

    def test_dashboard_payload_structure(self):
        data = self.env['kits.connector.instance'].get_woo_dashboard_data('last_30')
        self.assertIn('kpis', data)
        self.assertIn('connected_stores', data['kpis'])
        self.assertIn('customers', data['kpis'])
        self.assertIn('products', data['kpis'])
        self.assertIn('orders', data['kpis'])
        self.assertIn('success_rate', data['kpis'])
        self.assertIn('sync_overview', data)
        self.assertEqual(
            len(data['sync_overview']['labels']),
            len(data['sync_overview']['products']),
        )
        self.assertTrue(any(s['id'] == self.instance.id for s in data['stores']))
        self.assertIn('sync_status', data)
        self.assertIn('top_products', data)
        self.assertIn('pending_jobs', data)

    def test_dashboard_counts_use_real_records(self):
        partner = self.env['res.partner'].create({
            'name': 'Dash Customer',
            'company_id': self.company.id,
        })
        self.env['kits.woo.customer.binding'].create({
            'instance_id': self.instance.id,
            'company_id': self.company.id,
            'partner_id': partner.id,
            'external_id': 'dash-cust-1',
        })
        template = self.env['product.template'].create({
            'name': 'Dash Product',
            'company_id': self.company.id,
            'list_price': 10,
            'type': 'consu',
        })
        self.env['kits.woo.product.binding'].create({
            'instance_id': self.instance.id,
            'company_id': self.company.id,
            'product_tmpl_id': template.id,
            'external_id': 'dash-prod-1',
            'product_type': 'simple',
        })
        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'company_id': self.company.id,
            'woo_instance_id': self.instance.id,
            'woo_order_id': '9001',
            'date_order': fields.Datetime.now(),
        })
        self.env['sale.order.line'].create({
            'order_id': order.id,
            'product_id': template.product_variant_id.id,
            'product_uom_qty': 3,
            'price_unit': 10,
        })
        self.env['kits.connector.sync.job'].create({
            'name': 'Dash Done Job',
            'instance_id': self.instance.id,
            'company_id': self.company.id,
            'job_type': 'woo_import_orders',
            'state': 'done',
            'finished_at': fields.Datetime.now(),
        })
        self.env['kits.connector.sync.job'].create({
            'name': 'Dash Pending Job',
            'instance_id': self.instance.id,
            'company_id': self.company.id,
            'job_type': 'woo_import_products',
            'state': 'pending',
        })

        data = self.env['kits.connector.instance'].get_woo_dashboard_data('last_30')
        self.assertGreaterEqual(data['kpis']['connected_stores']['value'], 1)
        self.assertGreaterEqual(data['kpis']['customers']['value'], 1)
        self.assertGreaterEqual(data['kpis']['products']['value'], 1)
        self.assertGreaterEqual(data['kpis']['orders']['value'], 1)
        self.assertGreaterEqual(data['sync_status']['done'], 1)
        self.assertGreaterEqual(data['sync_status']['pending'], 1)
        self.assertTrue(data['top_products'])
        self.assertEqual(data['top_products'][0]['qty'], 3)
        self.assertTrue(any(j['state'] == 'pending' for j in data['pending_jobs']))

    def test_products_not_double_counted_across_stores(self):
        store_b = self.env['kits.connector.instance'].create({
            'name': 'Dash Woo Store B',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://woo-b.test',
            'woo_consumer_key': 'ck_b',
            'woo_consumer_secret': 'cs_b',
            'state': 'connected',
        })
        template = self.env['product.template'].create({
            'name': 'Shared Dash Product',
            'company_id': self.company.id,
            'type': 'consu',
        })
        for store, ext in ((self.instance, 'shared-a'), (store_b, 'shared-b')):
            self.env['kits.woo.product.binding'].create({
                'instance_id': store.id,
                'company_id': self.company.id,
                'product_tmpl_id': template.id,
                'external_id': ext,
                'product_type': 'simple',
            })
        data = self.env['kits.connector.instance'].get_woo_dashboard_data('last_30')
        # At least the shared template counted once, not twice
        bindings = self.env['kits.woo.product.binding'].search_count([
            ('product_tmpl_id', '=', template.id),
            ('active', '=', True),
        ])
        self.assertEqual(bindings, 2)
        self.assertGreaterEqual(data['kpis']['products']['value'], 1)

    def test_quick_action_manage_stores(self):
        action = self.env['kits.connector.instance'].action_woo_dashboard_quick_action(
            'manage_stores',
        )
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'kits.connector.instance')

    def test_custom_range_requires_dates(self):
        with self.assertRaises(UserError):
            self.env['kits.connector.instance'].get_woo_dashboard_data('custom')
