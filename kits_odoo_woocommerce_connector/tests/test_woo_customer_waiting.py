# -*- coding: utf-8 -*-
from unittest.mock import MagicMock

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
