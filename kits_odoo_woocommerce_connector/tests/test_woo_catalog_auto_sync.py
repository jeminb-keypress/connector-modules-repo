# -*- coding: utf-8 -*-
"""Automatic category/tag sync before product import/export."""

from unittest.mock import MagicMock

from odoo.exceptions import UserError, ValidationError
from odoo.fields import Command
from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooCatalogAutoSync(WooConnectorCommon):

    def _make_other_instance(self):
        return self.env['kits.woo.instance'].create({
            'name': 'Other Auto Sync Store',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://other-auto.example.test',
            'woo_consumer_key': 'ck_other_auto',
            'woo_consumer_secret': 'cs_other_auto',
            'state': 'connected',
            'warehouse_id': self.warehouse.id if self.warehouse else False,
            'woo_import_images': False,
        })

    def _use_catalog(self, api, *, categories=None, tags=None):
        if categories is not None:
            api.get_categories.side_effect = None
            api.get_categories.return_value = iter(categories)
        if tags is not None:
            api.get_tags.side_effect = None
            api.get_tags.return_value = iter(tags)

    # ------------------------------------------------------------------
    # Import matching
    # ------------------------------------------------------------------

    def test_find_category_by_slug_same_store_only(self):
        local = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Laptops',
            'slug': 'laptops',
        })
        other = self._make_other_instance()
        self.env['kits.woo.category'].create({
            'instance_id': other.id,
            'name': 'Laptops',
            'slug': 'laptops',
        })
        found = self.env['kits.woo.category']._find(self.instance, slug='LAPTOPS')
        self.assertEqual(found, local)
        self.assertFalse(
            self.env['kits.woo.category']._find(self.instance, slug='missing'),
        )

    def test_find_tag_external_id_then_slug_then_name(self):
        tag = self.env['kits.woo.tag'].create({
            'instance_id': self.instance.id,
            'name': 'Featured',
            'slug': 'featured',
            'external_id': '17',
            'sync_state': 'synced',
        })
        Tag = self.env['kits.woo.tag']
        self.assertEqual(Tag._find(self.instance, external_id='17'), tag)
        pending = self.env['kits.woo.tag'].create({
            'instance_id': self.instance.id,
            'name': 'Summer',
            'slug': 'summer-sale',
        })
        self.assertEqual(Tag._find(self.instance, slug='summer-sale'), pending)
        self.assertEqual(Tag._find(self.instance, name='Summer'), pending)

    def test_ensure_category_matches_slug_and_links_external_id(self):
        pending = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Electronics Local',
            'slug': 'electronics',
        })
        found = self.instance._woo_ensure_woo_categories([{
            'id': 16,
            'name': 'Electronics',
            'slug': 'electronics',
            'parent': 0,
        }])
        self.assertEqual(found, pending)
        self.assertEqual(pending.external_id, '16')
        self.assertTrue(pending.exists_in_store)
        self.assertEqual(pending.export_status, 'exported')

    def test_ensure_tag_creates_from_product_payload(self):
        found = self.instance._woo_ensure_woo_tags([{
            'id': 22,
            'name': 'New Tag',
            'slug': 'new-tag',
        }])
        self.assertEqual(len(found), 1)
        self.assertEqual(found.external_id, '22')
        self.assertEqual(found.slug, 'new-tag')
        self.assertTrue(found.exists_in_store)
        # No native product.tag created
        self.assertFalse(self.env['product.tag'].search([('name', '=', 'New Tag')]))

    def test_ensure_no_cross_store_slug_match(self):
        other = self._make_other_instance()
        foreign = self.env['kits.woo.category'].create({
            'instance_id': other.id,
            'name': 'Shared Slug',
            'slug': 'shared-slug',
        })
        found = self.instance._woo_ensure_woo_categories([{
            'id': 50,
            'name': 'Shared Slug',
            'slug': 'shared-slug',
            'parent': 0,
        }])
        self.assertTrue(found)
        self.assertNotEqual(found, foreign)
        self.assertEqual(found.instance_id, self.instance)
        self.assertEqual(found.external_id, '50')

    def test_import_prerequisites_failure_blocks_product_import(self):
        api = MagicMock()
        api.get_categories.side_effect = RuntimeError('catalog down')
        with self._patch_api(api):
            with self.assertRaises(UserError):
                self.instance._woo_sync_catalog_prerequisites()

    # ------------------------------------------------------------------
    # Export preparation
    # ------------------------------------------------------------------

    def test_export_prepares_assigned_pending_parents_and_tags(self):
        root = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Electronics',
            'slug': 'electronics',
        })
        child = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Laptops',
            'slug': 'laptops',
            'parent_id': root.id,
        })
        unrelated = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Unrelated Pending',
            'slug': 'unrelated-pending',
        })
        tag = self.env['kits.woo.tag'].create({
            'instance_id': self.instance.id,
            'name': 'Featured',
            'slug': 'featured',
        })
        unrelated_tag = self.env['kits.woo.tag'].create({
            'instance_id': self.instance.id,
            'name': 'Unused Tag',
            'slug': 'unused-tag',
        })
        template = self.env['product.template'].create({
            'name': 'Laptop Pro',
            'type': 'consu',
            'default_code': 'LAP-AUTO-1',
            'list_price': 99,
            'woo_instance_ids': [Command.set([self.instance.id])],
        })
        binding = self.env['kits.woo.product.binding'].create({
            'instance_id': self.instance.id,
            'product_tmpl_id': template.id,
            'title': 'Laptop Pro',
            'exists_in_store': False,
            'sync_state': 'not_synced',
            'woo_category_ids': [Command.set([child.id])],
            'woo_tag_ids': [Command.set([tag.id])],
        })

        api = MagicMock()
        api.create_category.side_effect = [
            {'id': 100, 'slug': 'electronics'},
            {'id': 101, 'slug': 'laptops'},
        ]
        api.create_tag.return_value = {'id': 200, 'slug': 'featured'}
        api.create_product.return_value = {'id': 9001, 'type': 'simple'}

        with self._patch_api(api):
            self._use_catalog(api, categories=[], tags=[])
            summary = self.instance._woo_export_products()

        self.assertIn('created=1', summary)
        self.assertEqual(root.external_id, '100')
        self.assertEqual(child.external_id, '101')
        self.assertEqual(tag.external_id, '200')
        self.assertFalse(unrelated.external_id)
        self.assertFalse(unrelated_tag.external_id)
        self.assertEqual(api.create_category.call_count, 2)
        self.assertEqual(api.create_tag.call_count, 1)
        # Parent before child
        first_payload = api.create_category.call_args_list[0][0][0]
        self.assertEqual(first_payload.get('name'), 'Electronics')
        second_payload = api.create_category.call_args_list[1][0][0]
        self.assertEqual(second_payload.get('parent'), 100)
        product_payload = api.create_product.call_args[0][0]
        self.assertEqual(product_payload['categories'], [{'id': 101}])
        self.assertEqual(product_payload['tags'], [{'id': 200}])
        binding.invalidate_recordset()
        self.assertEqual(binding.external_id, '9001')

    def test_export_blocks_product_with_failed_category_continues_sibling(self):
        good_cat = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Good Cat',
            'slug': 'good-cat',
            'external_id': '44',
            'sync_state': 'synced',
        })
        bad_cat = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Bad Cat',
            'slug': 'bad-cat',
        })
        good = self.env['product.template'].create({
            'name': 'Good Product',
            'type': 'consu',
            'default_code': 'GOOD-1',
            'list_price': 10,
            'woo_instance_ids': [Command.set([self.instance.id])],
        })
        bad = self.env['product.template'].create({
            'name': 'Bad Product',
            'type': 'consu',
            'default_code': 'BAD-1',
            'list_price': 10,
            'woo_instance_ids': [Command.set([self.instance.id])],
        })
        self.env['kits.woo.product.binding'].create({
            'instance_id': self.instance.id,
            'product_tmpl_id': good.id,
            'title': 'Good Product',
            'exists_in_store': False,
            'sync_state': 'not_synced',
            'woo_category_ids': [Command.set([good_cat.id])],
        })
        self.env['kits.woo.product.binding'].create({
            'instance_id': self.instance.id,
            'product_tmpl_id': bad.id,
            'title': 'Bad Product',
            'exists_in_store': False,
            'sync_state': 'not_synced',
            'woo_category_ids': [Command.set([bad_cat.id])],
        })

        api = MagicMock()
        api.create_category.side_effect = RuntimeError('Woo category create failed')
        api.create_product.return_value = {'id': 8001, 'type': 'simple'}

        with self._patch_api(api):
            self._use_catalog(api, categories=[], tags=[])
            summary = self.instance._woo_export_products()

        self.assertIn('created=1', summary)
        self.assertIn('failed=1', summary)
        self.assertFalse(bad_cat.external_id)
        good_binding = self.env['kits.woo.product.binding']._find(
            self.instance, odoo_record_id=good.id,
        )
        self.assertEqual(good_binding.external_id, '8001')

    def test_payload_validation_rejects_missing_parent_external_id(self):
        parent = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Parent Missing',
            'slug': 'parent-missing',
        })
        child = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Child Ok',
            'slug': 'child-ok',
            'parent_id': parent.id,
            'external_id': '77',
            'sync_state': 'synced',
        })
        template = self.env['product.template'].create({
            'name': 'Needs Parent',
            'type': 'consu',
            'list_price': 5,
        })
        binding = self.env['kits.woo.product.binding'].create({
            'instance_id': self.instance.id,
            'product_tmpl_id': template.id,
            'external_id': '701',
            'title': 'Needs Parent',
            'woo_category_ids': [Command.set([child.id])],
        })
        with self.assertRaises(UserError) as err:
            self.instance._woo_validate_binding_catalog_for_export(binding, template)
        self.assertIn('Parent Missing', str(err.exception))

    def test_price_only_update_skips_catalog_prep(self):
        template = self.env['product.template'].create({
            'name': 'Price Only',
            'type': 'consu',
            'default_code': 'PRICE-1',
            'list_price': 12,
            'woo_instance_ids': [Command.set([self.instance.id])],
        })
        self.env['kits.woo.product.binding'].create({
            'instance_id': self.instance.id,
            'product_tmpl_id': template.id,
            'external_id': '555',
            'title': 'Price Only',
        })
        api = MagicMock()
        api.update_product.return_value = {'id': 555}
        api.get_categories = MagicMock(side_effect=AssertionError('should not sync'))
        job = self.env['kits.connector.sync.job'].create({
            'name': 'price only',
            'instance_id': f'kits.woo.instance,{self.instance.id}',
            'job_type': 'woo_export_products',
            'state': 'pending',
            'payload': {
                'template_ids': [template.id],
                'update_groups': ['price'],
            },
        })
        with self._patch_api(api):
            summary = self.instance._woo_export_products(job=job)
        self.assertIn('updated=1', summary)
        api.get_categories.assert_not_called()

    def test_import_does_not_steal_external_id_from_slug_collision(self):
        """Id match wins; slug of an already-exported row is never stolen."""
        owner = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Owner Cat',
            'slug': 'owner-cat',
            'external_id': '88',
            'sync_state': 'synced',
        })
        pending = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Pending Same Remote',
            'slug': 'pending-same-remote',
        })
        # External id already owned → resolve returns owner, not pending.
        resolved = self.instance._woo_resolve_local_category(
            woo_id=88, slug='pending-same-remote',
        )
        self.assertEqual(resolved, owner)
        # Slug row that already has a different external id must not be linked.
        taken = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Taken Slug',
            'slug': 'taken-slug',
            'external_id': '50',
            'sync_state': 'synced',
        })
        self.assertFalse(
            self.instance._woo_resolve_local_category(woo_id=99, slug='taken-slug')
        )
        # Import id 88 with pending's slug: update owner, leave pending untouched.
        result = self.instance._woo_import_one_category({
            'id': 88,
            'name': 'Owner Cat Remote',
            'slug': 'pending-same-remote',
            'parent': 0,
        })
        self.assertEqual(result, 'updated')
        owner.invalidate_recordset()
        pending.invalidate_recordset()
        self.assertEqual(owner.external_id, '88')
        self.assertEqual(owner.name, 'Owner Cat Remote')
        self.assertFalse(pending.external_id)
        self.assertEqual(taken.external_id, '50')

    def test_import_incomplete_payload_preserves_parent(self):
        parent = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Keep Parent',
            'slug': 'keep-parent',
            'external_id': '70',
            'sync_state': 'synced',
        })
        child = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Keep Child',
            'slug': 'keep-child',
            'parent_id': parent.id,
            'external_id': '71',
            'sync_state': 'synced',
        })
        # Product-style payload without parent key must not clear hierarchy.
        self.instance._woo_import_one_category({
            'id': 71,
            'name': 'Keep Child',
            'slug': 'keep-child',
        })
        child.invalidate_recordset()
        self.assertEqual(child.parent_id, parent)

    def test_false_string_external_id_normalized(self):
        cat = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Bad Ext',
            'slug': 'bad-ext',
        })
        cat.write({'external_id': 'False'})
        self.assertFalse(cat.external_id)
        self.assertFalse(cat._kits_is_exported())

    def test_validate_error_includes_sku(self):
        pending = self.env['kits.woo.category'].create({
            'instance_id': self.instance.id,
            'name': 'Still Pending',
            'slug': 'still-pending-sku',
        })
        template = self.env['product.template'].create({
            'name': 'Blue Shirt',
            'type': 'consu',
            'default_code': 'SHIRT-001',
            'list_price': 10,
        })
        binding = self.env['kits.woo.product.binding'].create({
            'instance_id': self.instance.id,
            'product_tmpl_id': template.id,
            'external_id': '801',
            'title': 'Blue Shirt',
            'woo_category_ids': [Command.set([pending.id])],
        })
        with self.assertRaises(UserError) as err:
            self.instance._woo_validate_binding_catalog_for_export(binding, template)
        msg = str(err.exception)
        self.assertIn('SHIRT-001', msg)
        self.assertIn('Still Pending', msg)
