# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.kits_odoo_base_connector.services.exceptions import ConnectorError

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooWebhookManagement(WooConnectorCommon):

    def setUp(self):
        super().setUp()
        self.instance.write({
            'woo_webhook_public_base_url': 'https://odoo.test',
        })

    def _mock_api(self):
        api = MagicMock()
        return api

    def test_switches_default_false(self):
        self.assertFalse(self.instance.woo_webhook_product_enabled)
        self.assertFalse(self.instance.woo_webhook_customer_enabled)
        self.assertFalse(self.instance.woo_webhook_order_enabled)
        self.assertFalse(self.instance.woo_webhook_coupon_enabled)

    def test_public_base_url_falls_back_to_web_base_url(self):
        ICP = self.env['ir.config_parameter'].sudo()
        if hasattr(ICP, 'set_str'):
            ICP.set_str('web.base.url', 'https://odoo.from.param')
        else:
            ICP.set_param('web.base.url', 'https://odoo.from.param')
        self.instance.with_context(
            kits_woo_webhook_skip_orchestrate=True,
        ).write({'public_webhook_url': False})
        self.assertEqual(
            self.instance._woo_get_public_base_url(),
            'https://odoo.from.param',
        )
        self.assertEqual(
            self.instance._get_webhook_delivery_url('customer.created'),
            f'https://odoo.from.param/kits_woo/webhook/{self.instance.id}/customers',
        )
        # Explicit override still wins over get_base_url().
        self.instance.with_context(
            kits_woo_webhook_skip_orchestrate=True,
        ).write({'public_webhook_url': 'https://tunnel.example'})
        self.assertEqual(
            self.instance._woo_get_public_base_url(),
            'https://tunnel.example',
        )

    def test_tmp_fields_mirror_stored(self):
        self.instance.with_context(
            kits_woo_webhook_skip_orchestrate=True,
        ).write({'woo_webhook_customer_enabled': True})
        self.instance.invalidate_recordset()
        self.assertTrue(self.instance.woo_webhook_customer_enabled_tmp)
        self.assertFalse(self.instance.woo_webhook_product_enabled_tmp)

    def test_unrelated_save_makes_no_api_calls(self):
        api = self._mock_api()
        with self._patch_api(api):
            self.instance.write({'name': 'Renamed Store'})
        api.create_webhook.assert_not_called()
        api.update_webhook.assert_not_called()
        api.get_webhook.assert_not_called()

    def test_enable_customers_creates_three_webhooks(self):
        api = self._mock_api()
        api.create_webhook.side_effect = [
            {'id': 101, 'status': 'active'},
            {'id': 102, 'status': 'active'},
            {'id': 103, 'status': 'active'},
        ]
        with self._patch_api(api):
            self.instance.write({'woo_webhook_customer_enabled': True})
        self.assertEqual(api.create_webhook.call_count, 3)
        self.assertTrue(self.instance.woo_webhook_customer_enabled)
        records = self.env['kits.woo.webhook'].search([
            ('instance_id', '=', self.instance.id),
        ])
        self.assertEqual(len(records), 3)
        self.assertEqual(
            set(records.mapped('topic')),
            {'customer.created', 'customer.updated', 'customer.deleted'},
        )
        secrets = set(records.mapped('secret'))
        self.assertEqual(len(secrets), 3)
        self.assertTrue(all(secrets))

    def test_unchanged_switch_no_api(self):
        api = self._mock_api()
        api.create_webhook.side_effect = [
            {'id': 1, 'status': 'active'},
            {'id': 2, 'status': 'active'},
            {'id': 3, 'status': 'active'},
        ]
        with self._patch_api(api):
            self.instance.write({'woo_webhook_customer_enabled': True})
        api.reset_mock()
        with self._patch_api(api):
            self.instance.write({
                'woo_webhook_customer_enabled_tmp': True,
                'name': 'Still Enabled',
            })
        api.create_webhook.assert_not_called()
        api.update_webhook.assert_not_called()

    def test_only_changed_resource_triggers_api(self):
        api = self._mock_api()
        api.create_webhook.side_effect = [
            {'id': 11, 'status': 'active'},
            {'id': 12, 'status': 'active'},
            {'id': 13, 'status': 'active'},
            {'id': 21, 'status': 'active'},
            {'id': 22, 'status': 'active'},
            {'id': 23, 'status': 'active'},
        ]
        with self._patch_api(api):
            self.instance.write({'woo_webhook_customer_enabled': True})
        customer_calls = api.create_webhook.call_count
        with self._patch_api(api):
            self.instance.write({'woo_webhook_product_enabled': True})
        self.assertEqual(api.create_webhook.call_count, customer_calls + 3)
        topics = [
            c.args[0]['topic'] for c in api.create_webhook.call_args_list[customer_calls:]
        ]
        self.assertTrue(all(t.startswith('product.') for t in topics))

    def test_disable_pauses_not_deletes(self):
        api = self._mock_api()
        api.create_webhook.side_effect = [
            {'id': 31, 'status': 'active'},
            {'id': 32, 'status': 'active'},
            {'id': 33, 'status': 'active'},
        ]
        api.update_webhook.return_value = {'id': 31, 'status': 'paused'}
        api.get_webhook.side_effect = lambda wid: {
            'id': wid, 'status': 'active',
        }
        with self._patch_api(api):
            self.instance.write({'woo_webhook_customer_enabled': True})
            self.instance.write({'woo_webhook_customer_enabled': False})
        self.assertFalse(self.instance.woo_webhook_customer_enabled)
        records = self.env['kits.woo.webhook'].search([
            ('instance_id', '=', self.instance.id),
        ])
        self.assertEqual(len(records), 3)
        self.assertTrue(all(records.mapped('webhook_id')))
        api.delete_webhook.assert_not_called()
        self.assertTrue(api.update_webhook.called)

    def test_reuse_existing_records_on_reenable(self):
        api = self._mock_api()
        api.create_webhook.side_effect = [
            {'id': 41, 'status': 'active'},
            {'id': 42, 'status': 'active'},
            {'id': 43, 'status': 'active'},
        ]
        api.update_webhook.return_value = {'status': 'active'}
        api.get_webhook.side_effect = lambda wid: {'id': wid, 'status': 'paused'}
        with self._patch_api(api):
            self.instance.write({'woo_webhook_coupon_enabled': True})
            self.instance.write({'woo_webhook_coupon_enabled': False})
            create_count = api.create_webhook.call_count
            self.instance.write({'woo_webhook_coupon_enabled': True})
        self.assertEqual(api.create_webhook.call_count, create_count)
        self.assertEqual(
            self.env['kits.woo.webhook'].search_count([
                ('instance_id', '=', self.instance.id),
            ]),
            3,
        )

    def test_partial_registration_keeps_successes_and_switch_off(self):
        api = self._mock_api()
        api.create_webhook.side_effect = [
            {'id': 51, 'status': 'active'},
            {'id': 52, 'status': 'active'},
            Exception('boom'),
        ]
        # Partial failure must not raise (raising would roll back sibling successes).
        with self._patch_api(api):
            self.instance.write({'woo_webhook_order_enabled': True})
        self.assertFalse(self.instance.woo_webhook_order_enabled)
        self.assertEqual(
            self.env['kits.woo.webhook'].search_count([
                ('instance_id', '=', self.instance.id),
                ('webhook_id', '!=', False),
            ]),
            2,
        )

    def test_unique_instance_topic_constraint(self):
        Webhook = self.env['kits.woo.webhook']
        Webhook.create({
            'name': 'A',
            'instance_id': self.instance.id,
            'topic': 'product.created',
            'delivery_url': 'https://odoo.test/kits_woo/webhook/1/products',
            'secret': 's1',
        })
        with self.assertRaises(Exception):
            Webhook.create({
                'name': 'B',
                'instance_id': self.instance.id,
                'topic': 'product.created',
                'delivery_url': 'https://odoo.test/kits_woo/webhook/1/products',
                'secret': 's2',
            })

    def test_individual_activate_stop_delete(self):
        api = self._mock_api()
        webhook = self.env['kits.woo.webhook'].create({
            'name': 'Order Created',
            'instance_id': self.instance.id,
            'topic': 'order.created',
            'webhook_id': '77',
            'delivery_url': self.instance.woo_webhook_resource_base_url + '/orders',
            'secret': 'sec-77',
            'status': 'paused',
        })
        api.update_webhook.return_value = {'id': 77, 'status': 'active'}
        with self._patch_api(api):
            webhook.action_activate()
        self.assertEqual(webhook.status, 'active')
        api.update_webhook.return_value = {'id': 77, 'status': 'paused'}
        with self._patch_api(api):
            webhook.action_stop()
        self.assertEqual(webhook.status, 'paused')
        with self._patch_api(api):
            webhook.action_delete_webhook()
        self.assertFalse(webhook.exists())
        api.delete_webhook.assert_called()

    def test_refresh_syncs_and_removes_missing(self):
        api = self._mock_api()
        keep = self.env['kits.woo.webhook'].create({
            'name': 'Keep',
            'instance_id': self.instance.id,
            'topic': 'product.updated',
            'webhook_id': '88',
            'delivery_url': 'https://odoo.test/x/products',
            'secret': 'a',
            'status': 'active',
        })
        gone = self.env['kits.woo.webhook'].create({
            'name': 'Gone',
            'instance_id': self.instance.id,
            'topic': 'product.deleted',
            'webhook_id': '89',
            'delivery_url': 'https://odoo.test/x/products',
            'secret': 'b',
            'status': 'active',
        })

        def get_webhook(wid):
            if int(wid) == 88:
                return {'id': 88, 'status': 'disabled'}
            raise ConnectorError('missing', status_code=404)

        api.get_webhook.side_effect = get_webhook
        with self._patch_api(api):
            self.instance.action_woo_refresh_webhooks()
        keep.invalidate_recordset()
        self.assertEqual(keep.status, 'disabled')
        self.assertFalse(gone.exists())

    def test_deactivate_clears_switches_and_deletes_remotes(self):
        api = self._mock_api()
        self.env['kits.woo.webhook'].create({
            'name': 'Cust',
            'instance_id': self.instance.id,
            'topic': 'customer.created',
            'webhook_id': '91',
            'delivery_url': 'https://odoo.test/x/customers',
            'secret': 'z',
            'status': 'active',
        })
        self.instance.with_context(
            kits_woo_webhook_skip_orchestrate=True,
        ).write({
            'woo_webhook_customer_enabled': True,
            'woo_auto_import_orders': True,
        })
        with self._patch_api(api):
            self.instance.write({'active': False})
        self.assertFalse(self.instance.woo_webhook_customer_enabled)
        self.assertFalse(self.instance.woo_auto_import_orders)
        self.assertEqual(
            self.env['kits.woo.webhook'].search_count([
                ('instance_id', '=', self.instance.id),
            ]),
            0,
        )
        api.delete_webhook.assert_called()
        # Reactivate does not recreate webhooks
        self.instance.write({'active': True})
        self.assertFalse(self.instance.woo_webhook_customer_enabled)
        self.assertEqual(
            self.env['kits.woo.webhook'].search_count([
                ('instance_id', '=', self.instance.id),
            ]),
            0,
        )

    def test_product_stock_protection(self):
        product_payload = {
            'id': 9001,
            'name': 'Stock Guard',
            'type': 'simple',
            'status': 'publish',
            'sku': 'SG-1',
            'regular_price': '10',
            'manage_stock': True,
            'stock_quantity': 999,
            'stock_status': 'instock',
        }
        job = self.env['kits.connector.sync.job'].create({
            'name': 'product webhook',
            'instance_id': self.instance.id,
            'job_type': 'woo_webhook_product',
            'payload': {
                'topic': 'product.updated',
                'woo_id': 9001,
                'product': product_payload,
            },
        })
        api = MagicMock()
        api.get_variations.return_value = iter([])
        with self._patch_api(api):
            self.instance._woo_process_product_webhook(job=job)
        row = self.env['kits.woo.product.waiting'].search([
            ('instance_id', '=', self.instance.id),
            ('external_id', '=', '9001'),
        ], limit=1)
        self.assertTrue(row)
        sent = row.payload or {}
        self.assertNotIn('stock_quantity', sent)
        self.assertNotIn('stock_status', sent)
        self.assertNotIn('manage_stock', sent)

    def test_name_topic_locked_after_remote_id(self):
        webhook = self.env['kits.woo.webhook'].create({
            'name': 'Locked',
            'instance_id': self.instance.id,
            'topic': 'coupon.created',
            'webhook_id': '55',
            'delivery_url': 'https://odoo.test/x/coupons',
            'secret': 's',
        })
        with self.assertRaises(UserError):
            webhook.write({'name': 'Changed'})
        with self.assertRaises(UserError):
            webhook.write({'topic': 'coupon.updated'})


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooWebhookManagementHttp(HttpCase):

    def setUp(self):
        super().setUp()
        warehouse = self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1,
        )
        self.instance = self.env['kits.woo.instance'].create({
            'name': 'HTTP Mgmt Webhook Store',
            'platform_type': 'woocommerce',
            'company_id': self.env.company.id,
            'base_url': 'https://woo.http.test',
            'woo_consumer_key': 'ck_http',
            'woo_consumer_secret': 'cs_http',
            'warehouse_id': warehouse.id if warehouse else False,
            'state': 'connected',
            'woo_webhook_public_base_url': 'http://127.0.0.1',
        })
        self.webhook = self.env['kits.woo.webhook'].create({
            'name': 'Customer Created',
            'instance_id': self.instance.id,
            'topic': 'customer.created',
            'webhook_id': '501',
            'delivery_url': (
                f'{self.instance.woo_webhook_resource_base_url}/customers'
            ),
            'secret': 'per-hook-secret',
            'status': 'active',
        })

    def _sign(self, body: bytes, secret: str = 'per-hook-secret') -> str:
        digest = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).digest()
        return base64.b64encode(digest).decode('ascii')

    def test_valid_signature_accepted(self):
        url = f'/kits_woo/webhook/{self.instance.id}/customers'
        payload = {
            'id': 880,
            'role': 'customer',
            'email': 'http-hook@example.com',
            'first_name': 'Http',
            'last_name': 'Hook',
            'billing': {},
            'shipping': {},
        }
        body = json.dumps(payload).encode()
        response = self.url_open(
            url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-WC-Webhook-Signature': self._sign(body),
                'X-WC-Webhook-Topic': 'customer.created',
                'X-WC-Webhook-ID': '501',
                'X-WC-Webhook-Source': 'https://woo.http.test',
                'X-WC-Webhook-Delivery-ID': 'del-ok-1',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'accepted', response.content)

    def test_invalid_signature_401(self):
        url = f'/kits_woo/webhook/{self.instance.id}/customers'
        body = b'{"id": 1}'
        response = self.url_open(
            url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-WC-Webhook-Signature': 'bad',
                'X-WC-Webhook-Topic': 'customer.created',
                'X-WC-Webhook-ID': '501',
                'X-WC-Webhook-Source': 'https://woo.http.test',
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_unknown_webhook_id_401(self):
        url = f'/kits_woo/webhook/{self.instance.id}/customers'
        body = b'{"id": 1}'
        response = self.url_open(
            url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-WC-Webhook-Signature': self._sign(body),
                'X-WC-Webhook-Topic': 'customer.created',
                'X-WC-Webhook-ID': '99999',
                'X-WC-Webhook-Source': 'https://woo.http.test',
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_invalid_source_401(self):
        url = f'/kits_woo/webhook/{self.instance.id}/customers'
        body = b'{"id": 1}'
        response = self.url_open(
            url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-WC-Webhook-Signature': self._sign(body),
                'X-WC-Webhook-Topic': 'customer.created',
                'X-WC-Webhook-ID': '501',
                'X-WC-Webhook-Source': 'https://evil.example',
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_delivery_id_idempotency(self):
        url = f'/kits_woo/webhook/{self.instance.id}/customers'
        payload = {
            'id': 881,
            'role': 'customer',
            'email': 'dup@example.com',
            'first_name': 'Dup',
            'last_name': 'Hook',
            'billing': {},
            'shipping': {},
        }
        body = json.dumps(payload).encode()
        headers = {
            'Content-Type': 'application/json',
            'X-WC-Webhook-Signature': self._sign(body),
            'X-WC-Webhook-Topic': 'customer.created',
            'X-WC-Webhook-ID': '501',
            'X-WC-Webhook-Source': 'https://woo.http.test',
            'X-WC-Webhook-Delivery-ID': 'same-delivery',
        }
        self.assertEqual(self.url_open(url, data=body, headers=headers).status_code, 200)
        jobs = self.env['kits.connector.sync.job'].search([
            ('instance_id', '=', self.instance._kits_instance_ref()),
            ('job_type', '=', 'woo_webhook_customer'),
        ])
        first_count = len(jobs)
        response = self.url_open(url, data=body, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'duplicate', response.content)
        self.assertEqual(
            self.env['kits.connector.sync.job'].search_count([
                ('instance_id', '=', self.instance._kits_instance_ref()),
                ('job_type', '=', 'woo_webhook_customer'),
            ]),
            first_count,
        )

    def test_paused_webhook_rejected(self):
        self.webhook.status = 'paused'
        url = f'/kits_woo/webhook/{self.instance.id}/customers'
        body = b'{"id": 1}'
        response = self.url_open(
            url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-WC-Webhook-Signature': self._sign(body),
                'X-WC-Webhook-Topic': 'customer.created',
                'X-WC-Webhook-ID': '501',
                'X-WC-Webhook-Source': 'https://woo.http.test',
            },
        )
        self.assertEqual(response.status_code, 403)
