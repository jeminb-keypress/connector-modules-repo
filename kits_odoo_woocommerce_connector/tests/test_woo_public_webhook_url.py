# -*- coding: utf-8 -*-
from unittest.mock import MagicMock

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooPublicWebhookUrl(WooConnectorCommon):

    def setUp(self):
        super().setUp()
        self.instance.write({'public_webhook_url': False})

    def _set_web_base_url(self, url):
        ICP = self.env['ir.config_parameter'].sudo()
        if hasattr(ICP, 'set_str'):
            ICP.set_str('web.base.url', url)
        else:
            ICP.set_param('web.base.url', url)

    def test_empty_uses_get_base_url(self):
        self._set_web_base_url('https://odoo.from.base')
        self.assertEqual(
            self.instance._woo_get_public_base_url(),
            'https://odoo.from.base',
        )
        self.assertEqual(
            self.instance.get_base_url().rstrip('/'),
            'https://odoo.from.base',
        )

    def test_configured_overrides_get_base_url(self):
        self._set_web_base_url('https://odoo.from.base')
        self.instance.write({'public_webhook_url': 'https://tunnel.example/'})
        self.assertEqual(
            self.instance.public_webhook_url,
            'https://tunnel.example',
        )
        self.assertEqual(
            self.instance._woo_get_public_base_url(),
            'https://tunnel.example',
        )

    def test_normalization_strips_trailing_slash(self):
        self.instance.write({'public_webhook_url': 'https://odoo.example.com/'})
        self.assertEqual(self.instance.public_webhook_url, 'https://odoo.example.com')

    def test_routes_for_all_twelve_topics(self):
        self.instance.write({'public_webhook_url': 'https://odoo.example.com'})
        expected = {
            'product.created': f'https://odoo.example.com/kits_woo/webhook/{self.instance.id}/products',
            'product.updated': f'https://odoo.example.com/kits_woo/webhook/{self.instance.id}/products',
            'product.deleted': f'https://odoo.example.com/kits_woo/webhook/{self.instance.id}/products',
            'customer.created': f'https://odoo.example.com/kits_woo/webhook/{self.instance.id}/customers',
            'customer.updated': f'https://odoo.example.com/kits_woo/webhook/{self.instance.id}/customers',
            'customer.deleted': f'https://odoo.example.com/kits_woo/webhook/{self.instance.id}/customers',
            'order.created': f'https://odoo.example.com/kits_woo/webhook/{self.instance.id}/orders',
            'order.updated': f'https://odoo.example.com/kits_woo/webhook/{self.instance.id}/orders',
            'order.deleted': f'https://odoo.example.com/kits_woo/webhook/{self.instance.id}/orders',
            'coupon.created': f'https://odoo.example.com/kits_woo/webhook/{self.instance.id}/coupons',
            'coupon.updated': f'https://odoo.example.com/kits_woo/webhook/{self.instance.id}/coupons',
            'coupon.deleted': f'https://odoo.example.com/kits_woo/webhook/{self.instance.id}/coupons',
        }
        for topic, url in expected.items():
            self.assertEqual(self.instance._get_webhook_delivery_url(topic), url)

    def test_instances_are_independent(self):
        other = self.env['kits.woo.instance'].create({
            'name': 'Other Store URL',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://woo2.test',
            'woo_consumer_key': 'ck_2',
            'woo_consumer_secret': 'cs_2',
            'warehouse_id': self.warehouse.id if self.warehouse else False,
            'state': 'connected',
            'public_webhook_url': 'https://store-b.example',
        })
        self.instance.write({'public_webhook_url': 'https://store-a.example'})
        self.assertEqual(
            self.instance._get_webhook_delivery_url('order.created'),
            f'https://store-a.example/kits_woo/webhook/{self.instance.id}/orders',
        )
        self.assertEqual(
            other._get_webhook_delivery_url('order.created'),
            f'https://store-b.example/kits_woo/webhook/{other.id}/orders',
        )

    def test_invalid_url_rejected(self):
        with self.assertRaises(ValidationError):
            self.instance.write({'public_webhook_url': 'ftp://bad.example'})
        with self.assertRaises(ValidationError):
            self.instance.write({
                'public_webhook_url': 'https://odoo.example.com/path',
            })
        with self.assertRaises(ValidationError):
            self.instance.write({
                'public_webhook_url': 'https://odoo.example.com?x=1',
            })
        with self.assertRaises(ValidationError):
            self.instance.write({
                'public_webhook_url': 'https://odoo.example.com#frag',
            })

    def test_warnings_for_localhost_private_http(self):
        Instance = self.env['kits.woo.instance']
        self.assertIn(
            'localhost',
            Instance._woo_public_webhook_url_warning('http://127.0.0.1:2099').lower(),
        )
        self.assertIn(
            'http',
            Instance._woo_public_webhook_url_warning('http://odoo.example.com').lower(),
        )
        self.assertIn(
            'private',
            Instance._woo_public_webhook_url_warning('https://192.168.1.10').lower(),
        )
        self.instance.write({'public_webhook_url': 'http://127.0.0.1:2099'})
        self.assertTrue(self.instance.public_webhook_url_warning)

    def test_registration_uses_configured_url(self):
        self.instance.write({'public_webhook_url': 'https://pub.example'})
        api = MagicMock()
        api.create_webhook.side_effect = [
            {'id': 11, 'status': 'active', 'delivery_url': f'https://pub.example/kits_woo/webhook/{self.instance.id}/customers'},
            {'id': 12, 'status': 'active'},
            {'id': 13, 'status': 'active'},
        ]
        with self._patch_api(api):
            self.instance.write({'woo_webhook_customer_enabled': True})
        payload = api.create_webhook.call_args_list[0].args[0]
        self.assertEqual(
            payload['delivery_url'],
            f'https://pub.example/kits_woo/webhook/{self.instance.id}/customers',
        )

    def test_reenable_pushes_new_public_url_not_refresh(self):
        """Existing remote webhooks must PUT the new delivery URL on re-enable.

        Refresh-only would keep WooCommerce pointing at the previous public URL.
        """
        self.instance.write({'public_webhook_url': 'https://old.pub'})
        old_url = f'https://old.pub/kits_woo/webhook/{self.instance.id}/customers'
        new_url = f'https://new.pub/kits_woo/webhook/{self.instance.id}/customers'
        for topic, rid in (
            ('customer.created', '101'),
            ('customer.updated', '102'),
            ('customer.deleted', '103'),
        ):
            self.env['kits.woo.webhook'].create({
                'name': topic,
                'instance_id': self.instance.id,
                'topic': topic,
                'webhook_id': rid,
                'delivery_url': old_url,
                'secret': f'sec-{rid}',
                'status': 'active',
            })
        # Change public URL without toggling the switch (avoids unpatched API).
        self.instance.write({'public_webhook_url': 'https://new.pub'})
        api = MagicMock()
        api.update_webhook.side_effect = lambda wid, payload: {
            'id': wid,
            'status': 'active',
            'delivery_url': payload.get('delivery_url'),
        }
        with self._patch_api(api):
            # Re-enable path must PUT the new delivery URL, not refresh-only.
            self.assertTrue(self.instance._woo_enable_webhook_resource('customers'))
        self.assertEqual(api.update_webhook.call_count, 3)
        api.get_webhook.assert_not_called()
        api.create_webhook.assert_not_called()
        for call in api.update_webhook.call_args_list:
            self.assertEqual(call.args[1]['delivery_url'], new_url)
            self.assertEqual(call.args[1]['status'], 'active')
            self.assertIn(call.args[1]['secret'], ('sec-101', 'sec-102', 'sec-103'))
        records = self.env['kits.woo.webhook'].search([
            ('instance_id', '=', self.instance.id),
            ('topic', 'like', 'customer.%'),
        ])
        self.assertEqual(set(records.mapped('delivery_url')), {new_url})
        self.assertEqual(set(records.mapped('webhook_id')), {'101', '102', '103'})
        self.assertEqual(
            set(records.mapped('secret')),
            {'sec-101', 'sec-102', 'sec-103'},
        )

    def test_rebuild_store_a_does_not_touch_store_b(self):
        other = self.env['kits.woo.instance'].create({
            'name': 'Store B Rebuild Isolation',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://woo-b.test',
            'woo_consumer_key': 'ck_b',
            'woo_consumer_secret': 'cs_b',
            'warehouse_id': self.warehouse.id if self.warehouse else False,
            'state': 'connected',
            'public_webhook_url': 'https://store-b.example',
        })
        self.instance.write({'public_webhook_url': 'https://store-a-old.example'})
        wh_a = self.env['kits.woo.webhook'].create({
            'name': 'Order Created A',
            'instance_id': self.instance.id,
            'topic': 'order.created',
            'webhook_id': '201',
            'delivery_url': (
                f'https://store-a-old.example/kits_woo/webhook/{self.instance.id}/orders'
            ),
            'secret': 'sec-a',
            'status': 'active',
        })
        wh_b = self.env['kits.woo.webhook'].create({
            'name': 'Order Created B',
            'instance_id': other.id,
            'topic': 'order.created',
            'webhook_id': '202',
            'delivery_url': (
                f'https://store-b.example/kits_woo/webhook/{other.id}/orders'
            ),
            'secret': 'sec-b',
            'status': 'active',
        })
        self.instance.write({'public_webhook_url': 'https://store-a-new.example'})
        api = MagicMock()
        api.update_webhook.return_value = {
            'id': 201,
            'status': 'active',
            'delivery_url': (
                f'https://store-a-new.example/kits_woo/webhook/{self.instance.id}/orders'
            ),
        }
        with self._patch_api(api):
            self.instance.action_woo_rebuild_webhook_delivery_urls()
        api.update_webhook.assert_called_once()
        self.assertEqual(api.update_webhook.call_args.args[0], 201)
        wh_b.invalidate_recordset()
        self.assertEqual(
            wh_b.delivery_url,
            f'https://store-b.example/kits_woo/webhook/{other.id}/orders',
        )
        self.assertEqual(wh_b.webhook_id, '202')
        self.assertEqual(wh_b.secret, 'sec-b')
        self.assertEqual(
            wh_a.delivery_url,
            f'https://store-a-new.example/kits_woo/webhook/{self.instance.id}/orders',
        )

    def test_rebuild_updates_remote_preserves_id_and_secret(self):
        self.instance.write({'public_webhook_url': 'https://old.example'})
        webhook = self.env['kits.woo.webhook'].create({
            'name': 'Order Created',
            'instance_id': self.instance.id,
            'topic': 'order.created',
            'webhook_id': '55',
            'delivery_url': f'https://old.example/kits_woo/webhook/{self.instance.id}/orders',
            'secret': 'keep-me-secret',
            'status': 'active',
        })
        self.instance.write({'public_webhook_url': 'https://new.example'})
        api = MagicMock()
        api.update_webhook.return_value = {
            'id': 55,
            'status': 'active',
            'delivery_url': f'https://new.example/kits_woo/webhook/{self.instance.id}/orders',
        }
        with self._patch_api(api):
            self.instance.action_woo_rebuild_webhook_delivery_urls()
        api.update_webhook.assert_called_once()
        args, kwargs = api.update_webhook.call_args
        self.assertEqual(args[0], 55)
        self.assertEqual(
            args[1]['delivery_url'],
            f'https://new.example/kits_woo/webhook/{self.instance.id}/orders',
        )
        self.assertEqual(args[1]['secret'], 'keep-me-secret')
        self.assertEqual(webhook.webhook_id, '55')
        self.assertEqual(webhook.secret, 'keep-me-secret')
        self.assertEqual(
            webhook.delivery_url,
            f'https://new.example/kits_woo/webhook/{self.instance.id}/orders',
        )

    def test_rebuild_skips_unchanged_urls(self):
        self.instance.write({'public_webhook_url': 'https://same.example'})
        self.env['kits.woo.webhook'].create({
            'name': 'Product Updated',
            'instance_id': self.instance.id,
            'topic': 'product.updated',
            'webhook_id': '66',
            'delivery_url': f'https://same.example/kits_woo/webhook/{self.instance.id}/products',
            'secret': 'sec',
            'status': 'active',
        })
        api = MagicMock()
        with self._patch_api(api):
            self.instance.action_woo_rebuild_webhook_delivery_urls()
        api.update_webhook.assert_not_called()

    def test_rebuild_partial_failure_keeps_old_url(self):
        self.instance.write({'public_webhook_url': 'https://old.example'})
        webhook = self.env['kits.woo.webhook'].create({
            'name': 'Coupon Created',
            'instance_id': self.instance.id,
            'topic': 'coupon.created',
            'webhook_id': '77',
            'delivery_url': f'https://old.example/kits_woo/webhook/{self.instance.id}/coupons',
            'secret': 'sec-77',
            'status': 'paused',
        })
        self.instance.write({'public_webhook_url': 'https://new.example'})
        api = MagicMock()
        api.update_webhook.side_effect = Exception('Woo rejected URL')
        with self._patch_api(api):
            action = self.instance.action_woo_rebuild_webhook_delivery_urls()
        self.assertEqual(action.get('tag'), 'display_notification')
        self.assertEqual(action['params'].get('type'), 'danger')
        self.assertIn('coupon.created', action['params'].get('message', ''))
        webhook.invalidate_recordset()
        self.assertEqual(
            webhook.delivery_url,
            f'https://old.example/kits_woo/webhook/{self.instance.id}/coupons',
        )
        self.assertEqual(webhook.webhook_id, '77')
        self.assertEqual(webhook.secret, 'sec-77')
        self.assertTrue(webhook.last_error)

    def test_refresh_does_not_push_new_delivery_url(self):
        self.instance.write({'public_webhook_url': 'https://local.example'})
        webhook = self.env['kits.woo.webhook'].create({
            'name': 'Customer Updated',
            'instance_id': self.instance.id,
            'topic': 'customer.updated',
            'webhook_id': '88',
            'delivery_url': 'https://remote.example/old',
            'secret': 'sec',
            'status': 'active',
        })
        api = MagicMock()
        api.get_webhook.return_value = {
            'id': 88,
            'status': 'active',
            'delivery_url': 'https://remote.example/old',
        }
        with self._patch_api(api):
            self.instance.action_woo_refresh_webhooks()
        api.update_webhook.assert_not_called()
        self.assertEqual(webhook.delivery_url, 'https://remote.example/old')

    def test_settings_loads_and_saves_url_per_instance(self):
        other = self.env['kits.woo.instance'].create({
            'name': 'Settings URL Store',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://woo3.test',
            'woo_consumer_key': 'ck_3',
            'woo_consumer_secret': 'cs_3',
            'warehouse_id': self.warehouse.id if self.warehouse else False,
            'state': 'connected',
            'public_webhook_url': 'https://other.settings',
        })
        self.instance.write({'public_webhook_url': 'https://first.settings'})
        Settings = self.env['res.config.settings']
        settings = Settings.create({'woo_instance_id': self.instance.id})
        self.assertEqual(settings.public_webhook_url, 'https://first.settings')
        settings.woo_instance_id = other
        settings._onchange_woo_instance_id()
        self.assertEqual(settings.public_webhook_url, 'https://other.settings')
        settings.public_webhook_url = 'https://other.updated'
        settings.set_values()
        self.assertEqual(other.public_webhook_url, 'https://other.updated')
        self.assertEqual(self.instance.public_webhook_url, 'https://first.settings')
