# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch

from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooCustomerSync(WooConnectorCommon):

    def test_import_same_email_different_instance_reuses_partner(self):
        """The same email across stores reuses one contact and adds a binding per store."""
        other = self.env['kits.connector.instance'].create({
            'name': 'Woo Store B',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'http://store-b.example',
            'woo_consumer_key': 'ck_b',
            'woo_consumer_secret': 'cs_b',
            'state': 'connected',
        })
        woo_customer = {
            'id': 101,
            'email': 'shared@example.com',
            'first_name': 'Shared',
            'last_name': 'Buyer',
            'billing': {
                'first_name': 'Shared',
                'last_name': 'Buyer',
                'email': 'shared@example.com',
                'phone': '111',
                'address_1': 'A St',
                'city': 'Town',
                'postcode': '1000',
                'country': 'US',
            },
            'shipping': {},
        }
        api_a = MagicMock()
        api_a.get_customers.return_value = iter([woo_customer])
        with self._patch_api(api_a):
            self.instance._woo_import_customers()
        partner_a = self.env['kits.woo.customer.binding']._find(
            self.instance, external_id='101',
        ).partner_id
        self.assertTrue(partner_a)
        self.assertEqual(partner_a.email, 'shared@example.com')

        woo_b = dict(woo_customer, id=202)
        api_b = MagicMock()
        api_b.get_customers.return_value = iter([woo_b])
        with patch.object(type(other), '_woo_api', return_value=api_b):
            other._woo_import_customers()
        partner_b = self.env['kits.woo.customer.binding']._find(
            other, external_id='202',
        ).partner_id
        self.assertEqual(partner_a, partner_b)
        self.assertEqual(partner_a.name, 'Shared Buyer')
        self.assertEqual(
            self.env['kits.woo.customer.binding'].search_count([
                ('partner_id', '=', partner_a.id),
            ]),
            2,
        )

    def test_reimport_same_instance_id_updates_existing(self):
        partner = self.env['res.partner'].create({
            'name': 'Old Name',
            'email': 'reimport@example.com',
            'company_id': self.company.id,
        })
        self.instance._woo_bind('res.partner', partner.id, '77')
        remote = {
            'id': 77,
            'email': 'reimport@example.com',
            'first_name': 'New',
            'last_name': 'Name',
            'billing': {'email': 'reimport@example.com', 'phone': '999'},
            'shipping': {},
        }
        api = MagicMock()
        api.get_customer.return_value = remote
        job = self.env['kits.connector.sync.job'].create({
            'name': 'reimport customer',
            'instance_id': self.instance.id,
            'job_type': 'woo_import_customers',
            'payload': {'woo_ids': [77]},
            'state': 'pending',
        })
        with self._patch_api(api):
            summary = self.instance._woo_import_customers(job=job)
        self.assertIn('reused=1', summary)
        partner.invalidate_recordset()
        self.assertEqual(partner.name, 'Old Name')
        self.assertFalse(partner.phone)
        bindings = self.env['kits.woo.customer.binding'].search([
            ('instance_id', '=', self.instance.id),
            ('external_id', '=', '77'),
        ])
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings.partner_id, partner)

    def test_fetch_creates_then_updates_without_duplicate(self):
        remote = {
            'id': 88,
            'email': 'fetch@example.com',
            'first_name': 'Fetch',
            'last_name': 'One',
            'billing': {'email': 'fetch@example.com'},
            'shipping': {},
        }
        api = MagicMock()
        api.get_customer.return_value = remote
        with self._patch_api(api):
            partner1 = self.instance._woo_fetch_customer_by_id(88)
            partner2 = self.instance._woo_fetch_customer_by_id(88)
        self.assertEqual(partner1.id, partner2.id)
        self.assertEqual(
            self.env['kits.woo.customer.binding'].search_count([
                ('instance_id', '=', self.instance.id),
                ('external_id', '=', '88'),
            ]),
            1,
        )

    def test_fetch_reuses_unbound_email_match_without_changing_it(self):
        existing = self.env['res.partner'].create({
            'name': 'Local Only',
            'email': 'local@example.com',
            'company_id': self.company.id,
        })
        remote = {
            'id': 99,
            'email': 'local@example.com',
            'first_name': 'Remote',
            'last_name': 'Twin',
            'billing': {'email': 'local@example.com'},
            'shipping': {},
        }
        api = MagicMock()
        api.get_customer.return_value = remote
        with self._patch_api(api):
            fetched = self.instance._woo_fetch_customer_by_id(99)
        self.assertEqual(fetched, existing)
        self.assertEqual(existing.name, 'Local Only')

    def test_guest_order_does_not_bind_customer_zero(self):
        woo_order = {
            'id': 700,
            'customer_id': 0,
            'billing': {
                'first_name': 'Guest',
                'last_name': 'User',
                'email': 'guest@example.com',
                'address_1': '1 Main',
                'city': 'Town',
                'postcode': '2000',
                'country': 'US',
            },
            'shipping': {},
        }
        partner = self.instance._woo_partner_from_order(woo_order)
        self.assertTrue(partner)
        self.assertEqual(partner.email, 'guest@example.com')
        bindings = self.env['kits.woo.customer.binding'].search([
            ('instance_id', '=', self.instance.id),
            ('partner_id', '=', partner.id),
        ])
        self.assertFalse(bindings)

    def test_partner_connected_instances_compute(self):
        partner = self.env['res.partner'].create({
            'name': 'Linked',
            'email': 'linked@example.com',
            'company_id': self.company.id,
        })
        self.assertFalse(partner.kits_connected_instance_ids)
        self.instance._woo_bind('res.partner', partner.id, '12')
        partner.invalidate_recordset()
        self.assertIn(self.instance, partner.kits_connected_instance_ids)
        self.assertEqual(partner.kits_binding_count, 1)
        self.assertEqual(partner.woo_sync_state, 'synced')
