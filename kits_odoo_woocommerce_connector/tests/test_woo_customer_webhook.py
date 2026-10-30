# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooCustomerWebhook(WooConnectorCommon):

    def setUp(self):
        super().setUp()
        self.instance.write({
            'woo_webhook_public_base_url': 'https://odoo.test',
            'woo_webhook_secret': 'test-webhook-secret',
        })

    def _sign(self, body: bytes, secret: str = 'test-webhook-secret') -> str:
        digest = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).digest()
        return base64.b64encode(digest).decode('ascii')

    def test_signature_verification(self):
        body = b'{"id": 1}'
        good = self._sign(body)
        self.assertTrue(
            self.instance._woo_verify_webhook_signature(
                body, good, self.instance.woo_webhook_secret,
            )
        )
        self.assertFalse(
            self.instance._woo_verify_webhook_signature(
                body, good, 'wrong-secret',
            )
        )
        self.assertFalse(
            self.instance._woo_verify_webhook_signature(
                body, 'bad', self.instance.woo_webhook_secret,
            )
        )
        self.assertFalse(
            self.instance._woo_verify_webhook_signature(
                body, False, self.instance.woo_webhook_secret,
            )
        )

    def test_endpoint_url_uses_instance_id(self):
        self.assertEqual(
            self.instance.woo_webhook_endpoint_url,
            f'https://odoo.test/kits_woo/webhook/{self.instance.id}/customers',
        )

    def test_created_webhook_creates_partner_binding_only(self):
        other = self.env['kits.connector.instance'].create({
            'name': 'Webhook Store B',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://store-b.test',
            'woo_consumer_key': 'ck_b',
            'woo_consumer_secret': 'cs_b',
            'state': 'connected',
        })
        existing = self.env['res.partner'].create({
            'name': 'Unrelated',
            'email': 'hook@example.com',
            'company_id': self.company.id,
        })
        payload = {
            'topic': 'customer.created',
            'delivery_id': 'del-1',
            'woo_id': 501,
            'body_hash': 'abc',
            'customer': {
                'id': 501,
                'email': 'hook@example.com',
                'first_name': 'Hook',
                'last_name': 'Created',
                'billing': {'email': 'hook@example.com', 'phone': '555'},
                'shipping': {},
            },
        }
        job = self.env['kits.connector.sync.job'].create({
            'name': 'webhook created',
            'instance_id': self.instance.id,
            'job_type': 'woo_webhook_customer',
            'payload': payload,
            'state': 'pending',
        })
        summary = self.instance._woo_process_customer_webhook(job=job)
        self.assertIn('customer.created', summary)
        binding = self.env['kits.connector.binding']._kits_find_binding(
            self.instance, 'res.partner', external_id='501',
        )
        self.assertTrue(binding)
        self.assertEqual(binding.sync_state, 'synced')
        partner = binding._kits_get_odoo_record()
        self.assertNotEqual(partner.id, existing.id)
        self.assertEqual(partner.email, 'hook@example.com')
        self.assertEqual(partner.name, 'Hook Created')
        other_bind = self.env['kits.connector.binding'].search([
            ('instance_id', '=', other.id),
            ('odoo_model', '=', 'res.partner'),
            ('external_id', '=', '501'),
        ])
        self.assertFalse(other_bind)

    def test_updated_webhook_updates_mapped_partner(self):
        partner = self.env['res.partner'].create({
            'name': 'Before Update',
            'email': 'upd@example.com',
            'company_id': self.company.id,
        })
        self.instance._woo_bind('res.partner', partner.id, '502')
        job = self.env['kits.connector.sync.job'].create({
            'name': 'webhook updated',
            'instance_id': self.instance.id,
            'job_type': 'woo_webhook_customer',
            'payload': {
                'topic': 'customer.updated',
                'delivery_id': 'del-2',
                'woo_id': 502,
                'customer': {
                    'id': 502,
                    'email': 'upd@example.com',
                    'first_name': 'After',
                    'last_name': 'Update',
                    'billing': {},
                    'shipping': {},
                },
            },
            'state': 'pending',
        })
        self.instance._woo_process_customer_webhook(job=job)
        partner.invalidate_recordset()
        self.assertEqual(partner.name, 'After Update')

    def test_deleted_webhook_marks_binding_not_partner(self):
        partner = self.env['res.partner'].create({
            'name': 'Keep Me',
            'email': 'del@example.com',
            'company_id': self.company.id,
        })
        binding = self.instance._woo_bind('res.partner', partner.id, '503')
        job = self.env['kits.connector.sync.job'].create({
            'name': 'webhook deleted',
            'instance_id': self.instance.id,
            'job_type': 'woo_webhook_customer',
            'payload': {
                'topic': 'customer.deleted',
                'delivery_id': 'del-3',
                'woo_id': 503,
                'customer': {'id': 503},
            },
            'state': 'pending',
        })
        summary = self.instance._woo_process_customer_webhook(job=job)
        self.assertIn('deleted remote', summary.lower())
        binding.invalidate_recordset()
        self.assertEqual(binding.sync_state, 'deleted')
        self.assertTrue(partner.exists())
        self.assertEqual(partner.name, 'Keep Me')

    def test_enqueue_idempotent_on_delivery_id(self):
        body = {'id': 600, 'email': 'idemp@example.com', 'first_name': 'A', 'last_name': 'B'}
        job1 = self.instance._woo_enqueue_customer_webhook(
            topic='customer.updated',
            delivery_id='same-delivery',
            woo_id=600,
            payload_body=body,
            body_hash='hash-1',
        )
        job2 = self.instance._woo_enqueue_customer_webhook(
            topic='customer.updated',
            delivery_id='same-delivery',
            woo_id=600,
            payload_body=body,
            body_hash='hash-2',
        )
        self.assertEqual(job1.id, job2.id)

    def test_enqueue_supersedes_stale_pending(self):
        body = {'id': 601, 'email': 'stale@example.com'}
        old = self.instance._woo_enqueue_customer_webhook(
            topic='customer.updated',
            delivery_id='old-del',
            woo_id=601,
            payload_body=body,
            body_hash='h-old',
        )
        self.assertEqual(old.state, 'pending')
        new = self.instance._woo_enqueue_customer_webhook(
            topic='customer.updated',
            delivery_id='new-del',
            woo_id=601,
            payload_body=dict(body, first_name='Newer'),
            body_hash='h-new',
        )
        old.invalidate_recordset()
        self.assertEqual(old.state, 'cancelled')
        self.assertEqual(new.state, 'pending')
        self.assertNotEqual(old.id, new.id)

    def test_job_handler_dispatches(self):
        partner = self.env['res.partner'].create({
            'name': 'Job Handler',
            'email': 'jobh@example.com',
            'company_id': self.company.id,
        })
        binding = self.instance._woo_bind('res.partner', partner.id, '700')
        job = self.env['kits.connector.sync.job'].create({
            'name': 'dispatch webhook',
            'instance_id': self.instance.id,
            'job_type': 'woo_webhook_customer',
            'payload': {
                'topic': 'customer.deleted',
                'woo_id': 700,
                'customer': {'id': 700},
            },
            'state': 'pending',
        })
        job.action_run_now()
        self.assertEqual(job.state, 'done')
        binding.invalidate_recordset()
        self.assertEqual(binding.sync_state, 'deleted')
        self.assertTrue(partner.exists())

    def test_register_webhooks_calls_api(self):
        api = MagicMock()
        api.create_webhook.side_effect = [
            {'id': 11}, {'id': 12}, {'id': 13},
        ]
        with self._patch_api(api):
            self.instance.action_woo_register_customer_webhooks()
        self.assertEqual(api.create_webhook.call_count, 3)
        self.assertTrue(self.instance.woo_webhook_enabled)
        self.assertEqual(
            set((self.instance.woo_webhook_ids or {}).keys()),
            {'customer.created', 'customer.updated', 'customer.deleted'},
        )
        topics = [c.args[0]['topic'] for c in api.create_webhook.call_args_list]
        self.assertEqual(
            sorted(topics),
            ['customer.created', 'customer.deleted', 'customer.updated'],
        )
        for call in api.create_webhook.call_args_list:
            payload = call.args[0]
            self.assertEqual(
                payload['delivery_url'],
                self.instance.woo_webhook_endpoint_url,
            )
            self.assertEqual(payload['secret'], self.instance.woo_webhook_secret)


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooCustomerWebhookHttp(HttpCase):

    def setUp(self):
        super().setUp()
        warehouse = self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1,
        )
        self.instance = self.env['kits.connector.instance'].create({
            'name': 'HTTP Woo Webhook Store',
            'platform_type': 'woocommerce',
            'company_id': self.env.company.id,
            'base_url': 'https://woo.http.test',
            'woo_consumer_key': 'ck_http',
            'woo_consumer_secret': 'cs_http',
            'warehouse_id': warehouse.id if warehouse else False,
            'state': 'connected',
            'woo_webhook_public_base_url': 'http://127.0.0.1',
            'woo_webhook_secret': 'http-secret',
        })

    def _sign(self, body: bytes) -> str:
        digest = hmac.new(b'http-secret', body, hashlib.sha256).digest()
        return base64.b64encode(digest).decode('ascii')

    def test_http_rejects_bad_signature(self):
        url = f'/kits_woo/webhook/{self.instance.id}/customers'
        body = json.dumps({'id': 1, 'email': 'x@y.com'}).encode()
        response = self.url_open(
            url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-WC-Webhook-Signature': 'invalid',
                'X-WC-Webhook-Topic': 'customer.created',
                'X-WC-Webhook-Delivery-ID': 'http-1',
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_http_accepts_valid_signature_and_enqueues(self):
        url = f'/kits_woo/webhook/{self.instance.id}/customers'
        payload = {
            'id': 880,
            'email': 'http-hook@example.com',
            'first_name': 'Http',
            'last_name': 'Hook',
            'billing': {},
            'shipping': {},
        }
        body = json.dumps(payload).encode()
        response = self.url_open(
            url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-WC-Webhook-Signature': self._sign(body),
                'X-WC-Webhook-Topic': 'customer.created',
                'X-WC-Webhook-Delivery-ID': 'http-2',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'accepted', response.content)
        job = self.env['kits.connector.sync.job'].search([
            ('instance_id', '=', self.instance.id),
            ('job_type', '=', 'woo_webhook_customer'),
        ], limit=1)
        self.assertTrue(job)
        self.assertEqual(job.state, 'pending')
        self.assertEqual((job.payload or {}).get('woo_id'), 880)

    def test_http_get_probe_ok(self):
        url = f'/kits_woo/webhook/{self.instance.id}/customers'
        response = self.url_open(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'ok', response.content)
