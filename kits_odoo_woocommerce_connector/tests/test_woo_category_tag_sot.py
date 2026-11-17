# -*- coding: utf-8 -*-
"""Connector-side category/tag source-of-truth tests."""

from unittest.mock import MagicMock

from odoo.exceptions import UserError, ValidationError
from odoo.fields import Command
from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooCategoryTagSourceOfTruth(WooConnectorCommon):

    def _make_other_instance(self):
        return self.env['kits.woo.instance'].create({
            'name': 'Other Woo Store',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://other.example.test',
            'woo_consumer_key': 'ck_other',
            'woo_consumer_secret': 'cs_other',
            'state': 'connected',
            'warehouse_id': self.warehouse.id if self.warehouse else False,
            'woo_import_images': False,
        })

    def _use_catalog(self, api, *, categories=None, tags=None):
        """Override empty catalog stubs from WooConnectorCommon._stub_catalog_api."""
        if categories is not None:
            api.get_categories.side_effect = None
            api.get_categories.return_value = iter(categories)
        if tags is not None:
            api.get_tags.side_effect = None
            api.get_tags.return_value = iter(tags)

    def test_create_category_and_duplicate_name_slug(self):
        cat = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Electronics',
            'slug': 'electronics',
        })
        self.assertFalse(cat.external_id)
        self.assertEqual(cat.export_status, 'pending')
        with self.assertRaises(ValidationError):
            self.env['kits.woo.category'].create({
                'instance_id': self.instance.id,
                'name': ' electronics ',
                'slug': 'other',
            })
        with self.assertRaises(ValidationError):
            self.env['kits.woo.category'].create({
                'instance_id': self.instance.id,
                'name': 'Other',
                'slug': 'ELECTRONICS',
            })
        other = self._make_other_instance()
        twin = self.env['kits.woo.category'].create({
            'instance_id': other.id,
            'name': 'Electronics',
            'slug': 'electronics',
        })
        self.assertTrue(twin)

    def test_import_category_no_native_create(self):
        api = MagicMock()
        before = self.env['product.category'].search_count([])
        with self._patch_api(api):
            self._use_catalog(api, categories=[
                {'id': 10, 'name': 'Parent', 'parent': 0, 'slug': 'parent'},
                {'id': 11, 'name': 'Child', 'parent': 10, 'slug': 'child'},
            ])
            summary = self.instance._woo_import_categories()
        self.assertIn('created=2', summary)
        parent = self.env['kits.woo.category']._find(self.instance, external_id='10')
        child = self.env['kits.woo.category']._find(self.instance, external_id='11')
        self.assertEqual(child.parent_id, parent)
        self.assertEqual(parent.export_status, 'exported')
        self.assertEqual(
            self.env['kits.connector.binding'].search_count([
                ('instance_id', '=', self.instance._kits_instance_ref()),
                ('odoo_model', '=', 'product.category'),
                ('external_id', 'in', ['10', '11']),
            ]),
            0,
        )
        self.assertEqual(self.env['product.category'].search_count([]), before)

    def test_export_new_and_update_required(self):
        cat = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Brand New',
            'slug': 'brand-new',
        })
        api = MagicMock()
        api.create_category.return_value = {'id': 88, 'slug': 'brand-new'}
        api.update_category.return_value = {'id': 88}
        with self._patch_api(api):
            summary = cat.action_export_selected()
        self.assertEqual(cat.external_id, '88')
        self.assertEqual(cat.export_status, 'exported')
        api.create_category.assert_called()
        cat.write({'name': 'Brand New Updated'})
        self.assertEqual(cat.export_status, 'to_update')
        with self._patch_api(api):
            cat.action_export_selected()
        self.assertEqual(cat.export_status, 'exported')
        api.update_category.assert_called()

    def test_export_child_exports_missing_parents(self):
        root = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Electronics',
            'slug': 'electronics',
        })
        mid = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Computers',
            'slug': 'computers',
            'parent_id': root.id,
        })
        leaf = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Laptops',
            'slug': 'laptops',
            'parent_id': mid.id,
        })
        api = MagicMock()
        api.create_category.side_effect = [
            {'id': 1, 'slug': 'electronics'},
            {'id': 2, 'slug': 'computers'},
            {'id': 3, 'slug': 'laptops'},
        ]
        with self._patch_api(api):
            leaf.action_export_selected()
        self.assertEqual(root.external_id, '1')
        self.assertEqual(mid.external_id, '2')
        self.assertEqual(leaf.external_id, '3')
        self.assertEqual(api.create_category.call_count, 3)
        # Shared parent not re-created on second export of sibling
        sibling = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Desktops',
            'slug': 'desktops',
            'parent_id': mid.id,
        })
        api.create_category.reset_mock()
        api.create_category.side_effect = [{'id': 4, 'slug': 'desktops'}]
        with self._patch_api(api):
            sibling.action_export_selected()
        self.assertEqual(sibling.external_id, '4')
        self.assertEqual(api.create_category.call_count, 1)

    def test_parent_export_failure_blocks_child(self):
        root = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Root Fail',
            'slug': 'root-fail',
        })
        child = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Child Fail',
            'slug': 'child-fail',
            'parent_id': root.id,
        })
        api = MagicMock()
        api.create_category.side_effect = Exception('remote down')
        with self._patch_api(api):
            summary = self.instance._woo_export_selected_categories(child)
        self.assertIn('failed=', summary)
        self.assertFalse(child.external_id)
        self.assertFalse(root.external_id)
        self.assertEqual(root.export_status, 'error')

    def test_circular_and_cross_store_parent(self):
        a = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'A',
            'slug': 'a-cat',
        })
        b = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'B',
            'slug': 'b-cat',
            'parent_id': a.id,
        })
        with self.assertRaises((ValidationError, UserError)):
            a.parent_id = b
        other = self._make_other_instance()
        foreign = self.env['kits.woo.category'].create({
            'instance_id': other.id,
            'name': 'Foreign',
            'slug': 'foreign',
        })
        with self.assertRaises(ValidationError):
            b.parent_id = foreign

    def test_tag_duplicate_and_export(self):
        tag = self.env['kits.woo.tag'].create({
            'instance_id': self.instance.id,
            'name': 'Summer',
            'slug': 'summer',
        })
        with self.assertRaises(ValidationError):
            self.env['kits.woo.tag'].create({
                'instance_id': self.instance.id,
                'name': 'SUMMER',
            })
        other = self._make_other_instance()
        self.env['kits.woo.tag'].create({
            'instance_id': other.id,
            'name': 'Summer',
            'slug': 'summer',
        })
        api = MagicMock()
        api.get_tags.return_value = iter([])
        api.create_tag.return_value = {'id': 9, 'slug': 'summer'}
        with self._patch_api(api):
            tag.action_export_selected()
        self.assertEqual(tag.external_id, '9')
        self.assertEqual(tag.export_status, 'exported')

    def test_product_binding_assignment_rules(self):
        pending = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Pending Cat',
            'slug': 'pending-cat',
        })
        exported = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Exported Cat',
            'slug': 'exported-cat',
            'external_id': '101',
            'sync_state': 'synced',
        })
        other = self._make_other_instance()
        foreign = self.env['kits.woo.category'].create({
            'instance_id': other.id,
            'name': 'Foreign Cat',
            'slug': 'foreign-cat',
            'external_id': '55',
            'sync_state': 'synced',
        })
        template = self.env['product.template'].create({
            'name': 'Bound Product',
            'type': 'consu',
            'list_price': 10,
        })
        binding = self.env['kits.woo.product.binding'].create({
            'instance_id': self.instance.id,
            'product_tmpl_id': template.id,
            'external_id': '501',
            'title': 'Bound Product',
        })
        # Pending same-store categories may be assigned; export prep pushes them.
        binding.woo_category_ids = pending
        self.assertEqual(binding.woo_category_ids, pending)
        with self.assertRaises(ValidationError):
            binding.woo_category_ids = foreign
        binding.woo_category_ids = exported
        self.assertEqual(binding.woo_category_ids, exported)

        tag = self.env['kits.woo.tag'].create({
            'instance_id': self.instance.id,
            'name': 'OK Tag',
            'slug': 'ok-tag',
            'external_id': '201',
            'sync_state': 'synced',
        })
        binding.woo_tag_ids = tag

    def test_product_import_assigns_connector_categories_tags(self):
        api = MagicMock()
        with self._patch_api(api):
            self._use_catalog(
                api,
                categories=[{'id': 1, 'name': 'CatA', 'parent': 0, 'slug': 'cata'}],
                tags=[{'id': 2, 'name': 'TagA', 'slug': 'taga'}],
            )
            self.instance._woo_import_categories()
            self.instance._woo_import_tags()
        woo_product = {
            'id': 900,
            'name': 'Imported Item',
            'type': 'simple',
            'sku': 'IMP-CAT-1',
            'status': 'publish',
            'regular_price': '12.00',
            'categories': [{'id': 1, 'name': 'CatA'}],
            'tags': [{'id': 2, 'name': 'TagA'}],
            'images': [],
            'attributes': [],
        }
        self.instance.woo_create_products = True
        before_native = self.env['product.category'].search_count([])
        with self._patch_api(api):
            self._use_catalog(
                api,
                categories=[{'id': 1, 'name': 'CatA', 'parent': 0, 'slug': 'cata'}],
                tags=[{'id': 2, 'name': 'TagA', 'slug': 'taga'}],
            )
            self.instance._woo_import_one_product(woo_product)
        binding = self.env['kits.woo.product.binding']._find(self.instance, external_id='900')
        self.assertTrue(binding)
        self.assertEqual(binding.woo_category_ids.external_id, '1')
        self.assertEqual(binding.woo_tag_ids.external_id, '2')
        self.assertEqual(self.env['product.category'].search_count([]), before_native)

    def test_product_export_uses_connector_external_ids(self):
        template = self.env['product.template'].create({
            'name': 'Export Me',
            'type': 'consu',
            'default_code': 'EXP-CAT-1',
            'list_price': 15,
            'woo_instance_ids': [Command.set([self.instance.id])],
        })
        cat = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Export Cat',
            'slug': 'export-cat',
            'external_id': '44',
            'sync_state': 'synced',
        })
        tag = self.env['kits.woo.tag'].create({
            'instance_id': self.instance.id,
            'name': 'Export Tag',
            'slug': 'export-tag',
            'external_id': '45',
            'sync_state': 'synced',
        })
        self.env['kits.woo.product.binding'].create({
            'instance_id': self.instance.id,
            'product_tmpl_id': template.id,
            'external_id': '700',
            'title': 'Export Me',
            'woo_category_ids': [Command.set([cat.id])],
            'woo_tag_ids': [Command.set([tag.id])],
        })
        api = MagicMock()
        api.update_product.return_value = {'id': 700}
        with self._patch_api(api):
            self.instance._woo_export_one_template(template, api)
        payload = api.update_product.call_args[0][1]
        self.assertEqual(payload['categories'], [{'id': 44}])
        self.assertEqual(payload['tags'], [{'id': 45}])

    def test_product_export_rejects_unexported_assignment(self):
        template = self.env['product.template'].create({
            'name': 'Bad Export',
            'type': 'consu',
            'default_code': 'BAD-CAT-1',
            'list_price': 15,
            'woo_instance_ids': [Command.set([self.instance.id])],
        })
        pending = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Still Pending',
            'slug': 'still-pending',
            'external_id': '99',
            'sync_state': 'error',
            'exists_in_store': False,
        })
        # Bypass assignment validation to simulate invalid state for export check
        binding = self.env['kits.woo.product.binding'].create({
            'instance_id': self.instance.id,
            'product_tmpl_id': template.id,
            'external_id': '701',
            'title': 'Bad Export',
        })
        self.env.cr.execute(
            'INSERT INTO kits_woo_product_binding_woo_category_rel '
            '(binding_id, woo_category_id) VALUES (%s, %s)',
            (binding.id, pending.id),
        )
        binding.invalidate_recordset()
        api = MagicMock()
        with self._patch_api(api):
            with self.assertRaises(UserError):
                self.instance._woo_export_one_template(template, api)
