# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooCustomerWaiting(WooConnectorCommon):

    def _customer(self, woo_id, email, role='customer', **extra):
        data = {
            'id': woo_id,
            'role': role,
            'email': email,
            'first_name': 'Wait',
            'last_name': str(woo_id),
            'billing': {
                'first_name': 'Wait',
                'last_name': str(woo_id),
                'email': email,
                'address_1': '1 Queue St',
                'address_2': 'Suite 2',
                'city': 'Town',
                'postcode': '1000',
                'country': 'US',
                'phone': '111',
            },
            'shipping': {
                'first_name': 'Ship',
                'last_name': str(woo_id),
                'address_1': '9 Delivery Rd',
                'city': 'Port',
                'postcode': '2000',
                'country': 'US',
            },
        }
        data.update(extra)
        return data

    def test_bulk_import_queues_then_processes(self):
        api = MagicMock()
        api.get_customers.return_value = iter([
            self._customer(801, 'queue1@example.com'),
            self._customer(802, 'queue2@example.com'),
        ])
        with self._patch_api(api):
            summary = self.instance._woo_import_customers()
        self.assertIn('queued=2', summary)
        self.assertIn('done=2', summary)
        self.assertEqual(
            self.env['kits.woo.customer.binding'].search_count([
                ('instance_id', '=', self.instance.id),
                ('external_id', 'in', ['801', '802']),
                ('active', '=', True),
            ]),
            2,
        )
        waiting = self.env['kits.woo.customer.waiting'].search([
            ('instance_id', '=', self.instance.id),
            ('external_id', 'in', ['801', '802']),
        ])
        self.assertTrue(waiting)
        self.assertTrue(all(row.state == 'done' for row in waiting))

    def test_duplicate_enqueue_refreshes_pending(self):
        Waiting = self.env['kits.woo.customer.waiting']
        first = Waiting._enqueue(
            self.instance,
            external_id=900,
            payload=self._customer(900, 'dup@example.com', first_name='Old'),
            source='bulk_import',
            event_type='update',
            trigger_batch=False,
        )
        second = Waiting._enqueue(
            self.instance,
            external_id=900,
            payload=self._customer(900, 'dup@example.com', first_name='New'),
            source='webhook',
            event_type='update',
            trigger_batch=False,
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.customer_name, 'New 900')
        self.assertEqual(
            Waiting.search_count([
                ('instance_id', '=', self.instance.id),
                ('external_id', '=', '900'),
                ('state', '=', 'pending'),
            ]),
            1,
        )

    def test_webhook_ignores_non_customer_role(self):
        job = self.env['kits.connector.sync.job'].create({
            'name': 'admin webhook',
            'instance_id': self.instance.id,
            'job_type': 'woo_webhook_customer',
            'payload': {
                'topic': 'customer.created',
                'woo_id': 910,
                'customer': self._customer(910, 'admin@example.com', role='administrator'),
            },
            'state': 'pending',
        })
        summary = self.instance._woo_process_customer_webhook(job=job)
        self.assertIn('non-customer', summary.lower())
        self.assertFalse(self.env['kits.woo.customer.binding']._find(
            self.instance, external_id='910',
        ))

    def test_ambiguous_email_goes_manual(self):
        self.env['res.partner'].create({
            'name': 'One',
            'email': 'ambig@example.com',
            'company_id': self.company.id,
        })
        self.env['res.partner'].create({
            'name': 'Two',
            'email': 'ambig@example.com',
            'company_id': self.company.id,
        })
        result = self.instance._woo_import_one_customer(
            self._customer(920, 'ambig@example.com'),
        )
        self.assertEqual(result, 'ambiguous')
        Waiting = self.env['kits.woo.customer.waiting']
        row = Waiting._enqueue(
            self.instance,
            external_id=920,
            payload=self._customer(920, 'ambig@example.com'),
            source='webhook',
            event_type='create',
            trigger_batch=False,
        )
        row._process_entries()
        row.invalidate_recordset()
        self.assertEqual(row.state, 'manual')

    def test_order_uses_billing_and_shipping_children(self):
        product = self.env['product.product'].create({
            'name': 'Order Child Addr',
            'default_code': 'ADDR-1',
            'list_price': 10,
            'type': 'consu',
            'company_id': self.company.id,
        })
        self.instance._woo_bind('product.template', product.product_tmpl_id.id, '50')
        self.instance._woo_bind('product.product', product.id, '50')
        woo_order = {
            'id': 950,
            'number': '950',
            'status': 'processing',
            'currency': 'USD',
            'customer_id': 0,
            'billing': {
                'first_name': 'Bill',
                'last_name': 'Guest',
                'email': 'billship@example.com',
                'address_1': 'Billing St 1',
                'city': 'BillCity',
                'postcode': '1111',
                'country': 'US',
            },
            'shipping': {
                'first_name': 'Ship',
                'last_name': 'Guest',
                'address_1': 'Shipping Ave 2',
                'city': 'ShipCity',
                'postcode': '2222',
                'country': 'US',
            },
            'line_items': [{
                'id': 1,
                'product_id': 50,
                'variation_id': 0,
                'quantity': 1,
                'price': 10,
                'total': '10',
                'sku': 'ADDR-1',
                'name': 'Order Child Addr',
            }],
        }
        api = MagicMock()
        with self._patch_api(api):
            result, _date = self.instance._woo_import_one_order(woo_order)
        self.assertEqual(result, 'created')
        order = self.env['sale.order'].search([('woo_order_id', '=', '950')], limit=1)
        self.assertTrue(order)
        self.assertNotEqual(order.partner_invoice_id, order.partner_id)
        self.assertNotEqual(order.partner_shipping_id, order.partner_id)
        self.assertEqual(order.partner_invoice_id.type, 'invoice')
        self.assertEqual(order.partner_shipping_id.type, 'delivery')
        self.assertFalse(self.env['kits.woo.customer.binding'].search([
            ('partner_id', '=', order.partner_id.id),
        ]))

    def test_threshold_enqueues_process_job(self):
        Waiting = self.env['kits.woo.customer.waiting']
        for idx in range(50):
            Waiting._enqueue(
                self.instance,
                external_id=1000 + idx,
                payload=self._customer(1000 + idx, f'thresh{idx}@example.com'),
                source='webhook',
                event_type='create',
                trigger_batch=False,
            )
        Waiting._maybe_trigger_batch(self.instance)
        job = self.env['kits.connector.sync.job'].search([
            ('instance_id', '=', self.instance.id),
            ('job_type', '=', 'woo_process_customer_waiting'),
            ('state', 'in', ('pending', 'running', 'draft')),
        ], limit=1)
        self.assertTrue(job)

    def test_failed_retries_then_manual_after_three_attempts(self):
        Waiting = self.env['kits.woo.customer.waiting']
        row = Waiting._enqueue(
            self.instance,
            external_id=1101,
            payload=self._customer(1101, 'retry@example.com'),
            source='bulk_import',
            event_type='update',
            trigger_batch=False,
        )

        def boom(_self, _payload, job=None):
            raise ValueError('simulated failure')

        with patch.object(
            type(self.instance), '_woo_import_one_customer', boom,
        ):
            for expected_attempt in (1, 2):
                Waiting._process_pending_for_instance(self.instance, limit=50)
                row.invalidate_recordset()
                self.assertEqual(row.state, 'failed')
                self.assertEqual(row.attempt_count, expected_attempt)
            Waiting._process_pending_for_instance(self.instance, limit=50)
            row.invalidate_recordset()
            self.assertEqual(row.state, 'manual')
            self.assertEqual(row.attempt_count, 3)
            # Manual Retry resets the counter then processes once.
            row.action_retry_failed()
            row.invalidate_recordset()
            self.assertEqual(row.state, 'failed')
            self.assertEqual(row.attempt_count, 1)

    def test_ambiguous_email_not_auto_retried(self):
        self.env['res.partner'].create({
            'name': 'One',
            'email': 'noretry@example.com',
            'company_id': self.company.id,
        })
        self.env['res.partner'].create({
            'name': 'Two',
            'email': 'noretry@example.com',
            'company_id': self.company.id,
        })
        Waiting = self.env['kits.woo.customer.waiting']
        row = Waiting._enqueue(
            self.instance,
            external_id=1102,
            payload=self._customer(1102, 'noretry@example.com'),
            source='webhook',
            event_type='create',
            trigger_batch=False,
        )
        Waiting._process_pending_for_instance(self.instance, limit=50)
        row.invalidate_recordset()
        self.assertEqual(row.state, 'manual')
        attempt = row.attempt_count
        Waiting._process_pending_for_instance(self.instance, limit=50)
        row.invalidate_recordset()
        self.assertEqual(row.state, 'manual')
        self.assertEqual(row.attempt_count, attempt)

    def test_order_ambiguous_email_does_not_create_contact(self):
        self.env['res.partner'].create({
            'name': 'Ord One',
            'email': 'orderamb@example.com',
            'company_id': self.company.id,
        })
        self.env['res.partner'].create({
            'name': 'Ord Two',
            'email': 'orderamb@example.com',
            'company_id': self.company.id,
        })
        before = self.env['res.partner'].search_count([
            ('email', '=ilike', 'orderamb@example.com'),
        ])
        api = MagicMock()
        api.get_customer.return_value = self._customer(1200, 'orderamb@example.com')
        woo_order = {
            'id': 1200,
            'customer_id': 1200,
            'billing': {
                'first_name': 'Ord',
                'last_name': 'Amb',
                'email': 'orderamb@example.com',
            },
            'shipping': {},
        }
        with self._patch_api(api):
            with self.assertRaises(UserError):
                self.instance._woo_partner_from_order(woo_order)
        after = self.env['res.partner'].search_count([
            ('email', '=ilike', 'orderamb@example.com'),
        ])
        self.assertEqual(before, after)

    def test_role_filtering_bulk_fetch_order_webhook(self):
        admin = self._customer(1301, 'adminrole@example.com', role='administrator')
        api = MagicMock()
        api.get_customers.return_value = iter([admin])
        api.get_customer.return_value = admin
        with self._patch_api(api):
            summary = self.instance._woo_import_customers()
            self.assertIn('skipped=1', summary)
            with self.assertRaises(ValueError):
                self.instance._woo_fetch_customer_by_id(1301)
            partner = self.instance._woo_partner_from_order({
                'id': 1301,
                'customer_id': 1301,
                'billing': {
                    'first_name': 'Admin',
                    'last_name': 'Role',
                    'email': 'adminrole@example.com',
                },
                'shipping': {},
            })
        self.assertTrue(partner)
        self.assertFalse(self.env['kits.woo.customer.binding']._find(
            self.instance, external_id='1301',
        ))
        job = self.env['kits.connector.sync.job'].create({
            'name': 'shop manager webhook',
            'instance_id': self.instance.id,
            'job_type': 'woo_webhook_customer',
            'payload': {
                'topic': 'customer.created',
                'woo_id': 1302,
                'customer': self._customer(1302, 'mgr@example.com', role='shop_manager'),
            },
            'state': 'pending',
        })
        summary = self.instance._woo_process_customer_webhook(job=job)
        self.assertIn('non-customer', summary.lower())

    def test_process_batch_limit_fifty(self):
        Waiting = self.env['kits.woo.customer.waiting']
        for idx in range(55):
            Waiting._enqueue(
                self.instance,
                external_id=1400 + idx,
                payload=self._customer(1400 + idx, f'batch{idx}@example.com'),
                source='bulk_import',
                event_type='update',
                trigger_batch=False,
            )
        summary = Waiting._process_pending_for_instance(self.instance, limit=50)
        self.assertIn('done=50', summary)
        pending = Waiting.search_count([
            ('instance_id', '=', self.instance.id),
            ('external_id', '>=', '1400'),
            ('external_id', '<=', '1454'),
            ('state', '=', 'pending'),
        ])
        # External ids are strings; recount by numeric range via done vs pending.
        done = Waiting.search_count([
            ('instance_id', '=', self.instance.id),
            ('state', '=', 'done'),
            ('source', '=', 'bulk_import'),
            ('external_id', 'in', [str(1400 + i) for i in range(55)]),
        ])
        left = Waiting.search_count([
            ('instance_id', '=', self.instance.id),
            ('state', '=', 'pending'),
            ('external_id', 'in', [str(1400 + i) for i in range(55)]),
        ])
        self.assertEqual(done, 50)
        self.assertEqual(left, 5)
        self.assertEqual(pending, left)

    def test_delete_then_reimport_restores_same_binding(self):
        result = self.instance._woo_import_one_customer(
            self._customer(1500, 'restore@example.com'),
        )
        self.assertEqual(result, 'created')
        binding = self.env['kits.woo.customer.binding']._find(
            self.instance, external_id='1500',
        )
        partner = binding.partner_id
        binding_id = binding.id
        self.instance._woo_delete_customer_binding(1500)
        binding.invalidate_recordset()
        self.assertFalse(binding.active)
        self.assertEqual(binding.sync_state, 'deleted_remotely')
        partner.invalidate_recordset()
        self.assertEqual(partner.woo_sync_state, 'deleted')
        self.assertFalse(partner.woo_is_store_customer)
        result = self.instance._woo_import_one_customer(
            self._customer(1500, 'restore@example.com'),
        )
        self.assertEqual(result, 'reused')
        binding.invalidate_recordset()
        self.assertEqual(binding.id, binding_id)
        self.assertTrue(binding.active)
        self.assertEqual(binding.sync_state, 'synced')
        self.assertEqual(
            self.env['kits.woo.customer.binding'].search_count([
                ('instance_id', '=', self.instance.id),
                ('external_id', '=', '1500'),
            ]),
            1,
        )
        self.assertEqual(
            self.env['res.partner'].search_count([
                ('email', '=ilike', 'restore@example.com'),
                ('parent_id', '=', False),
            ]),
            1,
        )

    def test_order_update_customer_setting_on_and_off(self):
        product = self.env['product.product'].create({
            'name': 'Cust Update Prod',
            'default_code': 'CUPD-1',
            'list_price': 10,
            'type': 'consu',
            'company_id': self.company.id,
        })
        self.instance._woo_bind('product.template', product.product_tmpl_id.id, '60')
        self.instance._woo_bind('product.product', product.id, '60')
        customer = self._customer(1600, 'updsetting@example.com')
        api = MagicMock()
        api.get_customer.return_value = customer
        woo_order = {
            'id': 1600,
            'number': '1600',
            'status': 'processing',
            'currency': 'USD',
            'customer_id': 1600,
            'billing': {
                'first_name': 'Bill',
                'last_name': 'One',
                'email': 'updsetting@example.com',
                'address_1': 'Old Bill St',
                'city': 'OldCity',
                'postcode': '1111',
                'country': 'US',
                'phone': '111',
            },
            'shipping': {
                'first_name': 'Ship',
                'last_name': 'One',
                'address_1': 'Old Ship Rd',
                'city': 'OldPort',
                'postcode': '2222',
                'country': 'US',
            },
            'line_items': [{
                'id': 1,
                'product_id': 60,
                'variation_id': 0,
                'quantity': 1,
                'price': 10,
                'total': '10',
                'sku': 'CUPD-1',
                'name': 'Cust Update Prod',
            }],
        }
        self.instance.woo_update_customer_on_order_import = False
        with self._patch_api(api):
            result, _ = self.instance._woo_import_one_order(woo_order)
        self.assertEqual(result, 'created')
        order = self.env['sale.order'].search([('woo_order_id', '=', '1600')], limit=1)
        partner = order.partner_id
        original_name = partner.name
        original_invoice = order.partner_invoice_id
        # OFF: re-import with new addresses must not refresh children/partner fields.
        woo_order['billing'] = dict(woo_order['billing'], address_1='New Bill St', city='NewCity')
        woo_order['shipping'] = dict(woo_order['shipping'], address_1='New Ship Rd', city='NewPort')
        customer['billing'] = woo_order['billing']
        customer['shipping'] = woo_order['shipping']
        with self._patch_api(api):
            result, _ = self.instance._woo_import_one_order(woo_order)
        self.assertEqual(result, 'skipped')
        order.invalidate_recordset()
        partner.invalidate_recordset()
        self.assertEqual(partner.name, original_name)
        self.assertEqual(order.partner_invoice_id, original_invoice)
        self.assertEqual(original_invoice.street, 'Old Bill St')
        # ON: refresh may add missing address children; never overwrite name/email/phone.
        self.instance.woo_update_customer_on_order_import = True
        with self._patch_api(api):
            result, _ = self.instance._woo_import_one_order(woo_order)
        self.assertEqual(result, 'skipped')
        order.invalidate_recordset()
        partner.invalidate_recordset()
        self.assertEqual(partner.name, original_name)
        self.assertEqual(partner.email, 'updsetting@example.com')
        invoice_streets = partner.child_ids.filtered(
            lambda c: c.type == 'invoice'
        ).mapped('street')
        self.assertIn('New Bill St', invoice_streets)
        self.assertIn('Old Bill St', invoice_streets)

    def test_older_webhook_event_ignored(self):
        payload_new = self._customer(
            1700, 'staleevt@example.com',
            date_modified_gmt='2026-11-05T12:00:00',
        )
        self.instance._woo_import_one_customer(payload_new)
        binding = self.env['kits.woo.customer.binding']._find(
            self.instance, external_id='1700',
        )
        self.assertTrue(binding.remote_modified_at)
        older = self._customer(
            1700, 'staleevt@example.com',
            first_name='Older',
            date_modified_gmt='2026-11-04T12:00:00',
            billing={
                'first_name': 'Older',
                'last_name': '1700',
                'email': 'staleevt@example.com',
                'address_1': 'Should Not Appear',
                'city': 'Nowhere',
                'postcode': '0000',
                'country': 'US',
            },
        )
        Waiting = self.env['kits.woo.customer.waiting']
        row = Waiting._enqueue(
            self.instance,
            external_id=1700,
            payload=older,
            source='webhook',
            event_type='update',
            trigger_batch=False,
        )
        row._process_entries()
        row.invalidate_recordset()
        self.assertEqual(row.state, 'skipped')
        partner = binding.partner_id
        self.assertEqual(partner.name, 'Wait 1700')
        self.assertNotIn(
            'Should Not Appear',
            partner.child_ids.mapped('street'),
        )

    def test_enqueue_does_not_reset_processing_row(self):
        Waiting = self.env['kits.woo.customer.waiting']
        row = Waiting._enqueue(
            self.instance,
            external_id=1800,
            payload=self._customer(1800, 'lock@example.com', first_name='First'),
            source='webhook',
            event_type='update',
            trigger_batch=False,
        )
        row.write({'state': 'processing', 'attempt_count': 1})
        follow = Waiting._enqueue(
            self.instance,
            external_id=1800,
            payload=self._customer(1800, 'lock@example.com', first_name='Second'),
            source='webhook',
            event_type='update',
            trigger_batch=False,
        )
        row.invalidate_recordset()
        self.assertEqual(row.state, 'processing')
        self.assertNotEqual(follow.id, row.id)
        self.assertEqual(follow.state, 'pending')
        self.assertEqual(follow.customer_name, 'Second 1800')
