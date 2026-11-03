# -*- coding: utf-8 -*-
from unittest.mock import MagicMock

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooSeparateBindings(WooConnectorCommon):

    def _other_store(self):
        return self.env['kits.connector.instance'].create({
            'name': 'Woo Store B',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://store-b.test',
            'woo_consumer_key': 'ck_b',
            'woo_consumer_secret': 'cs_b',
            'warehouse_id': self.warehouse.id if self.warehouse else False,
            'state': 'connected',
            'woo_import_images': False,
            'woo_create_products': True,
            'woo_sync_price': False,
        })

    def _customer(self, woo_id='10', email='buyer@example.com', first='Amit', last='Patel', street='1 Main'):
        return {
            'id': int(woo_id),
            'email': email,
            'first_name': first,
            'last_name': last,
            'billing': {
                'first_name': first,
                'last_name': last,
                'email': email,
                'phone': '111',
                'company': 'Demo Co',
                'address_1': street,
                'city': 'Ahmedabad',
                'postcode': '380001',
                'country': 'IN',
                'state': 'GJ',
            },
            'shipping': {
                'first_name': first,
                'last_name': last,
                'address_1': '9 Dock',
                'city': 'Surat',
                'postcode': '395003',
                'country': 'IN',
            },
        }

    def test_customer_binding_by_store_and_external_id_does_not_change_contact(self):
        partner = self.env['res.partner'].create({
            'name': 'Keep Name',
            'email': 'buyer@example.com',
            'phone': '000',
            'company_id': self.company.id,
        })
        self.env['kits.woo.customer.binding']._bind(self.instance, partner.id, '10')
        api = MagicMock()
        api.get_customers.return_value = iter([self._customer()])
        with self._patch_api(api):
            summary = self.instance._woo_import_customers()
        self.assertIn('reused=1', summary)
        self.assertEqual(partner.name, 'Keep Name')
        self.assertEqual(partner.phone, '000')
        self.assertEqual(self.env['kits.woo.customer.binding'].search_count([
            ('instance_id', '=', self.instance.id),
            ('external_id', '=', '10'),
        ]), 1)

    def test_customer_reused_by_email_across_stores_without_edits(self):
        other = self._other_store()
        api = MagicMock()
        api.get_customers.return_value = iter([self._customer(woo_id='10')])
        with self._patch_api(api):
            self.instance._woo_import_customers()
        partner = self.env['kits.woo.customer.binding']._find(self.instance, external_id='10').partner_id
        original_name = partner.name
        api_b = MagicMock()
        api_b.get_customers.return_value = iter([self._customer(woo_id='11', first='Other', last='Person')])
        with self._patch_api(api_b):
            other._woo_import_customers()
        partner.invalidate_recordset()
        self.assertEqual(partner.name, original_name)
        self.assertEqual(
            self.env['kits.woo.customer.binding']._find(other, external_id='11').partner_id,
            partner,
        )

    def test_new_customer_and_skip_without_name(self):
        api = MagicMock()
        nameless = self._customer(woo_id='12', first='', last='')
        api.get_customers.return_value = iter([self._customer(woo_id='13', email='new@example.com'), nameless])
        with self._patch_api(api):
            summary = self.instance._woo_import_customers()
        self.assertIn('created=1', summary)
        self.assertIn('skipped=1', summary)
        self.assertFalse(self.env['kits.woo.customer.binding']._find(self.instance, external_id='12'))
        created = self.env['kits.woo.customer.binding']._find(self.instance, external_id='13')
        self.assertEqual(created.partner_id.name, 'Amit Patel')
        self.assertTrue(created.partner_id.woo_is_store_customer)

    def test_billing_and_shipping_address_reuse_and_create(self):
        remote = self._customer()
        api = MagicMock()
        api.get_customers.return_value = iter([remote])
        with self._patch_api(api):
            self.instance._woo_import_customers()
            api.get_customers.return_value = iter([remote])
            self.instance._woo_import_customers()
        partner = self.env['kits.woo.customer.binding']._find(self.instance, external_id='10').partner_id
        self.assertEqual(len(partner.child_ids.filtered(lambda child: child.type == 'invoice')), 1)
        self.assertEqual(len(partner.child_ids.filtered(lambda child: child.type == 'delivery')), 1)
        changed = dict(remote)
        changed['billing'] = dict(remote['billing'], address_1='99 New Street')
        api.get_customers.return_value = iter([changed])
        with self._patch_api(api):
            self.instance._woo_import_customers()
        invoices = partner.child_ids.filtered(lambda child: child.type == 'invoice')
        self.assertEqual(len(invoices), 2)
        self.assertIn('1 Main', invoices.mapped('street'))
        self.assertIn('99 New Street', invoices.mapped('street'))

    def _simple(self, woo_id='50', sku='SKU-50', name='Remote Hat'):
        return {
            'id': int(woo_id),
            'name': name,
            'type': 'simple',
            'status': 'publish',
            'sku': sku,
            'regular_price': '15.00',
            'weight': '1.5',
            'description': '<p>Store only</p>',
            'short_description': 'Short store',
            'tax_status': 'taxable',
            'virtual': False,
            'categories': [],
            'tags': [],
            'images': [],
            'permalink': 'https://woo.test/product/hat',
        }

    def test_product_match_external_id_store_sku_and_odoo_sku(self):
        template = self.env['product.template'].create({
            'name': 'Odoo Hat',
            'default_code': 'SKU-50',
            'type': 'consu',
            'is_storable': True,
            'list_price': 4.0,
            'description': '<p>Odoo text</p>',
            'company_id': self.company.id,
        })
        variant = template.product_variant_id
        api = MagicMock()
        api.get_products.return_value = iter([self._simple()])
        self.instance.woo_sync_price = False
        self.instance.woo_sync_weight = False
        with self._patch_api(api):
            self.instance._woo_import_products()
        binding = self.env['kits.woo.variant.binding']._find(self.instance, external_id='50')
        self.assertEqual(binding.product_id, variant)
        self.assertEqual(template.name, 'Odoo Hat')
        self.assertIn('Odoo text', template.description or '')
        self.assertEqual(binding.product_binding_id.title, 'Remote Hat')
        api.get_products.return_value = iter([self._simple(woo_id='51', sku='SKU-50', name='Other Title')])
        with self._patch_api(api):
            self.instance._woo_import_products()
        by_sku = self.env['kits.woo.variant.binding']._find(self.instance, sku='SKU-50')
        self.assertTrue(by_sku)
        other = self._other_store()
        other.woo_sync_price = False
        api_b = MagicMock()
        api_b.get_products.return_value = iter([self._simple(woo_id='80', sku='SKU-50', name='Store B Hat')])
        with self._patch_api(api_b):
            other._woo_import_products()
        self.assertEqual(
            self.env['kits.woo.variant.binding']._find(other, external_id='80').product_id,
            variant,
        )
        self.assertEqual(template.name, 'Odoo Hat')
        self.assertEqual(self.env['product.template'].search_count([
            ('default_code', '=', 'SKU-50'),
        ]), 1)

    def test_create_products_setting_and_sku_validation(self):
        self.instance.woo_create_products = False
        api = MagicMock()
        api.get_products.return_value = iter([self._simple(sku='MISSING-ODOO')])
        with self._patch_api(api):
            summary = self.instance._woo_import_products()
        self.assertIn('failed=1', summary)
        self.assertFalse(self.env['product.product'].search([('default_code', '=', 'MISSING-ODOO')]))

        self.instance.woo_create_products = True
        api.get_products.return_value = iter([self._simple(woo_id='60', sku='NEW-SKU', name='Created Hat')])
        with self._patch_api(api):
            summary = self.instance._woo_import_products()
        self.assertIn('created=1', summary)
        self.assertTrue(self.env['product.product'].search([('default_code', '=', 'NEW-SKU')]))

        api.get_products.return_value = iter([self._simple(woo_id='61', sku='')])
        with self._patch_api(api):
            summary = self.instance._woo_import_products()
        self.assertIn('failed=1', summary)
        self.assertIn('no SKU', summary)

        duplicate = self._simple(woo_id='70', sku='')
        duplicate['type'] = 'variable'
        duplicate['attributes'] = [{
            'name': 'Size', 'variation': True, 'options': ['S', 'S'],
        }]
        api.get_products.return_value = iter([duplicate])
        api.get_variations.return_value = iter([
            {'id': 71, 'sku': 'DUP', 'attributes': [{'name': 'Size', 'option': 'S'}]},
            {'id': 72, 'sku': 'DUP', 'attributes': [{'name': 'Size', 'option': 'S'}]},
        ])
        with self._patch_api(api):
            summary = self.instance._woo_import_products()
        self.assertIn('repeated', summary)
        self.assertFalse(self.env['kits.woo.product.binding']._find(self.instance, external_id='70'))

        bare = self._simple(woo_id='73', sku='')
        bare['type'] = 'variable'
        bare['attributes'] = [{'name': 'Size', 'variation': True, 'options': []}]
        api.get_products.return_value = iter([bare])
        with self._patch_api(api):
            summary = self.instance._woo_import_products()
        self.assertIn('no option values', summary)

    def test_remote_and_local_deletion(self):
        partner = self.env['res.partner'].create({
            'name': 'Stay',
            'email': 'stay@example.com',
            'company_id': self.company.id,
        })
        self.env['kits.woo.customer.binding']._bind(self.instance, partner.id, '15')
        self.instance._woo_delete_customer_binding('15')
        self.assertFalse(self.env['kits.woo.customer.binding']._find(self.instance, external_id='15'))
        self.assertTrue(partner.exists())
        self.assertFalse(partner.woo_is_store_customer)

        template = self.env['product.template'].create({
            'name': 'Kept Product',
            'default_code': 'KEEP-1',
            'type': 'consu',
            'is_storable': True,
            'company_id': self.company.id,
        })
        self.instance._woo_bind('product.template', template.id, '90', {'type': 'simple', 'sku': 'KEEP-1'})
        self.instance._woo_bind('product.product', template.product_variant_id.id, '90', {'sku': 'KEEP-1'})
        self.instance._woo_mark_product_deleted_remote('90')
        product_binding = self.env['kits.woo.product.binding'].with_context(active_test=False)._find(
            self.instance, external_id='90',
        )
        variant_binding = self.env['kits.woo.variant.binding'].with_context(active_test=False)._find(
            self.instance, external_id='90',
        )
        self.assertFalse(product_binding.active)
        self.assertFalse(product_binding.exists_in_store)
        self.assertFalse(variant_binding.active)
        self.assertTrue(template.exists())
        self.assertTrue(template.product_variant_id.exists())
        product_binding.write({'active': True, 'exists_in_store': True, 'sync_state': 'synced'})
        variant_binding.write({'active': True, 'exists_in_store': True, 'sync_state': 'synced'})
        self.instance._woo_archive_variant_remote('90')
        variant_binding.invalidate_recordset()
        self.assertFalse(variant_binding.active)
        self.assertTrue(template.product_variant_id.exists())

        template.unlink()
        self.assertFalse(self.env['kits.woo.product.binding'].with_context(active_test=False).search([
            ('external_id', '=', '90'),
        ]))

    def test_repeated_import_and_stock_uses_store_variant_binding(self):
        api = MagicMock()
        api.get_products.side_effect = lambda **_kw: iter([self._simple(woo_id='55', sku='STOCK-1')])
        with self._patch_api(api):
            self.instance._woo_import_products()
            self.instance._woo_import_products()
        self.assertEqual(self.env['kits.woo.product.binding'].search_count([
            ('instance_id', '=', self.instance.id),
            ('external_id', '=', '55'),
        ]), 1)
        other = self._other_store()
        template = self.env['kits.woo.variant.binding']._find(self.instance, external_id='55').product_id.product_tmpl_id
        other._woo_bind('product.template', template.id, '550', {'type': 'simple', 'sku': 'STOCK-1'})
        other._woo_bind('product.product', template.product_variant_id.id, '550', {'sku': 'STOCK-1'})
        api_stock = MagicMock()
        with self._patch_api(api_stock):
            self.instance._woo_export_inventory(job=self.env['kits.connector.sync.job'].create({
                'name': 'stock a',
                'instance_id': self.instance.id,
                'job_type': 'woo_export_inventory',
                'state': 'pending',
                'payload': {'full': True},
            }))
        self.assertEqual(api_stock.update_product.call_args[0][0], 55)
        api_b = MagicMock()
        with self._patch_api(api_b):
            other._woo_export_inventory(job=self.env['kits.connector.sync.job'].create({
                'name': 'stock b',
                'instance_id': other.id,
                'job_type': 'woo_export_inventory',
                'state': 'pending',
                'payload': {'full': True},
            }))
        self.assertEqual(api_b.update_product.call_args[0][0], 550)

    def test_direct_validators_raise_before_create(self):
        with self.assertRaises(UserError):
            self.instance._woo_validate_import_variants(self._simple(sku=''))
