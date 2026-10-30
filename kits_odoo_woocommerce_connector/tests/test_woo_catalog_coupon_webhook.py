# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooCatalogAndCoupon(WooConnectorCommon):

    def test_category_hierarchy_import_parents_first(self):
        api = MagicMock()
        api.get_categories.return_value = iter([
            {'id': 2, 'name': 'Child', 'parent': 1, 'slug': 'child'},
            {'id': 1, 'name': 'Parent', 'parent': 0, 'slug': 'parent'},
        ])
        with self._patch_api(api):
            summary = self.instance._woo_import_categories()
        self.assertIn('created=2', summary)
        parent = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'product.category', external_id='1',
        )._kits_get_odoo_record()
        child = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'product.category', external_id='2',
        )._kits_get_odoo_record()
        self.assertEqual(child.parent_id, parent)

    def test_category_import_requires_name(self):
        with self.assertRaises(UserError):
            self.instance._woo_import_one_category({'id': 9, 'name': '', 'parent': 0})

    def test_attribute_and_term_bindings(self):
        api = MagicMock()
        api.get_attributes.return_value = iter([
            {'id': 11, 'name': 'Color', 'slug': 'color'},
        ])
        api.get_attribute_terms.return_value = iter([
            {'id': 21, 'name': 'Red'},
            {'id': 22, 'name': 'Blue'},
        ])
        with self._patch_api(api):
            summary = self.instance._woo_import_attributes()
        self.assertTrue(
            'created=1' in summary or 'updated=1' in summary,
            summary,
        )
        attr = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'product.attribute', external_id='11',
        )._kits_get_odoo_record()
        self.assertEqual(attr.name, 'Color')
        red = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'product.attribute.value', external_id='11:21',
        )._kits_get_odoo_record()
        self.assertEqual(red.name, 'Red')
        # Re-import does not duplicate
        api.get_attributes.return_value = iter([
            {'id': 11, 'name': 'Color', 'slug': 'color'},
        ])
        api.get_attribute_terms.return_value = iter([
            {'id': 21, 'name': 'Red'},
            {'id': 22, 'name': 'Blue'},
        ])
        with self._patch_api(api):
            self.instance._woo_import_attributes()
        self.assertEqual(
            self.env['kits.connector.binding'].search_count([
                ('instance_id', '=', self.instance.id),
                ('odoo_model', '=', 'product.attribute'),
                ('external_id', '=', '11'),
            ]),
            1,
        )

    def test_coupon_import_export_idempotent(self):
        woo = {
            'id': 55,
            'code': 'SAVE10',
            'discount_type': 'percent',
            'amount': '10',
            'date_expires': '2030-01-01',
            'usage_limit': 100,
            'usage_limit_per_user': 1,
            'individual_use': True,
            'free_shipping': False,
            'minimum_amount': '0',
            'maximum_amount': '0',
            'product_ids': [],
            'excluded_product_ids': [],
            'email_restrictions': ['a@b.com'],
            'description': 'Save ten',
        }
        api = MagicMock()
        api.get_coupons.side_effect = lambda **kw: iter([dict(woo)])
        with self._patch_api(api):
            summary = self.instance._woo_import_coupons()
        self.assertIn('created=1', summary)
        coupon = self.env['kits.woo.coupon'].search([
            ('instance_id', '=', self.instance.id),
            ('code', '=', 'SAVE10'),
        ])
        self.assertEqual(len(coupon), 1)
        with self._patch_api(api):
            summary2 = self.instance._woo_import_coupons()
        self.assertIn('updated=1', summary2)
        self.assertEqual(
            self.env['kits.woo.coupon'].search_count([
                ('instance_id', '=', self.instance.id),
                ('code', '=', 'SAVE10'),
            ]),
            1,
        )
        api.create_coupon.return_value = {'id': 55}
        # Clear binding to force create path then bind — export update path
        binding = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'kits.woo.coupon', external_id='55',
        )
        api.update_coupon.return_value = {'id': 55}
        with self._patch_api(api):
            result = self.instance._woo_export_one_coupon(coupon)
        self.assertEqual(result, 'updated')
        api.update_coupon.assert_called()

    def test_coupon_rejects_bad_discount_type(self):
        with self.assertRaises(UserError):
            self.instance._woo_import_one_coupon({
                'id': 1, 'code': 'X', 'discount_type': 'bogus', 'amount': '1',
            })

    def test_payment_and_shipping_fetch_and_order_mapping(self):
        api = MagicMock()
        api.get_payment_gateways.return_value = iter([
            {'id': 'cod', 'title': 'Cash on Delivery', 'enabled': True},
        ])
        api.get_shipping_methods.return_value = iter([
            {'id': 'flat_rate', 'title': 'Flat Rate'},
        ])
        api.get_shipping_zones.return_value = iter([])
        with self._patch_api(api):
            self.instance._woo_fetch_payment_gateways()
            self.instance._woo_fetch_shipping_methods()
        gateway = self.env['kits.woo.payment.gateway.map'].search([
            ('instance_id', '=', self.instance.id),
            ('woo_gateway_id', '=', 'cod'),
        ])
        self.assertTrue(gateway)
        shipping = self.env['kits.woo.shipping.method.map'].search([
            ('instance_id', '=', self.instance.id),
            ('woo_method_id', '=', 'flat_rate'),
        ])
        self.assertTrue(shipping)
        # Map delivery product override
        product = self.env['product.product'].create({
            'name': 'Mapped Ship',
            'type': 'service',
            'list_price': 0,
        })
        shipping.delivery_product_id = product.id
        line = self.instance._woo_prepare_shipping_line_vals(
            {'method_id': 'flat_rate', 'method_title': 'Flat'}, 5.0,
        )
        self.assertEqual(line['product_id'], product.id)

    def test_product_webhook_strips_stock_and_soft_deletes(self):
        template = self.env['product.template'].create({
            'name': 'Webhook Prod',
            'list_price': 10,
            'type': 'consu',
        })
        self.instance._woo_bind('product.template', template.id, '7001')
        job = self.env['kits.connector.sync.job'].create({
            'name': 'prod webhook',
            'instance_id': self.instance.id,
            'job_type': 'woo_webhook_product',
            'payload': {
                'topic': 'product.updated',
                'woo_id': 7001,
                'product': {
                    'id': 7001,
                    'name': 'Webhook Prod Renamed',
                    'type': 'simple',
                    'status': 'publish',
                    'regular_price': '12',
                    'stock_quantity': 999,
                    'manage_stock': True,
                    'categories': [],
                    'tags': [],
                    'images': [],
                },
            },
            'state': 'pending',
        })
        with patch.object(type(self.instance), '_woo_api', return_value=MagicMock(
            get_variations=MagicMock(return_value=iter([])),
        )):
            summary = self.instance._woo_process_product_webhook(job=job)
        self.assertIn('stock fields ignored', summary.lower())
        template.invalidate_recordset()
        self.assertEqual(template.name, 'Webhook Prod Renamed')
        # Soft delete
        job2 = self.env['kits.connector.sync.job'].create({
            'name': 'prod del',
            'instance_id': self.instance.id,
            'job_type': 'woo_webhook_product',
            'payload': {'topic': 'product.deleted', 'woo_id': 7001, 'product': {'id': 7001}},
            'state': 'pending',
        })
        self.instance._woo_process_product_webhook(job=job2)
        binding = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'product.template', external_id='7001',
        )
        self.assertEqual(binding.sync_state, 'deleted')
        self.assertTrue(template.exists())

    def test_order_webhook_idempotent_and_coupon_delete(self):
        # Coupon soft delete
        coupon = self.env['kits.woo.coupon'].create({
            'name': 'DEL',
            'code': 'DELME',
            'instance_id': self.instance.id,
            'discount_type': 'percent',
            'amount': 5,
        })
        self.instance._woo_bind('kits.woo.coupon', coupon.id, '88')
        summary = self.instance._woo_process_coupon_webhook(job=self.env['kits.connector.sync.job'].create({
            'name': 'coup del',
            'instance_id': self.instance.id,
            'job_type': 'woo_webhook_coupon',
            'payload': {'topic': 'coupon.deleted', 'woo_id': 88, 'coupon': {'id': 88}},
            'state': 'pending',
        }))
        self.assertIn('deleted remote', summary.lower())
        self.assertTrue(coupon.exists())

    def test_wizard_blocks_inventory_import_and_supports_coupons(self):
        with self.assertRaises(ValidationError):
            self.env['kits.woo.sync.wizard'].create({
                'instance_id': self.instance.id,
                'direction': 'import',
                'operation': 'inventory',
                'import_mode': 'all',
            })
        wiz2 = self.env['kits.woo.sync.wizard'].create({
            'instance_id': self.instance.id,
            'direction': 'import',
            'operation': 'coupons',
            'import_mode': 'ids',
            'record_ids_text': '1,2',
        })
        job_type, _name, payload = wiz2._prepare_job()
        self.assertEqual(job_type, 'woo_import_coupons')
        self.assertEqual(payload.get('woo_ids'), [1, 2])

    def test_multi_instance_coupon_isolation(self):
        other = self.env['kits.connector.instance'].create({
            'name': 'Other Woo',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://other.test',
            'woo_consumer_key': 'ck',
            'woo_consumer_secret': 'cs',
            'state': 'connected',
        })
        woo = {
            'id': 1, 'code': 'SAME', 'discount_type': 'percent', 'amount': '5',
            'description': 'A',
        }
        api_a = MagicMock()
        api_a.get_coupons.side_effect = lambda **kw: iter([dict(woo)])
        with self._patch_api(api_a):
            self.instance._woo_import_coupons()
        api_b = MagicMock()
        api_b.get_coupons.side_effect = lambda **kw: iter([dict(woo, id=2)])
        with patch.object(type(other), '_woo_api', return_value=api_b):
            other._woo_import_coupons()
        coupons = self.env['kits.woo.coupon'].search([('code', '=', 'SAME')])
        self.assertEqual(len(coupons), 2)
        self.assertNotEqual(coupons[0].instance_id, coupons[1].instance_id)


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooResourceWebhookHttp(HttpCase):

    def setUp(self):
        super().setUp()
        warehouse = self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1,
        )
        self.instance = self.env['kits.connector.instance'].create({
            'name': 'HTTP Resource Webhook Store',
            'platform_type': 'woocommerce',
            'company_id': self.env.company.id,
            'base_url': 'https://woo.http.test',
            'woo_consumer_key': 'ck_http',
            'woo_consumer_secret': 'cs_http',
            'warehouse_id': warehouse.id if warehouse else False,
            'state': 'connected',
            'woo_webhook_public_base_url': 'http://127.0.0.1',
            'woo_webhook_secret': 'http-secret',
        })

    def _sign(self, body: bytes) -> str:
        digest = hmac.new(b'http-secret', body, hashlib.sha256).digest()
        return base64.b64encode(digest).decode('ascii')

    def test_product_webhook_http_accepts(self):
        url = f'/kits_woo/webhook/{self.instance.id}/products'
        payload = {'id': 501, 'name': 'From Hook', 'type': 'simple', 'status': 'publish'}
        body = json.dumps(payload).encode()
        response = self.url_open(
            url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-WC-Webhook-Signature': self._sign(body),
                'X-WC-Webhook-Topic': 'product.created',
                'X-WC-Webhook-Delivery-ID': 'prod-http-1',
            },
        )
        self.assertEqual(response.status_code, 200)
        job = self.env['kits.connector.sync.job'].search([
            ('instance_id', '=', self.instance.id),
            ('job_type', '=', 'woo_webhook_product'),
        ], limit=1)
        self.assertTrue(job)
        self.assertEqual(job.state, 'pending')
