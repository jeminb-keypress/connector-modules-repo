# -*- coding: utf-8 -*-
from unittest.mock import MagicMock

from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooResConfigSettings(WooConnectorCommon):
    """Centralized WooCommerce Settings app page."""

    def setUp(self):
        super().setUp()
        self.instance.write({
            'woo_webhook_public_base_url': 'https://odoo.test',
        })
        self.Settings = self.env['res.config.settings']

    def _create_second_instance(self):
        return self.env['kits.woo.instance'].create({
            'name': 'Second Woo Store',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://woo2.test',
            'woo_consumer_key': 'ck_test2',
            'woo_consumer_secret': 'cs_test2',
            'warehouse_id': self.warehouse.id if self.warehouse else False,
            'woo_import_images': False,
            'state': 'connected',
            'woo_webhook_public_base_url': 'https://odoo.test',
        })

    def test_settings_fields_exist_and_default_empty(self):
        settings = self.Settings.create({})
        self.assertFalse(settings.woo_instance_id)
        self.assertFalse(settings.woo_webhook_product_enabled)
        self.assertFalse(settings.woo_webhook_customer_enabled)
        self.assertFalse(settings.woo_webhook_order_enabled)
        self.assertFalse(settings.woo_webhook_coupon_enabled)

    def test_selecting_instance_loads_switch_values(self):
        self.instance.with_context(
            kits_woo_webhook_skip_orchestrate=True,
        ).write({
            'woo_webhook_product_enabled': True,
            'woo_webhook_customer_enabled': False,
            'woo_webhook_order_enabled': True,
            'woo_webhook_coupon_enabled': False,
        })
        settings = self.Settings.create({
            'woo_instance_id': self.instance.id,
        })
        self.assertTrue(settings.woo_webhook_product_enabled)
        self.assertFalse(settings.woo_webhook_customer_enabled)
        self.assertTrue(settings.woo_webhook_order_enabled)
        self.assertFalse(settings.woo_webhook_coupon_enabled)

    def test_switching_instances_shows_independent_values(self):
        other = self._create_second_instance()
        self.instance.with_context(
            kits_woo_webhook_skip_orchestrate=True,
        ).write({
            'woo_webhook_customer_enabled': True,
            'woo_webhook_product_enabled': False,
        })
        other.with_context(
            kits_woo_webhook_skip_orchestrate=True,
        ).write({
            'woo_webhook_customer_enabled': False,
            'woo_webhook_product_enabled': True,
        })

        settings = self.Settings.create({
            'woo_instance_id': self.instance.id,
        })
        self.assertTrue(settings.woo_webhook_customer_enabled)
        self.assertFalse(settings.woo_webhook_product_enabled)

        # Simulate UI instance change (onchange loads the other store).
        settings.woo_instance_id = other
        settings._onchange_woo_instance_id()
        self.assertFalse(settings.woo_webhook_customer_enabled)
        self.assertTrue(settings.woo_webhook_product_enabled)

    def test_save_updates_only_selected_instance(self):
        other = self._create_second_instance()
        api = MagicMock()
        api.create_webhook.side_effect = [
            {'id': 201, 'status': 'active'},
            {'id': 202, 'status': 'active'},
            {'id': 203, 'status': 'active'},
        ]
        settings = self.Settings.create({
            'woo_instance_id': self.instance.id,
            'woo_webhook_customer_enabled': True,
        })
        with self._patch_api(api):
            settings.set_values()

        self.assertTrue(self.instance.woo_webhook_customer_enabled)
        self.assertFalse(other.woo_webhook_customer_enabled)
        self.assertEqual(api.create_webhook.call_count, 3)

    def test_unrelated_settings_save_no_api_when_instance_empty(self):
        api = MagicMock()
        settings = self.Settings.create({})
        with self._patch_api(api):
            settings.execute()
        api.create_webhook.assert_not_called()
        api.update_webhook.assert_not_called()

    def test_unchanged_switches_on_save_no_api(self):
        self.instance.with_context(
            kits_woo_webhook_skip_orchestrate=True,
        ).write({'woo_webhook_customer_enabled': True})
        api = MagicMock()
        settings = self.Settings.create({
            'woo_instance_id': self.instance.id,
        })
        # Related mirrors True; saving without change must not call API.
        self.assertTrue(settings.woo_webhook_customer_enabled)
        with self._patch_api(api):
            settings.set_values()
        api.create_webhook.assert_not_called()
        api.update_webhook.assert_not_called()
        api.get_webhook.assert_not_called()

    def test_create_instance_action(self):
        settings = self.Settings.create({})
        action = settings.action_woo_create_instance()
        self.assertEqual(action['res_model'], 'kits.woo.instance')
        self.assertEqual(action['view_mode'], 'form')
        self.assertEqual(
            action['context'].get('default_platform_type'),
            'woocommerce',
        )

    def test_instance_domain_respects_company_acl(self):
        other_company = self.env['res.company'].create({
            'name': 'Other Co Woo Settings',
        })
        foreign = self.env['kits.woo.instance'].sudo().create({
            'name': 'Foreign Store',
            'platform_type': 'woocommerce',
            'company_id': other_company.id,
            'base_url': 'https://foreign.woo.test',
            'woo_consumer_key': 'ck_x',
            'woo_consumer_secret': 'cs_x',
            'state': 'connected',
        })
        # Superuser bypasses company domains; use a company-restricted manager.
        user = self.env['res.users'].create({
            'name': 'Woo Settings User',
            'login': 'woo_settings_acl_user',
            'company_id': self.company.id,
            'company_ids': [(6, 0, [self.company.id])],
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref(
                    'kits_odoo_base_connector.group_kits_connector_manager',
                ).id,
            ])],
        })
        # Settings picker lists kits.woo.instance under the same company ACL.
        Instance = self.env['kits.woo.instance'].with_user(user)
        accessible = Instance.search([
            ('platform_type', '=', 'woocommerce'),
        ])
        self.assertIn(self.instance, accessible)
        self.assertNotIn(foreign, accessible)

    def test_settings_view_and_action_defined(self):
        view = self.env.ref(
            'kits_odoo_woocommerce_connector.res_config_settings_view_form',
        )
        self.assertEqual(view.model, 'res.config.settings')
        action = self.env['ir.actions.act_window']._for_xml_id(
            'kits_odoo_woocommerce_connector.kits_woo_action_config_settings',
        )
        self.assertEqual(action['res_model'], 'res.config.settings')
        context = action.get('context') or {}
        if isinstance(context, str):
            self.assertIn('kits_odoo_woocommerce_connector', context)
        else:
            self.assertEqual(
                context.get('module'),
                'kits_odoo_woocommerce_connector',
            )

    def test_reenable_after_delete_sets_company_id(self):
        """company_id precompute must allow create after local delete."""
        api = MagicMock()
        api.create_webhook.side_effect = [
            {'id': 301, 'status': 'active'},
            {'id': 302, 'status': 'active'},
            {'id': 303, 'status': 'active'},
        ]
        with self._patch_api(api):
            self.instance.write({'woo_webhook_customer_enabled': True})
        records = self.env['kits.woo.webhook'].search([
            ('instance_id', '=', self.instance.id),
        ])
        self.assertEqual(len(records), 3)
        self.assertTrue(all(records.mapped('company_id')))

        api.delete_webhook = MagicMock()
        with self._patch_api(api):
            for rec in records:
                rec.action_delete_webhook()
        self.assertFalse(self.env['kits.woo.webhook'].search([
            ('instance_id', '=', self.instance.id),
        ]))
        self.instance.with_context(
            kits_woo_webhook_skip_orchestrate=True,
        ).write({'woo_webhook_customer_enabled': False})

        api.create_webhook.side_effect = [
            {'id': 401, 'status': 'active'},
            {'id': 402, 'status': 'active'},
            {'id': 403, 'status': 'active'},
        ]
        with self._patch_api(api):
            self.instance.write({'woo_webhook_customer_enabled': True})
        recreated = self.env['kits.woo.webhook'].search([
            ('instance_id', '=', self.instance.id),
        ])
        self.assertEqual(len(recreated), 3)
        self.assertEqual(
            set(recreated.mapped('company_id')),
            {self.company},
        )
