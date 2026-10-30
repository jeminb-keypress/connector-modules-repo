# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.kits_odoo_base_connector.services.exceptions import ConnectorAPIError

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooRecordSyncAndStatus(WooConnectorCommon):

    def _expect_user_error(self, func):
        try:
            func()
        except UserError as exc:
            return exc
        self.fail('Expected UserError')

    def test_instance_specific_sku_bindings(self):
        other = self.env['kits.connector.instance'].create({
            'name': 'Second Woo Store',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://woo-b.test',
            'woo_consumer_key': 'ck_b',
            'woo_consumer_secret': 'cs_b',
            'warehouse_id': self.warehouse.id if self.warehouse else False,
            'state': 'connected',
        })
        tmpl_a = self.env['product.template'].create({
            'name': 'Shared SKU A',
            'default_code': 'SAME-SKU',
            'list_price': 10,
            'company_id': self.company.id,
        })
        tmpl_b = self.env['product.template'].create({
            'name': 'Shared SKU B',
            'default_code': 'SAME-SKU',
            'list_price': 20,
            'company_id': self.company.id,
        })
        self.instance._woo_bind('product.template', tmpl_a.id, '11')
        other._woo_bind('product.template', tmpl_b.id, '11')
        Binding = self.env['kits.connector.binding']
        self.assertEqual(
            Binding._kits_find_binding(self.instance, 'product.template', external_id='11').odoo_record_id,
            tmpl_a.id,
        )
        self.assertEqual(
            Binding._kits_find_binding(other, 'product.template', external_id='11').odoo_record_id,
            tmpl_b.id,
        )

    def test_mapped_field_marks_not_synced_per_instance(self):
        template = self.env['product.template'].create({
            'name': 'Status Product',
            'list_price': 5,
            'company_id': self.company.id,
            'woo_instance_ids': [(6, 0, [self.instance.id])],
        })
        binding = self.instance._woo_bind('product.template', template.id, '501')
        binding._kits_mark_synced()
        self.assertEqual(binding.sync_state, 'synced')
        template.write({'name': 'Status Product Updated'})
        self.assertEqual(binding.sync_state, 'not_synced')
        # Unrelated field should not dirty
        binding._kits_mark_synced()
        template.write({'description_purchase': 'internal only'})
        self.assertEqual(binding.sync_state, 'synced')

    def test_export_selected_templates_and_no_200_fallback(self):
        flagged = self.env['product.template'].create({
            'name': 'Flagged',
            'list_price': 9,
            'company_id': self.company.id,
            'woo_instance_ids': [(6, 0, [self.instance.id])],
            'sale_ok': True,
        })
        self.env['product.template'].create({
            'name': 'Not Flagged',
            'list_price': 9,
            'company_id': self.company.id,
            'woo_instance_ids': [(5, 0, 0)],
            'sale_ok': True,
        })
        api = MagicMock()
        api.create_product.return_value = {'id': 9001, 'images': []}
        with self._patch_api(api):
            summary = self.instance._woo_export_products()
        self.assertIn('created=1', summary)
        api.create_product.assert_called_once()
        # Selected IDs path
        api.create_product.reset_mock()
        api.create_product.return_value = {'id': 9002, 'images': []}
        job = self.env['kits.connector.sync.job'].create({
            'name': 'export selected',
            'instance_id': self.instance.id,
            'job_type': 'woo_export_products',
            'payload': {'template_ids': [flagged.id]},
            'state': 'pending',
        })
        with self._patch_api(api):
            # already bound above; update path
            flagged_binding = self.env['kits.connector.binding']._kits_find_binding(
                self.instance, 'product.template', odoo_record_id=flagged.id,
            )
            if flagged_binding:
                api.update_product.return_value = {'id': int(flagged_binding.external_id)}
                self.instance._woo_export_products(job=job)
                api.update_product.assert_called()
                flagged_binding.invalidate_recordset()
                self.assertEqual(flagged_binding.sync_state, 'synced')

    def test_export_update_marks_template_synced(self):
        """Successful update path must mark the existing template binding Synced."""
        template = self.env['product.template'].create({
            'name': 'Update Sync State',
            'list_price': 9,
            'company_id': self.company.id,
            'woo_instance_ids': [(6, 0, [self.instance.id])],
            'sale_ok': True,
        })
        binding = self.instance._woo_bind('product.template', template.id, '5501')
        binding._kits_mark_not_synced()
        self.assertEqual(binding.sync_state, 'not_synced')
        api = MagicMock()
        api.update_product.return_value = {'id': 5501, 'images': []}
        job = self.env['kits.connector.sync.job'].create({
            'name': 'export update synced',
            'instance_id': self.instance.id,
            'job_type': 'woo_export_products',
            'payload': {
                'template_ids': [template.id],
                'sync_versions': {str(template.id): binding.sync_version},
            },
            'state': 'pending',
        })
        with self._patch_api(api):
            summary = self.instance._woo_export_products(job=job)
        self.assertIn('updated=1', summary)
        binding.invalidate_recordset()
        self.assertEqual(binding.sync_state, 'synced')
        self.assertEqual(template.woo_sync_state, 'synced')

    def test_import_by_woo_ids_updates_existing(self):
        template = self.env['product.template'].create({
            'name': 'Old Name',
            'default_code': 'PULL-1',
            'list_price': 1,
            'company_id': self.company.id,
        })
        self.instance._woo_bind('product.template', template.id, '777')
        remote = {
            'id': 777,
            'name': 'New Name',
            'type': 'simple',
            'status': 'publish',
            'sku': 'PULL-1',
            'regular_price': '15.00',
            'price': '15.00',
            'description': '',
            'short_description': '',
            'categories': [],
            'tags': [],
            'images': [],
        }
        api = MagicMock()
        api.get_product.return_value = remote
        job = self.env['kits.connector.sync.job'].create({
            'name': 'import ids',
            'instance_id': self.instance.id,
            'job_type': 'woo_import_products',
            'payload': {'woo_ids': [777]},
            'state': 'pending',
        })
        with self._patch_api(api):
            summary = self.instance._woo_import_products(job=job)
        self.assertIn('updated=1', summary)
        self.assertEqual(template.name, 'New Name')
        binding = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'product.template', external_id='777',
        )
        self.assertEqual(binding.sync_state, 'synced')

    def test_missing_remote_product_marks_error(self):
        template = self.env['product.template'].create({
            'name': 'Gone Remote',
            'list_price': 1,
            'company_id': self.company.id,
        })
        binding = self.instance._woo_bind('product.template', template.id, '4041')
        api = MagicMock()
        api.get_product.side_effect = ConnectorAPIError('HTTP 404', status_code=404)
        job = self.env['kits.connector.sync.job'].create({
            'name': 'import missing',
            'instance_id': self.instance.id,
            'job_type': 'woo_import_products',
            'payload': {'woo_ids': [4041]},
            'state': 'pending',
        })
        with self._patch_api(api):
            self.instance._woo_import_products(job=job)
        self.assertEqual(binding.sync_state, 'error')

    def test_stale_export_job_skipped(self):
        template = self.env['product.template'].create({
            'name': 'Stale',
            'list_price': 3,
            'company_id': self.company.id,
            'woo_instance_ids': [(6, 0, [self.instance.id])],
        })
        binding = self.instance._woo_bind('product.template', template.id, '55')
        binding._kits_mark_synced()
        old_version = binding.sync_version
        binding._kits_mark_not_synced()
        api = MagicMock()
        job = self.env['kits.connector.sync.job'].create({
            'name': 'stale export',
            'instance_id': self.instance.id,
            'job_type': 'woo_export_products',
            'payload': {
                'template_ids': [template.id],
                'sync_versions': {str(template.id): old_version},
            },
            'state': 'pending',
        })
        with self._patch_api(api):
            summary = self.instance._woo_export_products(job=job)
        self.assertIn('skipped=1', summary)
        api.update_product.assert_not_called()

    def test_wizard_validation(self):
        Wizard = self.env['kits.woo.sync.wizard']
        with self.assertRaises(ValidationError):
            Wizard.create({
                'instance_id': self.instance.id,
                'direction': 'import',
                'operation': 'inventory',
                'import_mode': 'all',
            })
        with self.assertRaises(ValidationError):
            Wizard.create({
                'instance_id': self.instance.id,
                'direction': 'import',
                'operation': 'products',
                'import_mode': 'ids',
                'record_ids_text': '',
            })
        wiz = Wizard.create({
            'instance_id': self.instance.id,
            'direction': 'import',
            'operation': 'products',
            'import_mode': 'ids',
            'record_ids_text': '10,20',
        })
        with patch.object(type(self.instance), 'action_create_sync_job') as create_job:
            create_job.return_value = self.env['kits.connector.sync.job']
            wiz.action_execute()
            create_job.assert_called()
            self.assertEqual(create_job.call_args.kwargs['payload'].get('woo_ids'), [10, 20])

    def test_inventory_export_missing_binding_and_404(self):
        product = self.env['product.product'].create({
            'name': 'Stock Item',
            'is_storable': True,
            'list_price': 1,
            'company_id': self.company.id,
        })
        # No binding → enqueue does nothing useful
        job = self.instance._woo_enqueue_inventory_export(product)
        self.assertFalse(job)

        binding = self.instance._woo_bind(
            'product.product', product.id, '88', external_data={'parent_id': False},
        )
        api = MagicMock()
        api.update_product.side_effect = ConnectorAPIError('missing', status_code=404)
        with self._patch_api(api):
            summary = self.instance._woo_export_inventory(job=self.env['kits.connector.sync.job'].create({
                'name': 'inv',
                'instance_id': self.instance.id,
                'job_type': 'woo_export_inventory',
                'payload': {'product_ids': [product.id], 'sync_versions': {str(product.id): binding.sync_version}},
                'state': 'pending',
            }))
        self.assertIn('failed=1', summary)
        self.assertEqual(binding.sync_state, 'deleted')
        api.create_product.assert_not_called()

    def test_inventory_enqueue_merges_pending_jobs(self):
        self.instance.woo_auto_export_stock = True
        p1 = self.env['product.product'].create({
            'name': 'P1', 'is_storable': True, 'company_id': self.company.id,
        })
        p2 = self.env['product.product'].create({
            'name': 'P2', 'is_storable': True, 'company_id': self.company.id,
        })
        self.instance._woo_bind('product.product', p1.id, '1')
        self.instance._woo_bind('product.product', p2.id, '2')
        job1 = self.instance._woo_enqueue_inventory_export(p1)
        job2 = self.instance._woo_enqueue_inventory_export(p2)
        self.assertEqual(job1, job2)
        self.assertEqual(set(job1.payload['product_ids']), {p1.id, p2.id})

    def test_order_reimport_no_duplicate(self):
        product = self.env['product.product'].create({
            'name': 'Order Product',
            'default_code': 'ORD-1',
            'list_price': 10,
            'is_storable': True,
            'company_id': self.company.id,
        })
        self.instance._woo_bind('product.product', product.id, '61')
        woo_order = {
            'id': 900,
            'number': '900',
            'status': 'processing',
            'currency': 'USD',
            'date_created_gmt': '2026-01-01T10:00:00',
            'customer_id': 0,
            'billing': {
                'first_name': 'Guest', 'last_name': 'Buyer', 'email': 'guest@example.com',
                'address_1': 'Street', 'city': 'City', 'postcode': '1', 'country': 'US',
            },
            'shipping': {},
            'line_items': [{
                'id': 1, 'product_id': 61, 'variation_id': 0, 'quantity': 1,
                'name': 'Order Product', 'sku': 'ORD-1', 'price': 10, 'total': '10.00',
                'subtotal': '10.00',
            }],
            'shipping_lines': [],
            'fee_lines': [],
            'discount_total': '0',
            'prices_include_tax': False,
        }
        api = MagicMock()
        api.get_order.return_value = woo_order
        with self._patch_api(api):
            r1, _ = self.instance._woo_import_one_order(woo_order)
            r2, _ = self.instance._woo_import_one_order(woo_order)
        self.assertEqual(r1, 'created')
        self.assertEqual(r2, 'skipped')
        orders = self.env['sale.order'].search([('woo_order_id', '=', '900')])
        self.assertEqual(len(orders), 1)
        self.assertIn(orders.state, ('sale', 'done'))

    def test_sync_actions_require_manager(self):
        template = self.env['product.template'].create({
            'name': 'Secured',
            'list_price': 1,
            'company_id': self.company.id,
        })
        user = self.env['res.users'].create({
            'name': 'Woo User Only',
            'login': 'woo_user_only_test',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('kits_odoo_base_connector.group_kits_connector_user').id,
            ])],
        })
        with self.assertRaises(UserError):
            template.with_user(user).action_woo_export_to_store()
