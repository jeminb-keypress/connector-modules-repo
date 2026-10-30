# -*- coding: utf-8 -*-
import json
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from odoo.addons.kits_odoo_base_connector.services.exceptions import (
    ConnectorAPIError,
    ConnectorAuthError,
)

from .common import ShopifyConnectorCommon


@tagged('post_install', '-at_install', 'kits_shopify')
class TestShopifyConnection(ShopifyConnectorCommon):

    def test_successful_connection(self):
        payload = {'data': {'shop': {'name': 'Demo Shop'}}}
        with patch.object(
            type(self.instance), '_kits_http_request_json', return_value=payload,
        ) as http:
            self.assertTrue(self.instance.action_test_connection())
        self.assertEqual(http.call_args.args[0], 'POST')
        self.assertEqual(http.call_args.args[1], '/graphql.json')
        query = http.call_args.kwargs['json_body']['query']
        self.assertIn('shop', query)
        self.assertNotIn(self.token, json.dumps(http.call_args.kwargs))
        self.assertEqual(self.instance.state, 'connected')
        self.assertFalse(self.instance.last_error)
        self.assert_token_not_stored_in_logs(self.token, self.instance)

    def test_missing_domain_or_token_does_not_call_shopify(self):
        missing_domain = self.env['kits.connector.instance'].create({
            'name': 'Shopify Missing Domain',
            'platform_type': 'shopify',
            'company_id': self.company.id,
            'shopify_api_version': '2026-10',
            'shopify_access_token': self.token,
        })
        missing_token = self.env['kits.connector.instance'].create({
            'name': 'Shopify Missing Token',
            'platform_type': 'shopify',
            'company_id': self.company.id,
            'shopify_shop_domain': 'other-store',
            'shopify_api_version': '2026-10',
        })
        self.assertEqual(missing_token.shopify_shop_domain, 'other-store.myshopify.com')
        with patch.object(type(self.instance), '_kits_http_request_json') as http:
            domain_error = self._expect_user_error(missing_domain.action_test_connection)
            token_error = self._expect_user_error(missing_token.action_test_connection)
            http.assert_not_called()
        self.assertIn('shop domain', str(domain_error).lower())
        self.assertIn('access token', str(token_error).lower())
        self.assertNotIn(self.token, str(domain_error))
        self.assertNotIn(self.token, str(token_error))
        self.assertEqual(missing_domain.state, 'error')
        self.assertEqual(missing_token.state, 'error')
        self.assert_token_not_stored_in_logs(self.token, missing_domain)
        self.assert_token_not_stored_in_logs(self.token, missing_token)

    def test_authentication_failure(self):
        def fail_auth(method, endpoint, **kwargs):
            raise ConnectorAuthError(
                'HTTP 401 calling https://demo-store.myshopify.com/admin/api/2026-10/graphql.json',
                details='{"errors":"[API] Invalid API key or access token"} %s' % self.token,
                status_code=401,
            )

        with patch.object(
            type(self.instance), '_kits_http_request_json', side_effect=fail_auth,
        ):
            message = str(self._expect_user_error(self.instance.action_test_connection))
        self.assertIn('authentication failed', message.lower())
        self.assertIn('401', message)
        self.assertNotIn(self.token, message)
        self.assertIn('[redacted]', message)
        self.assertEqual(self.instance.state, 'error')
        self.assert_token_not_stored_in_logs(self.token, self.instance, extra=message)

    def test_graphql_errors_on_http_200(self):
        payload = {
            'errors': [{
                'message': 'Syntax error near %s' % self.token,
                'extensions': {'code': 'GRAPHQL_PARSE_FAILED'},
            }],
        }
        with patch.object(
            type(self.instance), '_kits_http_request_json', return_value=payload,
        ):
            message = str(self._expect_user_error(self.instance.action_test_connection))
        self.assertIn('GRAPHQL_PARSE_FAILED', message)
        self.assertNotIn(self.token, message)
        self.assertIn('[redacted]', message)
        self.assertEqual(self.instance.state, 'error')
        self.assert_token_not_stored_in_logs(self.token, self.instance, extra=message)

    def test_empty_password_does_not_wipe_token(self):
        self.instance.write({'shopify_access_token': ''})
        self.assertEqual(self.instance.shopify_access_token, self.token)
        self.instance.write({'shopify_access_token': False})
        self.assertEqual(self.instance.shopify_access_token, self.token)
        self.instance.write({'shopify_access_token': '   '})
        self.assertEqual(self.instance.shopify_access_token, self.token)
        self.instance.write({'name': 'Test Shopify Store Renamed'})
        self.assertEqual(self.instance.shopify_access_token, self.token)

    def test_auth_header_uses_shopify_token_not_bearer_key(self):
        self.instance.write({'api_key': 'generic-key-not-for-shopify'})
        headers = self.instance._kits_get_auth_headers('POST', 'https://demo-store.myshopify.com', {}, {})
        self.assertEqual(headers.get('X-Shopify-Access-Token'), self.token)
        self.assertNotIn('Authorization', headers)

    def test_throttled_response_is_retried(self):
        responses = [
            {
                'errors': [{
                    'message': 'Throttled',
                    'extensions': {'code': 'THROTTLED'},
                }],
                'extensions': {
                    'cost': {
                        'throttleStatus': {
                            'currentlyAvailable': 0,
                            'restoreRate': 100,
                        },
                    },
                },
            },
            {'data': {'shop': {'name': 'Demo Shop'}}},
        ]
        with patch.object(
            type(self.instance), '_kits_http_request_json', side_effect=responses,
        ) as http, patch(
            'odoo.addons.kits_odoo_shopify_connector.services.shopify_api.time.sleep',
        ) as sleep:
            self.assertTrue(self.instance.action_test_connection())
        self.assertEqual(http.call_count, 2)
        sleep.assert_called()
        self.assertEqual(self.instance.state, 'connected')
        self.assert_token_not_stored_in_logs(self.token, self.instance)

    def test_custom_domain_is_rejected_and_url_is_normalized(self):
        with self.assertRaises(ValidationError):
            self.env['kits.connector.instance'].create({
                'name': 'Shopify Custom Domain',
                'platform_type': 'shopify',
                'company_id': self.company.id,
                'shopify_shop_domain': 'https://shop.example.com/admin',
            })
        self.instance.write({
            'shopify_shop_domain': 'https://Demo-Shop.myshopify.com/admin',
            'shopify_api_version': '2026-07',
        })
        self.assertEqual(self.instance.shopify_shop_domain, 'demo-shop.myshopify.com')
        self.assertEqual(
            self.instance.base_url,
            'https://demo-shop.myshopify.com/admin/api/2026-07',
        )

    def test_http_429_is_retried(self):
        calls = {'count': 0}

        def flaky(method, endpoint, **kwargs):
            calls['count'] += 1
            if calls['count'] == 1:
                raise ConnectorAPIError(
                    'HTTP 429 calling https://demo-store.myshopify.com/admin/api/2026-10/graphql.json',
                    details='{"errors":"Throttled"}',
                    status_code=429,
                )
            return {'data': {'shop': {'name': 'Demo Shop'}}}

        with patch.object(
            type(self.instance), '_kits_http_request_json', side_effect=flaky,
        ), patch(
            'odoo.addons.kits_odoo_shopify_connector.services.shopify_api.time.sleep',
        ):
            self.assertTrue(self.instance.action_test_connection())
        self.assertEqual(calls['count'], 2)
        self.assertEqual(self.instance.state, 'connected')
