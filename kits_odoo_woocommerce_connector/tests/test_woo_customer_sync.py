# -*- coding: utf-8 -*-
from unittest.mock import MagicMock

from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooCustomerSync(WooConnectorCommon):

    def test_customer_import_export_and_email_conflict(self):
        existing = self.env['res.partner'].create({
            'name': 'Existing Buyer',
            'email': 'buyer@example.com',
            'company_id': self.company.id,
        })
        # Bound to a different Woo customer on this store
        self.instance._woo_bind('res.partner', existing.id, '50', external_data={'email': 'buyer@example.com'})

        woo_customer = {
            'id': 51,
            'email': 'buyer@example.com',
            'first_name': 'New',
            'last_name': 'Buyer',
            'billing': {
                'first_name': 'New',
                'last_name': 'Buyer',
                'email': 'buyer@example.com',
                'phone': '123',
                'address_1': 'Street 1',
                'city': 'City',
                'postcode': '1000',
                'country': 'US',
            },
            'shipping': {},
        }
        api = MagicMock()
        api.get_customers.return_value = iter([woo_customer])

        with self._patch_api(api):
            self.instance._woo_import_customers()

        # Email already bound to another Woo id → create a new partner, do not merge
        binding_51 = self.env['kits.connector.binding'].search([
            ('instance_id', '=', self.instance.id),
            ('odoo_model', '=', 'res.partner'),
            ('external_id', '=', '51'),
        ], limit=1)
        self.assertTrue(binding_51)
        self.assertNotEqual(binding_51.odoo_record_id, existing.id)

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
        bindings = self.env['kits.connector.binding'].search([
            ('instance_id', '=', self.instance.id),
            ('odoo_model', '=', 'res.partner'),
            ('odoo_record_id', '=', partner.id),
        ])
        self.assertFalse(bindings)

    def test_customer_export_requires_email(self):
        partner = self.env['res.partner'].create({
            'name': 'No Email',
            'company_id': self.company.id,
        })
        self.instance._woo_bind('res.partner', partner.id, '80')
        api = MagicMock()
        with self._patch_api(api):
            summary = self.instance._woo_export_customers()
        self.assertIn('failed=', summary)
        api.create_customer.assert_not_called()
