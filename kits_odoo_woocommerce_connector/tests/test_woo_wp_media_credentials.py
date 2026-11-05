# -*- coding: utf-8 -*-
from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooWpMediaCredentials(WooConnectorCommon):

    def test_set_and_display_wp_media_credentials(self):
        self.instance._woo_set_wp_media_credentials('wp-user', 'secret-app-pass')
        user, password = self.instance._woo_get_wp_media_credentials()
        self.assertEqual(user, 'wp-user')
        self.assertEqual(password, 'secret-app-pass')
        self.assertEqual(
            (self.instance.sudo().config_json or {}).get('woo_wp_username'),
            'wp-user',
        )
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.woo_wp_username_display, 'wp-user')
        self.assertEqual(self.instance.woo_wp_password_display, '********')

    def test_set_preserves_url_mode(self):
        self.instance.sudo().write({
            'config_json': {'woo_url_mode': 'index_php', 'woo_wp_username': 'old'},
        })
        self.instance._woo_set_wp_media_credentials('new-user', 'new-pass')
        cfg = self.instance.sudo().config_json or {}
        self.assertEqual(cfg.get('woo_url_mode'), 'index_php')
        self.assertEqual(cfg.get('woo_wp_username'), 'new-user')
        self.assertEqual(cfg.get('woo_wp_password'), 'new-pass')

    def test_api_version_forced_to_v3(self):
        self.assertEqual(self.instance._woo_api_version_path(), 'wc/v3')
        self.assertTrue(self.instance._kits_get_base_url().endswith('/wp-json/wc/v3'))

    def test_timeout_uses_default(self):
        self.instance.timeout = 5
        self.assertEqual(self.instance._kits_get_timeout(), 30)

    def test_wizard_save_and_open_action(self):
        wizard = self.env['kits.woo.wp.media.credentials'].create({
            'instance_id': self.instance.id,
            'username': 'u',
            'password': 'p',
        })
        wizard.action_save()
        user, password = self.instance._woo_get_wp_media_credentials()
        self.assertEqual(user, 'u')
        self.assertEqual(password, 'p')
        action = self.instance.action_woo_open_wp_media_credentials()
        self.assertEqual(action['res_model'], 'kits.woo.wp.media.credentials')
        self.assertEqual(action['context']['default_instance_id'], self.instance.id)
