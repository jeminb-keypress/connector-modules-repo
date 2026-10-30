# -*- coding: utf-8 -*-
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install', 'kits_shopify')
class ShopifyConnectorCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.token = 'shpat_unit_test_value'
        cls.instance = cls.env['kits.connector.instance'].create({
            'name': 'Test Shopify Store',
            'platform_type': 'shopify',
            'company_id': cls.company.id,
            'shopify_shop_domain': 'demo-store.myshopify.com',
            'shopify_api_version': '2026-10',
            'shopify_access_token': cls.token,
        })

    def _expect_user_error(self, func):
        """Catch UserError without Odoo's assertRaises savepoint rollback."""
        try:
            func()
        except UserError as exc:
            return exc
        self.fail('Expected UserError was not raised.')

    def assert_token_not_stored_in_logs(self, token, instance, extra=''):
        logs = self.env['kits.connector.log'].search([
            ('instance_id', '=', instance.id),
        ])
        chunks = [extra or '', instance.last_error or '']
        for log in logs:
            chunks.extend([
                log.name or '',
                log.message or '',
                log.details or '',
                log.request_data or '',
                log.response_data or '',
            ])
        blob = '\n'.join(chunks)
        self.assertNotIn(token, blob)
        self.assertNotIn(token, instance.last_error or '')
