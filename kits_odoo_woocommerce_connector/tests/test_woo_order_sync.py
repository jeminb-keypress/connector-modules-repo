# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch

from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooOrderSync(WooConnectorCommon):

    def _bound_product(self, woo_id=501, sku='ORD-SKU-1'):
        template = self.env['product.template'].create({
            'name': f'Order Product {sku}',
            'default_code': sku,
            'type': 'consu',
            'is_storable': True,
            'list_price': 25.0,
            'sale_ok': True,
            'company_id': self.company.id,
        })
        variant = template.product_variant_id
        self.instance._woo_bind('product.template', template.id, woo_id)
        self.instance._woo_bind('product.product', variant.id, woo_id, external_data={
            'sku': sku, 'parent_id': False,
        })
        return variant

    def _sample_order(self, order_id=800, product_id=501, status='processing', date='2026-01-01T10:00:00'):
        return {
            'id': order_id,
            'number': str(order_id),
            'status': status,
            'currency': 'USD',
            'customer_id': 0,
            'date_created': date,
            'date_created_gmt': date,
            'prices_include_tax': False,
            'discount_total': '5.00',
            'total': '30.00',
            'billing': {
                'first_name': 'Ada',
                'last_name': 'Lovelace',
                'email': f'ada{order_id}@example.com',
                'address_1': '42 Analytical',
                'city': 'London',
                'postcode': 'EC1',
                'country': 'GB',
            },
            'shipping': {
                'first_name': 'Ada',
                'last_name': 'Lovelace',
                'address_1': '42 Analytical',
                'city': 'London',
                'postcode': 'EC1',
                'country': 'GB',
            },
            'line_items': [{
                'id': 1,
                'name': 'Order Product',
                'product_id': product_id,
                'variation_id': 0,
                'quantity': 1,
                'price': 25.0,
                'sku': 'ORD-SKU-1',
                'total': '25.00',
            }],
            'shipping_lines': [{
                'method_title': 'Flat Rate',
                'total': '10.00',
            }],
            'fee_lines': [],
            'coupon_lines': [{'code': 'SAVE5'}],
            'tax_lines': [],
        }

    def test_order_import_duplicate_prevention(self):
        self._bound_product()
        order_payload = self._sample_order()
        api = MagicMock()
        api.get_orders.return_value = iter([order_payload])
        api.get_order.return_value = order_payload

        with self._patch_api(api):
            summary1 = self.instance._woo_import_orders()
            api.get_orders.return_value = iter([order_payload])
            summary2 = self.instance._woo_import_orders()

        self.assertIn('created=1', summary1)
        self.assertIn('skipped=1', summary2)
        orders = self.env['sale.order'].search([
            ('woo_order_id', '=', '800'),
            ('woo_instance_id', '=', self.instance.id),
        ])
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders.state, 'sale')
        # shipping + product + discount
        self.assertGreaterEqual(len(orders.order_line), 3)

    def test_unmapped_product_fails_by_default(self):
        order_payload = self._sample_order(order_id=801, product_id=99999)
        order_payload['line_items'][0]['sku'] = 'MISSING'
        api = MagicMock()
        api.get_orders.return_value = iter([order_payload])

        with self._patch_api(api):
            summary = self.instance._woo_import_orders()

        self.assertIn('failed=1', summary)
        self.assertIn(801, self.instance._woo_order_failure_ids())
        self.assertFalse(self.env['sale.order'].search([
            ('woo_order_id', '=', '801'),
        ]))

    def test_failed_order_retry_by_id_after_checkpoint(self):
        product = self._bound_product()
        # First run: fail unmapped, advance watermark via a later success
        bad = self._sample_order(order_id=810, product_id=99999, date='2026-02-01T10:00:00')
        bad['line_items'][0]['sku'] = 'MISSING'
        good = self._sample_order(order_id=811, product_id=501, date='2026-02-02T10:00:00')

        api = MagicMock()
        api.get_orders.return_value = iter([bad, good])
        with self._patch_api(api):
            summary1 = self.instance._woo_import_orders()
        self.assertIn('created=1', summary1)
        self.assertIn(810, self.instance._woo_order_failure_ids())
        self.assertTrue(self.instance.woo_last_order_sync)

        # Fix mapping and retry: bind missing product id, clear sku issue by using bound product
        self.instance._woo_bind('product.product', product.id, '99999')
        bad['line_items'][0]['product_id'] = 99999
        bad['line_items'][0]['sku'] = product.default_code
        api.get_orders.return_value = iter([])  # watermark already past
        api.get_order.return_value = bad
        with self._patch_api(api):
            summary2 = self.instance._woo_import_orders()

        self.assertIn('retried_ok=1', summary2)
        self.assertNotIn(810, self.instance._woo_order_failure_ids())
        self.assertTrue(self.env['sale.order'].search([('woo_order_id', '=', '810')]))

    def test_multi_store_bindings_isolated(self):
        other = self.env['kits.connector.instance'].create({
            'name': 'Other Woo Store',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://other.woo.test',
            'woo_consumer_key': 'ck_other',
            'woo_consumer_secret': 'cs_other',
            'warehouse_id': self.warehouse.id if self.warehouse else False,
            'woo_import_images': False,
            'state': 'connected',
        })
        self._bound_product(woo_id=501)
        # Same external order id on two stores must create two Odoo orders
        payload_a = self._sample_order(order_id=820)
        payload_b = self._sample_order(order_id=820)
        payload_b['billing']['email'] = 'other820@example.com'

        api_a = MagicMock()
        api_a.get_orders.return_value = iter([payload_a])
        api_b = MagicMock()
        api_b.get_orders.return_value = iter([payload_b])

        with self._patch_api(api_a):
            self.instance._woo_import_orders()
        with patch.object(type(other), '_woo_api', return_value=api_b):
            other._woo_import_orders()

        orders = self.env['sale.order'].search([('woo_order_id', '=', '820')])
        self.assertEqual(len(orders), 2)
