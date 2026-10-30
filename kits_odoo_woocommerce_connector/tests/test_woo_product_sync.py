# -*- coding: utf-8 -*-
from unittest.mock import MagicMock

from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooProductSync(WooConnectorCommon):

    def test_simple_product_import_and_idempotent_rerun(self):
        woo_product = {
            'id': 101,
            'name': 'Simple Tee',
            'type': 'simple',
            'status': 'publish',
            'sku': 'TEE-101',
            'barcode': '1234567890123',
            'regular_price': '19.99',
            'price': '19.99',
            'description': '<p>Desc</p>',
            'short_description': 'Short',
            'categories': [{'id': 7, 'name': 'Apparel'}],
            'tags': [{'id': 3, 'name': 'Summer'}],
            'images': [],
        }
        api = MagicMock()
        # Fresh iterator per call — a single exhausted iter() breaks the re-import assertion
        api.get_products.side_effect = lambda **_kw: iter([woo_product])
        api.get_variations.side_effect = lambda *_a, **_kw: iter([])

        with self._patch_api(api):
            summary1 = self.instance._woo_import_products()
            summary2 = self.instance._woo_import_products()

        self.assertIn('created=1', summary1)
        self.assertIn('updated=1', summary2)

        Binding = self.env['kits.connector.binding']
        tmpl_bindings = Binding.search([
            ('instance_id', '=', self.instance.id),
            ('odoo_model', '=', 'product.template'),
            ('external_id', '=', '101'),
        ])
        self.assertEqual(len(tmpl_bindings), 1)
        template = tmpl_bindings._kits_get_odoo_record()
        self.assertEqual(template.name, 'Simple Tee')
        self.assertEqual(template.default_code, 'TEE-101')
        # Second import must not create a second template with same SKU
        duplicates = self.env['product.template'].search([
            ('default_code', '=', 'TEE-101'),
            ('company_id', 'in', [False, self.company.id]),
        ])
        self.assertEqual(len(duplicates), 1)

    def test_sku_match_without_binding(self):
        existing = self.env['product.template'].create({
            'name': 'Local SKU Product',
            'default_code': 'SKU-MATCH-1',
            'type': 'consu',
            'is_storable': True,
            'list_price': 10.0,
            'company_id': self.company.id,
        })
        woo_product = {
            'id': 202,
            'name': 'Remote SKU Product',
            'type': 'simple',
            'status': 'publish',
            'sku': 'SKU-MATCH-1',
            'regular_price': '12.00',
            'categories': [],
            'tags': [],
            'images': [],
        }
        api = MagicMock()
        api.get_products.return_value = iter([woo_product])

        with self._patch_api(api):
            self.instance._woo_import_products()

        binding = self.env['kits.connector.binding'].search([
            ('instance_id', '=', self.instance.id),
            ('odoo_model', '=', 'product.template'),
            ('external_id', '=', '202'),
        ], limit=1)
        self.assertTrue(binding)
        self.assertEqual(binding.odoo_record_id, existing.id)
        self.assertEqual(existing.name, 'Remote SKU Product')

    def test_variable_product_import_no_duplicate_variants(self):
        woo_product = {
            'id': 303,
            'name': 'Variable Shirt',
            'type': 'variable',
            'status': 'publish',
            'sku': '',
            'regular_price': '',
            'attributes': [{
                'id': 1,
                'name': 'Size',
                'variation': True,
                'options': ['S', 'M'],
            }],
            'categories': [],
            'tags': [],
            'images': [],
        }
        variations = [
            {
                'id': 304,
                'sku': 'SHIRT-S',
                'regular_price': '20.00',
                'attributes': [{'name': 'Size', 'option': 'S'}],
                'image': {},
            },
            {
                'id': 305,
                'sku': 'SHIRT-M',
                'regular_price': '22.00',
                'attributes': [{'name': 'Size', 'option': 'M'}],
                'image': {},
            },
        ]
        api = MagicMock()
        api.get_products.return_value = iter([woo_product])
        api.get_variations.return_value = iter(variations)

        with self._patch_api(api):
            self.instance._woo_import_products()
            api.get_products.return_value = iter([woo_product])
            api.get_variations.return_value = iter(variations)
            self.instance._woo_import_products()

        template = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'product.template', external_id='303',
        )._kits_get_odoo_record()
        self.assertEqual(len(template.product_variant_ids), 2)
        var_bindings = self.env['kits.connector.binding'].search([
            ('instance_id', '=', self.instance.id),
            ('odoo_model', '=', 'product.product'),
            ('external_id', 'in', ['304', '305']),
        ])
        self.assertEqual(len(var_bindings), 2)

    def test_product_export_uses_binding(self):
        template = self.env['product.template'].create({
            'name': 'Export Me',
            'default_code': 'EXP-1',
            'type': 'consu',
            'is_storable': True,
            'list_price': 15.0,
            'sale_ok': True,
            'woo_instance_ids': [(6, 0, [self.instance.id])],
            'company_id': self.company.id,
        })
        api = MagicMock()
        api.create_product.return_value = {'id': 900, 'images': []}
        api.update_product.return_value = {'id': 900, 'images': []}

        with self._patch_api(api):
            self.instance._woo_export_products()
            self.instance._woo_export_products()

        self.assertEqual(api.create_product.call_count, 1)
        self.assertEqual(api.update_product.call_count, 1)
        binding = self.env['kits.connector.binding'].search([
            ('instance_id', '=', self.instance.id),
            ('odoo_model', '=', 'product.template'),
            ('odoo_record_id', '=', template.id),
        ], limit=1)
        self.assertEqual(binding.external_id, '900')
