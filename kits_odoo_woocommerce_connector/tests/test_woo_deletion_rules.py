# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.kits_odoo_base_connector.services.exceptions import ConnectorAPIError

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooDeletionAndSyncRules(WooConnectorCommon):

    def _simple_payload(self, woo_id, sku, **extra):
        payload = {
            'id': woo_id,
            'name': extra.pop('name', f'Remote {woo_id}'),
            'type': 'simple',
            'status': 'publish',
            'sku': sku,
            'regular_price': '10.00',
            'description': '<p>Full</p>',
            'short_description': 'Short',
            'categories': [],
            'tags': [],
            'images': [],
        }
        payload.update(extra)
        return payload

    def test_ambiguous_sku_does_not_guess(self):
        first = self.env['product.template'].create({
            'name': 'Local A',
            'default_code': 'AMBIG-1',
            'type': 'consu',
            'company_id': self.company.id,
        })
        second = self.env['product.template'].create({
            'name': 'Local B',
            'default_code': 'AMBIG-1',
            'type': 'consu',
            'company_id': self.company.id,
        })
        api = MagicMock()
        api.get_products.return_value = iter([self._simple_payload(501, 'AMBIG-1', name='Should Not Apply')])
        with self._patch_api(api):
            summary = self.instance._woo_import_products()
        self.assertIn('skipped=1', summary)
        self.assertFalse(self.env['kits.connector.binding'].search([
            ('instance_id', '=', self.instance.id),
            ('external_id', '=', '501'),
        ]))
        self.assertEqual(first.name, 'Local A')
        self.assertEqual(second.name, 'Local B')
        log = self.env['kits.connector.log'].search([
            ('instance_id', '=', self.instance.id),
            ('message', 'ilike', 'AMBIG-1'),
        ], limit=1)
        self.assertTrue(log)

    def test_customer_details_and_no_cross_store_company_merge(self):
        woo_customer = {
            'id': 44,
            'email': 'ada@example.com',
            'first_name': 'Ada',
            'last_name': 'Lovelace',
            'username': 'ada',
            'date_modified_gmt': '2026-10-30T10:00:00',
            'billing': {
                'first_name': 'Ada',
                'last_name': 'Lovelace',
                'email': 'ada@example.com',
                'phone': '555',
                'company': 'Analytical Engines',
                'address_1': '1 Compute',
                'city': 'London',
                'postcode': 'EC1',
                'country': 'GB',
            },
            'shipping': {
                'first_name': 'Ada',
                'last_name': 'Lovelace',
                'address_1': '2 Ship',
                'city': 'London',
                'postcode': 'EC2',
                'country': 'GB',
            },
            'meta_data': [{'key': 'mobile', 'value': '999'}],
        }
        api = MagicMock()
        api.get_customers.side_effect = lambda **_kw: iter([woo_customer])
        with self._patch_api(api):
            self.instance._woo_import_customers()
            self.instance._woo_import_customers()
        binding = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'res.partner', external_id='44',
        )
        partner = binding._kits_get_odoo_record()
        self.assertEqual(partner.name, 'Ada Lovelace')
        self.assertEqual(partner.phone, '555')
        if 'mobile' in partner._fields:
            self.assertEqual(partner.mobile, '999')
        else:
            self.assertEqual((binding.external_data or {}).get('mobile'), '999')
        self.assertFalse(partner.parent_id)
        self.assertEqual(partner.woo_company_name, 'Analytical Engines')
        self.assertEqual((binding.external_data or {}).get('company'), 'Analytical Engines')
        self.assertEqual(partner.street, '1 Compute')
        self.assertFalse(partner.child_ids)
        self.assertEqual(self.env['res.partner'].search_count([
            ('email', '=', 'ada@example.com'),
        ]), 1)
        self.assertFalse(self.env['res.partner'].search([
            ('name', '=', 'Analytical Engines'),
        ]))

        other = self.env['kits.connector.instance'].create({
            'name': 'Store B Customers',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://store-b.test',
            'woo_consumer_key': 'ck',
            'woo_consumer_secret': 'cs',
            'state': 'connected',
        })
        other_customer = dict(woo_customer, id=45)
        api_b = MagicMock()
        api_b.get_customers.return_value = iter([other_customer])
        with patch.object(type(other), '_woo_api', return_value=api_b):
            other._woo_import_customers()
        other_partner = self.env['kits.connector.binding']._kits_find_binding(
            other, 'res.partner', external_id='45',
        )._kits_get_odoo_record()
        self.assertNotEqual(other_partner, partner)
        self.assertFalse(other_partner.parent_id)
        self.assertEqual(other_partner.woo_company_name, 'Analytical Engines')
        self.assertFalse(self.env['res.partner'].search([
            ('name', '=', 'Analytical Engines'),
        ]))

    def test_customer_export_is_unavailable(self):
        job = self.env['kits.connector.sync.job'].create({
            'name': 'legacy export',
            'instance_id': self.instance.id,
            'job_type': 'woo_export_customers',
            'state': 'pending',
        })
        with self.assertRaises(UserError):
            job._run_woo_export_customers()
        self.assertFalse(hasattr(self.env['res.partner'], 'action_woo_export_to_store'))
        with self.assertRaises(ValidationError):
            self.env['kits.woo.sync.wizard'].create({
                'instance_id': self.instance.id,
                'direction': 'export',
                'operation': 'customers',
                'import_mode': 'all',
            })

    def test_customer_delete_is_idempotent_and_store_specific(self):
        partner = self.env['res.partner'].create({
            'name': 'Keep Me',
            'email': 'keep@example.com',
            'company_id': self.company.id,
        })
        other = self.env['kits.connector.instance'].create({
            'name': 'Store B Delete',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://store-b.test',
            'woo_consumer_key': 'ck',
            'woo_consumer_secret': 'cs',
            'state': 'connected',
        })
        binding = self.instance._woo_bind('res.partner', partner.id, '70')
        other_binding = other._woo_bind('res.partner', partner.id, '70')
        for _index in range(2):
            summary = self.instance._woo_process_customer_webhook(job=self.env['kits.connector.sync.job'].create({
                'name': 'cust del',
                'instance_id': self.instance.id,
                'job_type': 'woo_webhook_customer',
                'payload': {'topic': 'customer.deleted', 'woo_id': 70, 'customer': {'id': 70}},
                'state': 'pending',
            }))
            self.assertIn('deleted remote', summary.lower())
        self.assertEqual(binding.sync_state, 'deleted')
        self.assertTrue(partner.active)
        self.assertEqual(other_binding.sync_state, 'synced')

    def test_product_import_cost_and_stock_not_applied(self):
        api = MagicMock()
        api.get_products.return_value = iter([self._simple_payload(
            610, 'COST-1',
            meta_data=[{'key': '_wc_cog_cost', 'value': '4.50'}],
            stock_quantity=999,
            manage_stock=True,
        )])
        with self._patch_api(api):
            self.instance._woo_import_products()
        template = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'product.template', external_id='610',
        )._kits_get_odoo_record()
        variant = template.product_variant_id
        self.assertEqual(variant.standard_price, 4.5)
        self.assertEqual(variant.qty_available, 0)
        binding = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'product.product', external_id='610',
        )
        self.assertFalse((binding.external_data or {}).get('variation_id'))

    def test_trash_status_does_not_archive_odoo_product(self):
        template = self.env['product.template'].create({
            'name': 'Published',
            'type': 'consu',
            'company_id': self.company.id,
            'woo_instance_ids': [(6, 0, [self.instance.id])],
        })
        self.instance._woo_bind('product.template', template.id, '77')
        api = MagicMock()
        api.get_products.return_value = iter([self._simple_payload(77, 'TRASH-1', status='trash')])
        with self._patch_api(api):
            self.instance._woo_import_products()
        template.invalidate_recordset()
        self.assertTrue(template.active)
        binding = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'product.template', external_id='77',
        )
        self.assertEqual(binding.sync_state, 'deleted')
        self.assertNotIn(self.instance, template.woo_instance_ids)

    def test_remote_delete_is_store_specific_and_idempotent(self):
        template = self.env['product.template'].create({
            'name': 'Multi Store',
            'type': 'consu',
            'company_id': self.company.id,
        })
        other = self.env['kits.connector.instance'].create({
            'name': 'Store B Products',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://store-b.test',
            'woo_consumer_key': 'ck',
            'woo_consumer_secret': 'cs',
            'state': 'connected',
        })
        template.woo_instance_ids = self.instance | other
        binding_a = self.instance._woo_bind('product.template', template.id, '80')
        variant = template.product_variant_id
        self.instance._woo_bind('product.product', variant.id, '80', external_data={
            'parent_id': False,
            'variation_id': False,
        })
        binding_b = other._woo_bind('product.template', template.id, '180')
        self.instance._woo_process_product_webhook(job=self.env['kits.connector.sync.job'].create({
            'name': 'del',
            'instance_id': self.instance.id,
            'job_type': 'woo_webhook_product',
            'payload': {'topic': 'product.deleted', 'woo_id': 80, 'product': {'id': 80}},
            'state': 'pending',
        }))
        self.instance._woo_process_product_webhook(job=self.env['kits.connector.sync.job'].create({
            'name': 'del again',
            'instance_id': self.instance.id,
            'job_type': 'woo_webhook_product',
            'payload': {'topic': 'product.deleted', 'woo_id': 80, 'product': {'id': 80}},
            'state': 'pending',
        }))
        self.assertEqual(binding_a.sync_state, 'deleted')
        self.assertEqual(binding_b.sync_state, 'synced')
        self.assertTrue(template.active)
        self.assertNotIn(self.instance, template.woo_instance_ids)
        self.assertIn(other, template.woo_instance_ids)
        variant_binding = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'product.product', external_id='80',
        )
        self.assertEqual(variant_binding.sync_state, 'deleted')

    def test_odoo_delete_does_not_call_woo_or_recreate_on_import(self):
        template = self.env['product.template'].create({
            'name': 'Local Gone',
            'default_code': 'LOCAL-GONE',
            'type': 'consu',
            'is_storable': False,
            'company_id': self.company.id,
        })
        self.instance._woo_bind('product.template', template.id, '81')
        self.instance._woo_bind(
            'product.product', template.product_variant_id.id, '81',
            external_data={'parent_id': False},
        )
        api = MagicMock()
        with self._patch_api(api):
            template.unlink()
        api.delete_product.assert_not_called()
        api.update_product.assert_not_called()
        api.create_product.assert_not_called()
        binding = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'product.template', external_id='81',
        )
        self.assertEqual(binding.sync_state, 'deleted_local')
        api.get_products.return_value = iter([self._simple_payload(81, 'LOCAL-GONE')])
        with self._patch_api(api):
            summary = self.instance._woo_import_products()
        self.assertIn('skipped=1', summary)
        self.assertFalse(self.env['product.template'].search([
            ('default_code', '=', 'LOCAL-GONE'),
            ('company_id', 'in', [False, self.company.id]),
        ]))
        api.create_product.assert_not_called()

    def test_automatic_export_and_stock_do_not_recreate_deleted_remote(self):
        template = self.env['product.template'].create({
            'name': 'Remote Gone',
            'default_code': 'REMOTE-GONE',
            'type': 'consu',
            'is_storable': True,
            'list_price': 5,
            'sale_ok': True,
            'company_id': self.company.id,
            'woo_instance_ids': [(6, 0, [self.instance.id])],
        })
        binding = self.instance._woo_bind('product.template', template.id, '82')
        variant_binding = self.instance._woo_bind(
            'product.product', template.product_variant_id.id, '82',
            external_data={'parent_id': False, 'variation_id': False},
        )
        binding._kits_mark_deleted_remote(message='gone')
        variant_binding._kits_mark_deleted_remote(message='gone')
        template.with_context(kits_woo_skip_dirty=True).write({
            'woo_instance_ids': [(4, self.instance.id)],
        })
        api = MagicMock()
        job = self.env['kits.connector.sync.job'].create({
            'name': 'auto export',
            'instance_id': self.instance.id,
            'job_type': 'woo_export_products',
            'payload': {'template_ids': [template.id], 'automatic': True},
            'state': 'pending',
        })
        with self._patch_api(api):
            summary = self.instance._woo_export_products(job=job)
            stock_summary = self.instance._woo_export_inventory()
        self.assertIn('skipped=1', summary)
        api.create_product.assert_not_called()
        api.update_product.assert_not_called()
        self.assertIn('updated=0', stock_summary)
        api.create_product.assert_not_called()
        self.assertEqual(binding.sync_state, 'deleted')

        template.write({'name': 'Edited After Delete'})
        binding.invalidate_recordset()
        self.assertEqual(binding.sync_state, 'deleted')

    def test_stock_retry_uses_latest_version_and_store_failure_is_isolated(self):
        product = self.env['product.product'].create({
            'name': 'Qty Item',
            'is_storable': True,
            'list_price': 1,
            'company_id': self.company.id,
        })
        binding = self.instance._woo_bind('product.product', product.id, '83')
        binding._kits_mark_not_synced()
        stale_version = binding.sync_version
        binding._kits_mark_not_synced()
        api = MagicMock()
        api.update_product.return_value = {'id': 83}
        stale_job = self.env['kits.connector.sync.job'].create({
            'name': 'stale stock',
            'instance_id': self.instance.id,
            'job_type': 'woo_export_inventory',
            'payload': {
                'product_ids': [product.id],
                'sync_versions': {str(product.id): stale_version},
            },
            'state': 'pending',
        })
        with self._patch_api(api):
            self.instance._woo_export_inventory(job=stale_job)
        api.update_product.assert_not_called()
        fresh_job = self.env['kits.connector.sync.job'].create({
            'name': 'fresh stock',
            'instance_id': self.instance.id,
            'job_type': 'woo_export_inventory',
            'payload': {
                'product_ids': [product.id],
                'sync_versions': {str(product.id): binding.sync_version},
            },
            'state': 'pending',
        })
        with self._patch_api(api):
            self.instance._woo_export_inventory(job=fresh_job)
        api.update_product.assert_called_once()
        sent_qty = api.update_product.call_args.args[1]['stock_quantity']
        self.assertEqual(sent_qty, int(product.with_context(warehouse_id=self.warehouse.id).free_qty))

        other = self.env['kits.connector.instance'].create({
            'name': 'Store B Stock',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://store-b.test',
            'woo_consumer_key': 'ck',
            'woo_consumer_secret': 'cs',
            'warehouse_id': self.warehouse.id,
            'woo_auto_export_stock': True,
            'state': 'connected',
        })
        self.instance.woo_auto_export_stock = True
        other._woo_bind('product.product', product.id, '830')
        move = self.env['stock.move'].create({
            'product_id': product.id,
            'uom_id': product.uom_id.id,
            'location_id': self.warehouse.lot_stock_id.id,
            'location_dest_id': self.env.ref('stock.stock_location_customers').id,
            'product_uom_qty': 1,
            'company_id': self.company.id,
        })
        real = type(self.instance)._woo_enqueue_inventory_export

        def wrapped(inst, products, reason='stock_change'):
            if inst.id == self.instance.id:
                raise UserError('store A failed')
            return real(inst, products, reason=reason)

        with patch.object(type(self.instance), '_woo_enqueue_inventory_export', wrapped):
            self.env['stock.move']._woo_schedule_inventory_export_for_moves(move)
        other_jobs = self.env['kits.connector.sync.job'].search([
            ('instance_id', '=', other.id),
            ('job_type', '=', 'woo_export_inventory'),
        ])
        self.assertTrue(other_jobs)

    def test_webhook_signature_and_stale_product_event(self):
        body = b'{"id": 1}'
        secret = 'hook-secret'
        signature = base64.b64encode(
            hmac.new(secret.encode(), body, hashlib.sha256).digest()
        ).decode()
        self.assertTrue(self.instance._woo_verify_webhook_signature(body, signature, secret))
        self.assertFalse(self.instance._woo_verify_webhook_signature(body, 'nope', secret))
        self.assertFalse(self.instance._woo_verify_webhook_signature(body, signature, ''))

        template = self.env['product.template'].create({
            'name': 'Current Name',
            'type': 'consu',
            'list_price': 3,
            'company_id': self.company.id,
        })
        binding = self.instance._woo_bind('product.template', template.id, '84')
        binding.write({'external_data': {'date_modified': '2026-10-30T12:00:00'}})
        stale = self.env['kits.connector.sync.job'].create({
            'name': 'stale product',
            'instance_id': self.instance.id,
            'job_type': 'woo_webhook_product',
            'payload': {
                'topic': 'product.updated',
                'woo_id': 84,
                'product': self._simple_payload(
                    84, 'STALE',
                    name='Old Name',
                    date_modified_gmt='2026-10-29T12:00:00',
                ),
            },
            'state': 'pending',
        })
        summary = self.instance._woo_process_product_webhook(job=stale)
        self.assertIn('stale', summary.lower())
        self.assertEqual(template.name, 'Current Name')

        fresh = self.env['kits.connector.sync.job'].create({
            'name': 'fresh product',
            'instance_id': self.instance.id,
            'job_type': 'woo_webhook_product',
            'payload': {
                'topic': 'product.updated',
                'woo_id': 84,
                'product': self._simple_payload(
                    84, 'STALE',
                    name='New Name',
                    date_modified_gmt='2026-10-31T12:00:00',
                    stock_quantity=50,
                ),
            },
            'state': 'pending',
        })
        api = MagicMock()
        api.get_variations.return_value = iter([])
        with self._patch_api(api):
            self.instance._woo_process_product_webhook(job=fresh)
        self.assertEqual(template.name, 'New Name')
        self.assertEqual(template.product_variant_id.qty_available, 0)

    def test_explicit_export_recreate_is_single_and_repeated_export_updates(self):
        template = self.env['product.template'].create({
            'name': 'Recreate Me',
            'default_code': 'RE-1',
            'type': 'consu',
            'is_storable': True,
            'list_price': 8,
            'sale_ok': True,
            'company_id': self.company.id,
            'woo_instance_ids': [(6, 0, [self.instance.id])],
        })
        binding = self.instance._woo_bind('product.template', template.id, '85')
        binding._kits_mark_deleted_remote(message='gone')
        template.with_context(kits_woo_skip_dirty=True).write({
            'woo_instance_ids': [(4, self.instance.id)],
        })
        api = MagicMock()
        api.get_product.side_effect = ConnectorAPIError('missing', status_code=404)
        api.create_product.return_value = {'id': 850, 'images': []}
        api.update_product.return_value = {'id': 850, 'images': []}
        job = self.env['kits.connector.sync.job'].create({
            'name': 'explicit export',
            'instance_id': self.instance.id,
            'job_type': 'woo_export_products',
            'payload': {'template_ids': [template.id], 'allow_recreate': True},
            'state': 'pending',
        })
        with self._patch_api(api):
            self.instance._woo_export_products(job=job)
            self.instance._woo_export_products(job=job)
        self.assertEqual(api.create_product.call_count, 1)
        self.assertEqual(api.update_product.call_count, 1)
        binding.invalidate_recordset()
        self.assertEqual(binding.external_id, '850')
        self.assertEqual(binding.sync_state, 'synced')
