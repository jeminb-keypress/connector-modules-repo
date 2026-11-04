# -*- coding: utf-8 -*-
"""Matrix coverage for all 20 Sync Operation wizard operations (mocked Woo API)."""
from unittest.mock import MagicMock

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tools.binary import BinaryBytes

from odoo.addons.kits_odoo_base_connector.services.exceptions import ConnectorAPIError

from .common import WooConnectorCommon

WIZARD_OPERATIONS = [
    'import_customers',
    'import_products',
    'import_specific_products',
    'import_unshipped_orders',
    'import_shipped_orders',
    'import_cancelled_orders',
    'import_specific_orders',
    'import_categories',
    'import_tags',
    'import_attributes',
    'import_coupons',
    'map_products',
    'export_shipment',
    'export_stock',
    'export_categories',
    'update_categories',
    'export_tags',
    'update_tags',
    'export_coupons',
    'update_coupons',
]


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooWizardAllOperations(WooConnectorCommon):

    def _wizard(self, operation, **vals):
        values = {
            'instance_id': self.instance.id,
            'operation': operation,
        }
        values.update(vals)
        return self.env['kits.woo.sync.wizard'].create(values)

    def _job(self, job_type, payload=None):
        return self.env['kits.connector.sync.job'].create({
            'name': job_type,
            'instance_id': self.instance.id,
            'job_type': job_type,
            'payload': payload or {},
            'state': 'pending',
        })

    def _bind_product(self, sku='WIZ-SKU', woo_id=501):
        template = self.env['product.template'].create({
            'name': f'Wizard {sku}',
            'default_code': sku,
            'type': 'consu',
            'is_storable': True,
            'list_price': 10,
            'sale_ok': True,
            'company_id': self.company.id,
            'woo_instance_ids': [(4, self.instance.id)],
        })
        variant = template.product_variant_id
        self.instance._woo_bind('product.template', template.id, woo_id)
        self.instance._woo_bind(
            'product.product', variant.id, woo_id,
            external_data={'sku': sku, 'parent_id': False},
        )
        return template, variant

    def _sample_order(self, order_id, product_id, sku, status, date):
        return {
            'id': order_id,
            'number': str(order_id),
            'status': status,
            'currency': 'USD',
            'customer_id': 0,
            'date_created': date,
            'date_created_gmt': date,
            'prices_include_tax': False,
            'discount_total': '0.00',
            'total': '10.00',
            'billing': {
                'first_name': 'Ada',
                'last_name': 'Buyer',
                'email': f'buyer{order_id}@example.com',
                'address_1': '1 Street',
                'city': 'London',
                'postcode': 'EC1',
                'country': 'GB',
            },
            'shipping': {
                'first_name': 'Ada',
                'last_name': 'Buyer',
                'address_1': '1 Street',
                'city': 'London',
                'postcode': 'EC1',
                'country': 'GB',
            },
            'line_items': [{
                'id': 1,
                'name': 'Line',
                'product_id': product_id,
                'variation_id': 0,
                'quantity': 1,
                'price': 10.0,
                'sku': sku,
                'total': '10.00',
            }],
            'shipping_lines': [],
            'fee_lines': [],
            'coupon_lines': [],
            'tax_lines': [],
            'payment_method': '',
            'payment_method_title': '',
        }

    def test_wizard_exposes_exactly_twenty_operations(self):
        selection = [
            item[0] for item in self.env['kits.woo.sync.wizard']._fields['operation'].selection
        ]
        self.assertEqual(selection, WIZARD_OPERATIONS)
        self.assertEqual(len(selection), 20)
        self.assertNotIn('import_stock', selection)
        self.assertNotIn('export_customers', selection)
        self.assertNotIn('direction', self.env['kits.woo.sync.wizard']._fields)

    def test_http_error_urls_do_not_expose_oauth_query(self):
        from odoo.addons.kits_odoo_base_connector.services.http_client import ConnectorHttpClient
        dirty = (
            'http://localhost/wordpress/wp-json/wc/v3/orders/1'
            '?oauth_consumer_key=ck_secret&oauth_signature=sig'
        )
        safe = ConnectorHttpClient._safe_url_for_log(dirty)
        self.assertEqual(safe, 'http://localhost/wordpress/wp-json/wc/v3/orders/1')
        self.assertNotIn('oauth_consumer_key', safe)
        self.assertNotIn('ck_secret', safe)

    def test_all_queued_operations_create_expected_jobs(self):
        expected = {
            'import_customers': ('woo_import_customers', {}),
            'import_products': ('woo_import_products', {}),
            'import_specific_products': (
                'woo_import_products',
                {'woo_ids': [11, 12], 'allow_recreate': True},
            ),
            'import_unshipped_orders': ('woo_import_orders', {'order_scope': 'unshipped'}),
            'import_shipped_orders': ('woo_import_orders', {'order_scope': 'shipped'}),
            'import_cancelled_orders': ('woo_import_orders', {'order_scope': 'cancelled'}),
            'import_specific_orders': (
                'woo_import_orders',
                {'order_scope': 'specific', 'woo_ids': [91, 92]},
            ),
            'import_categories': ('woo_import_categories', {}),
            'import_tags': ('woo_import_tags', {}),
            'import_attributes': ('woo_import_attributes', {}),
            'import_coupons': ('woo_import_coupons', {}),
            'export_shipment': ('woo_export_shipments', {}),
            'export_stock': ('woo_export_inventory', {'full': True}),
            'export_categories': ('woo_export_categories', {'category_mode': 'export'}),
            'update_categories': ('woo_export_categories', {'category_mode': 'update'}),
            'export_tags': ('woo_export_tags', {'tag_mode': 'export'}),
            'update_tags': ('woo_export_tags', {'tag_mode': 'update'}),
            'export_coupons': ('woo_export_coupons', {'coupon_mode': 'export'}),
            'update_coupons': ('woo_export_coupons', {'coupon_mode': 'update'}),
        }
        for operation, (job_type, payload) in expected.items():
            vals = {}
            if operation == 'import_specific_products':
                vals['product_ids_text'] = '11,12'
            if operation == 'import_specific_orders':
                vals['order_ids_text'] = '91,92'
            wizard = self._wizard(operation, **vals)
            got_type, _name, got_payload = wizard._prepare_job()
            self.assertEqual(got_type, job_type, operation)
            self.assertEqual(got_payload, payload, operation)
            action = wizard.action_execute()
            self.assertEqual(action['type'], 'ir.actions.act_window', operation)
            job_ids = action['domain'][0][2]
            job = self.env['kits.connector.sync.job'].browse(job_ids)
            self.assertEqual(job.job_type, job_type, operation)
            self.assertEqual(job.payload or {}, payload, operation)
            self.assertIn(job.state, ('pending', 'running'), operation)

    def test_wizard_validation_edge_cases(self):
        with self.assertRaises(ValidationError):
            self._wizard('import_specific_products', product_ids_text='')
        with self.assertRaises(ValidationError):
            self._wizard('import_specific_orders', order_ids_text='abc')
        with self.assertRaises(ValidationError):
            self._wizard(
                'import_unshipped_orders',
                date_from=fields.Datetime.to_datetime('2026-02-01 00:00:00'),
                date_to=fields.Datetime.to_datetime('2026-01-01 00:00:00'),
            )
        with self.assertRaises(ValidationError):
            self._wizard('map_products')
        with self.assertRaises(ValidationError):
            self._wizard(
                'map_products',
                mapping_file=BinaryBytes(b'x', filename='map.txt'),
                mapping_filename='map.txt',
            )

    def test_01_import_customers_idempotent_and_no_overwrite(self):
        existing = self.env['res.partner'].create({
            'name': 'Keep Name',
            'email': 'keep@example.com',
            'phone': '111',
            'company_id': self.company.id,
        })
        api = MagicMock()
        api.get_customers.return_value = iter([{
            'id': 44,
            'email': 'keep@example.com',
            'first_name': 'Changed',
            'last_name': 'Person',
            'billing': {
                'first_name': 'Bill', 'last_name': 'To', 'address_1': '1 St',
                'city': 'City', 'postcode': '10001', 'country': 'US', 'phone': '222',
            },
            'shipping': {
                'first_name': 'Ship', 'last_name': 'To', 'address_1': '2 St',
                'city': 'City', 'postcode': '10001', 'country': 'US',
            },
        }, {
            'id': 45,
            'email': 'noname@example.com',
            'first_name': '',
            'last_name': '',
            'billing': {},
            'shipping': {},
        }])
        with self._patch_api(api):
            self.instance._woo_import_customers()
            self.instance._woo_import_customers()
        self.assertEqual(existing.name, 'Keep Name')
        self.assertEqual(existing.phone, '111')
        binding = self.env['kits.woo.customer.binding']._find(self.instance, external_id='44')
        self.assertEqual(binding.partner_id, existing)
        self.assertEqual(
            self.env['kits.woo.customer.binding'].search_count([
                ('instance_id', '=', self.instance.id),
                ('external_id', '=', '44'),
            ]),
            1,
        )
        self.assertFalse(
            self.env['kits.woo.customer.binding']._find(self.instance, external_id='45')
        )

    def test_02_03_import_products_and_specific_ids(self):
        remote = {
            'id': 801,
            'name': 'Simple One',
            'type': 'simple',
            'status': 'publish',
            'sku': 'SPEC-801',
            'regular_price': '9.00',
            'virtual': False,
            'tax_status': 'taxable',
            'date_modified_gmt': '2026-03-01T00:00:00',
            'date_created_gmt': '2026-03-01T00:00:00',
            'categories': [],
            'tags': [],
            'images': [],
        }

        def get_product(woo_id):
            if int(woo_id) == 801:
                return dict(remote)
            raise ConnectorAPIError('missing', status_code=404)

        api = MagicMock()
        api.get_product.side_effect = get_product
        api.get_variations.return_value = iter([])
        with self._patch_api(api):
            self.instance._woo_import_products(job=self._job(
                'woo_import_products', {'woo_ids': [801, 999]},
            ))
        self.assertTrue(self.env['kits.woo.product.binding']._find(self.instance, external_id='801'))
        self.assertFalse(self.env['kits.woo.product.binding']._find(self.instance, external_id='999'))
        count = self.env['product.template'].search_count([('default_code', '=', 'SPEC-801')])
        with self._patch_api(api):
            self.instance._woo_import_products(job=self._job(
                'woo_import_products', {'woo_ids': [801]},
            ))
        self.assertEqual(
            self.env['product.template'].search_count([('default_code', '=', 'SPEC-801')]),
            count,
        )

    def test_04_05_06_07_order_scopes(self):
        _template, variant = self._bind_product('ORD-SKU', 701)
        partner = self.env['res.partner'].create({
            'name': 'Order Buyer',
            'email': 'buyer@example.com',
            'company_id': self.company.id,
        })
        unshipped = self._sample_order(1001, 701, 'ORD-SKU', 'processing', '2026-04-10T00:00:00')
        shipped = self._sample_order(1002, 701, 'ORD-SKU', 'completed', '2026-05-10T00:00:00')

        api = MagicMock()
        api.get_orders.return_value = iter([unshipped])
        with self._patch_api(api):
            self.instance._woo_import_orders(job=self._job(
                'woo_import_orders', {
                    'order_scope': 'unshipped',
                    'date_from': '2026-04-01 00:00:00',
                    'date_to': '2026-04-30 00:00:00',
                },
            ))
        self.assertEqual(api.get_orders.call_args.kwargs['status'], 'pending,processing,on-hold')
        order = self.instance._woo_find_odoo('sale.order', 1001)
        self.assertTrue(order)
        self.assertEqual(order.state, 'sale')

        api.get_orders.return_value = iter([unshipped])
        with self._patch_api(api):
            self.instance._woo_import_orders(job=self._job(
                'woo_import_orders', {
                    'order_scope': 'unshipped',
                    'date_from': '2026-04-01 00:00:00',
                    'date_to': '2026-04-30 00:00:00',
                },
            ))
        self.assertEqual(
            self.env['sale.order'].search_count([
                ('woo_instance_id', '=', self.instance.id),
                ('woo_order_id', '=', '1001'),
            ]),
            1,
        )

        api.get_orders.return_value = iter([shipped])
        with self._patch_api(api):
            self.instance._woo_import_orders(job=self._job(
                'woo_import_orders', {'order_scope': 'shipped'},
            ))
        self.assertEqual(api.get_orders.call_args.kwargs['status'], 'completed')
        self.assertTrue(self.instance._woo_find_odoo('sale.order', 1002))

        linked = self.env['sale.order'].create({
            'partner_id': partner.id,
            'company_id': self.company.id,
            'woo_instance_id': self.instance.id,
            'woo_order_id': '1003',
            'woo_order_status': 'processing',
            'order_line': [(0, 0, {
                'product_id': variant.id,
                'product_uom_qty': 1,
                'price_unit': 10,
            })],
        })
        self.instance._woo_bind('sale.order', linked.id, 1003)
        api.get_orders.return_value = iter([{
            'id': 1003, 'number': '1003', 'status': 'cancelled',
            'date_created_gmt': '2026-05-01T00:00:00', 'line_items': [],
        }])
        with self._patch_api(api):
            self.instance._woo_import_orders(job=self._job(
                'woo_import_orders', {'order_scope': 'cancelled'},
            ))
        self.assertEqual(linked.state, 'cancel')

        api.get_order.return_value = dict(unshipped, status='on-hold')
        with self._patch_api(api):
            self.instance._woo_import_orders(job=self._job(
                'woo_import_orders', {'order_scope': 'specific', 'woo_ids': [1001, 4040]},
            ))
        self.assertEqual(order.woo_order_status, 'on-hold')

    def test_08_09_10_11_catalog_and_coupon_import(self):
        api = MagicMock()
        api.get_categories.return_value = iter([
            {'id': 1, 'name': 'Parent Cat', 'parent': 0, 'slug': 'parent-cat'},
            {'id': 2, 'name': 'Child Cat', 'parent': 1, 'slug': 'child-cat'},
            {'id': 3, 'name': '', 'parent': 0, 'slug': 'bad'},
        ])
        api.get_tags.return_value = iter([
            {'id': 8, 'name': 'Tag A', 'slug': 'tag-a'},
        ])
        api.get_attributes.return_value = iter([
            {'id': 3, 'name': 'Color', 'slug': 'color'},
        ])
        api.get_attribute_terms.return_value = iter([
            {'id': 30, 'name': 'Red'},
            {'id': 31, 'name': ''},
        ])
        api.get_coupons.side_effect = lambda **kw: iter([
            {
                'id': 55, 'code': 'SAVE10', 'discount_type': 'percent', 'amount': '10',
                'description': 'Ten', 'date_expires': None, 'usage_limit': None,
                'usage_limit_per_user': None, 'individual_use': False, 'free_shipping': False,
                'exclude_sale_items': False, 'minimum_amount': '', 'maximum_amount': '',
                'product_ids': [], 'excluded_product_ids': [],
                'product_categories': [], 'excluded_product_categories': [],
                'email_restrictions': [],
            },
            {
                'id': 56, 'code': 'FLAT5', 'discount_type': 'fixed_cart', 'amount': '5',
                'description': 'Five', 'date_expires': '2027-01-01T00:00:00',
                'usage_limit': 2, 'usage_limit_per_user': 1, 'individual_use': True,
                'free_shipping': False, 'exclude_sale_items': False,
                'minimum_amount': '10', 'maximum_amount': '',
                'product_ids': [], 'excluded_product_ids': [],
                'product_categories': [], 'excluded_product_categories': [],
                'email_restrictions': [],
            },
        ])
        with self._patch_api(api):
            cat_summary = self.instance._woo_import_categories()
            self.instance._woo_import_tags()
            self.instance._woo_import_attributes()
            self.instance._woo_import_coupons()
            self.instance._woo_import_categories()
            self.instance._woo_import_tags()
            self.instance._woo_import_coupons()
        self.assertIn('failed=', cat_summary)
        parent = self.instance._woo_find_odoo('product.category', 1)
        child = self.instance._woo_find_odoo('product.category', 2)
        self.assertTrue(parent)
        self.assertEqual(child.parent_id, parent)
        self.assertEqual(
            self.env['kits.connector.binding'].search_count([
                ('instance_id', '=', self.instance.id),
                ('odoo_model', '=', 'product.tag'),
                ('external_id', '=', '8'),
            ]),
            1,
        )
        self.assertTrue(self.instance._woo_find_odoo('product.attribute', 3))
        self.assertEqual(
            self.env['kits.woo.coupon'].search_count([
                ('instance_id', '=', self.instance.id), ('code', '=', 'SAVE10'),
            ]),
            1,
        )

    def test_12_map_products_rejects_bad_rows_and_links_good(self):
        good = self.env['product.template'].create({
            'name': 'Map Good',
            'default_code': 'MAP-GOOD',
            'type': 'consu',
            'list_price': 1,
            'company_id': self.company.id,
        })
        taken = self.env['product.template'].create({
            'name': 'Map Taken',
            'default_code': 'MAP-TAKEN',
            'type': 'consu',
            'list_price': 1,
            'company_id': self.company.id,
        })
        self.instance._woo_bind('product.template', taken.id, 900)
        content = BinaryBytes(
            b'woo_id,sku,odoo_id\n'
            b'901,MAP-GOOD,\n'
            b'902,MISSING,\n'
            b'900,MAP-TAKEN,\n'
            b'904,MAP-GOOD,\n',
            filename='map.csv',
        )
        action = self._wizard(
            'map_products',
            mapping_file=content,
            mapping_filename='map.csv',
        ).action_execute()
        message = action['params']['message'].lower()
        # Duplicate SKU rows are rejected together; missing/conflict rows fail.
        self.assertIn('failed', message)
        self.assertFalse(self.env['kits.woo.product.binding']._find(self.instance, external_id='901'))
        self.assertFalse(self.env['kits.woo.product.binding']._find(self.instance, external_id='902'))
        self.assertFalse(self.env['kits.woo.product.binding']._find(self.instance, external_id='904'))
        # Valid single-row mapping still works through the wizard Binary field.
        ok = self._wizard(
            'map_products',
            mapping_file=BinaryBytes(b'woo_id,sku\n905,MAP-GOOD\n', filename='ok.csv'),
            mapping_filename='ok.csv',
        ).action_execute()
        self.assertIn('linked', ok['params']['message'].lower())
        self.assertEqual(
            self.env['kits.woo.product.binding']._find(self.instance, external_id='905').product_tmpl_id,
            good,
        )
        self.assertEqual(
            self.env['kits.connector.sync.job'].search_count([
                ('instance_id', '=', self.instance.id),
                ('job_type', '=', 'woo_map_products'),
            ]),
            0,
        )

    def test_13_export_shipment_and_tracking_separation(self):
        _template, variant = self._bind_product('SHIP-1', 711)
        partner = self.env['res.partner'].create({
            'name': 'Ship Buyer', 'company_id': self.company.id,
        })
        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'company_id': self.company.id,
            'woo_instance_id': self.instance.id,
            'woo_order_id': '2001',
            'woo_order_status': 'processing',
            'order_line': [(0, 0, {
                'product_id': variant.id,
                'product_uom_qty': 1,
                'price_unit': 10,
            })],
        })
        order.action_confirm()
        for picking in order.picking_ids:
            picking.state = 'done'
        api = MagicMock()
        api.update_order.return_value = {'id': 2001, 'status': 'completed'}
        with self._patch_api(api):
            summary = self.instance._woo_export_shipments()
        self.assertIn('updated=1', summary)
        self.assertEqual(order.woo_order_status, 'completed')
        with self._patch_api(api):
            summary2 = self.instance._woo_export_shipments()
        self.assertIn('updated=0', summary2)

        unbound = self.env['sale.order'].create({
            'partner_id': partner.id,
            'company_id': self.company.id,
            'order_line': [(0, 0, {
                'product_id': variant.id,
                'product_uom_qty': 1,
                'price_unit': 10,
            })],
        })
        unbound.action_confirm()
        for picking in unbound.picking_ids:
            picking.state = 'done'
        api.update_order.reset_mock()
        with self._patch_api(api):
            self.instance._woo_export_shipments()
        api.update_order.assert_not_called()

        order.woo_order_status = 'processing'
        order.woo_tracking_number = 'TRACK-1'
        with self._patch_api(api):
            order.action_woo_export_tracking()
        self.assertEqual(order.woo_order_status, 'processing')

    def test_14_export_stock_filters_and_failures(self):
        self._bind_product('STK-1', 721)
        api = MagicMock()
        api.update_product.return_value = {'id': 721, 'stock_quantity': 5}
        with self._patch_api(api):
            summary = self.instance._woo_export_inventory(job=self._job(
                'woo_export_inventory', {'full': True},
            ))
        self.assertIn('updated', summary.lower())
        with self._patch_api(api):
            filtered = self.instance._woo_export_inventory(job=self._job(
                'woo_export_inventory', {'stock_from': '2099-01-01 00:00:00'},
            ))
        self.assertIn('updated=0', filtered)

        api.update_product.side_effect = ConnectorAPIError('gone', status_code=404)
        with self._patch_api(api):
            self.instance._woo_export_inventory(job=self._job(
                'woo_export_inventory', {'full': True},
            ))
        binding = self.env['kits.woo.variant.binding']._find(self.instance, external_id='721')
        self.assertEqual(binding.sync_state, 'deleted')

    def test_15_16_17_18_19_20_export_update_modes(self):
        category = self.env['product.category'].create({'name': 'Export Cat'})
        tag = self.env['product.tag'].create({'name': 'Export Tag'})
        coupon = self.env['kits.woo.coupon'].create({
            'name': 'Export Coupon',
            'code': 'EXPC10',
            'instance_id': self.instance.id,
            'discount_type': 'percent',
            'amount': 10,
            'sync_to_woo': True,
        })
        api = MagicMock()
        api.create_category.return_value = {'id': 41, 'name': category.name}
        api.update_category.return_value = {'id': 41, 'name': 'Export Cat Updated'}
        api.get_tags.return_value = iter([])
        api.create_tag.return_value = {'id': 42, 'name': tag.name}
        api.update_tag.return_value = {'id': 42, 'name': 'Export Tag Updated'}
        api.create_coupon.return_value = {'id': 43, 'code': coupon.code}
        api.update_coupon.return_value = {'id': 43, 'code': coupon.code, 'amount': '12'}
        with self._patch_api(api):
            self.instance._woo_export_categories(job=self._job(
                'woo_export_categories',
                {'category_mode': 'export', 'category_ids': [category.id]},
            ))
            self.instance._woo_export_categories(job=self._job(
                'woo_export_categories',
                {'category_mode': 'update', 'category_ids': [category.id]},
            ))
            unbound_cat = self.env['product.category'].create({'name': 'No Bind Cat'})
            create_cat_calls = api.create_category.call_count
            self.instance._woo_export_categories(job=self._job(
                'woo_export_categories',
                {'category_mode': 'update', 'category_ids': [unbound_cat.id]},
            ))
            self.assertEqual(api.create_category.call_count, create_cat_calls)
            self.instance._woo_export_tags(job=self._job(
                'woo_export_tags', {'tag_mode': 'export', 'tag_ids': [tag.id]},
            ))
            self.instance._woo_export_tags(job=self._job(
                'woo_export_tags', {'tag_mode': 'update', 'tag_ids': [tag.id]},
            ))
            unbound_tag = self.env['product.tag'].create({'name': 'No Bind Tag'})
            create_tag_calls = api.create_tag.call_count
            self.instance._woo_export_tags(job=self._job(
                'woo_export_tags', {'tag_mode': 'update', 'tag_ids': [unbound_tag.id]},
            ))
            self.assertEqual(api.create_tag.call_count, create_tag_calls)
            self.instance._woo_export_coupons(job=self._job(
                'woo_export_coupons', {'coupon_mode': 'export'},
            ))
            coupon.amount = 12
            self.instance._woo_export_coupons(job=self._job(
                'woo_export_coupons', {'coupon_mode': 'update'},
            ))
            unbound_coupon = self.env['kits.woo.coupon'].create({
                'name': 'No Bind Coupon',
                'code': 'NOBIND',
                'instance_id': self.instance.id,
                'discount_type': 'percent',
                'amount': 1,
                'sync_to_woo': True,
            })
            create_coupon_calls = api.create_coupon.call_count
            self.instance._woo_export_coupons(job=self._job(
                'woo_export_coupons',
                {'coupon_mode': 'update', 'coupon_ids': [unbound_coupon.id]},
            ))
            self.assertEqual(api.create_coupon.call_count, create_coupon_calls)
        self.assertTrue(self.instance._woo_find_odoo('product.category', 41))
        self.assertTrue(self.instance._woo_find_odoo('product.tag', 42))
        self.assertEqual(coupon.woo_external_id, '43')
        api.create_category.assert_called()
        api.update_category.assert_called()
        api.create_tag.assert_called()
        api.update_tag.assert_called()
        api.create_coupon.assert_called()
        api.update_coupon.assert_called()
