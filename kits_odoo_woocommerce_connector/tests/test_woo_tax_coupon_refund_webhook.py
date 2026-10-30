# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import json
from unittest.mock import MagicMock

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase
from odoo.fields import Command

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooTaxMapping(WooConnectorCommon):

    def _sale_tax(self, amount=10.0, name='Woo Test Tax 10%'):
        return self.env['account.tax'].create({
            'name': name,
            'amount': amount,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': self.company.id,
        })

    def test_fetch_tax_rates_creates_maps_without_inventing_taxes(self):
        api = MagicMock()
        api.get_taxes.return_value = iter([
            {'id': 7, 'rate': '20.0000', 'name': 'VAT', 'class': ''},
            {'id': 8, 'rate': '0.0000', 'name': 'Zero', 'class': 'zero-rate'},
        ])
        tax_count_before = self.env['account.tax'].search_count([])
        with self._patch_api(api):
            created, updated = self.instance._woo_fetch_tax_rates()
        self.assertEqual(created, 2)
        self.assertEqual(updated, 0)
        self.assertEqual(self.env['account.tax'].search_count([]), tax_count_before)
        maps = self.env['kits.woo.tax.map'].search([('instance_id', '=', self.instance.id)])
        self.assertEqual(len(maps), 2)
        zero = maps.filtered(lambda m: m.woo_rate_id == '8')
        self.assertTrue(zero.is_zero_rated)

    def test_mapped_tax_applied_and_missing_fails_in_mapped_mode(self):
        tax = self._sale_tax()
        self.env['kits.woo.tax.map'].create({
            'instance_id': self.instance.id,
            'woo_rate_id': '11',
            'woo_tax_class': '',
            'woo_rate': 10.0,
            'tax_id': tax.id,
        })
        self.instance.woo_tax_mapping_mode = 'mapped'
        template = self.env['product.template'].create({
            'name': 'Taxed Product',
            'default_code': 'TAX-SKU',
            'type': 'consu',
            'list_price': 100.0,
            'sale_ok': True,
            'company_id': self.company.id,
            'taxes_id': [Command.clear()],
        })
        variant = template.product_variant_id
        self.instance._woo_bind('product.template', template.id, 901)
        self.instance._woo_bind('product.product', variant.id, 901)

        order = {
            'id': 9101,
            'number': '9101',
            'status': 'processing',
            'currency': self.company.currency_id.name,
            'customer_id': 0,
            'date_created': '2026-01-02T10:00:00',
            'date_created_gmt': '2026-01-02T10:00:00',
            'prices_include_tax': False,
            'discount_total': '0',
            'total': '110.00',
            'total_tax': '10.00',
            'billing': {
                'first_name': 'Tax', 'last_name': 'Buyer',
                'email': 'taxbuyer@example.com',
                'address_1': '1 Tax St', 'city': 'City', 'postcode': '1',
                'country': self.company.country_id.code or 'US',
            },
            'shipping': {},
            'line_items': [{
                'id': 1,
                'name': 'Taxed Product',
                'product_id': 901,
                'variation_id': 0,
                'quantity': 1,
                'price': 100.0,
                'sku': 'TAX-SKU',
                'total': '100.00',
                'tax_class': '',
                'taxes': [{'id': 11, 'total': '10', 'subtotal': '10'}],
            }],
            'shipping_lines': [],
            'fee_lines': [],
            'tax_lines': [{'id': 11, 'rate_id': 11, 'label': 'VAT', 'tax_total': '10'}],
        }
        result, _ = self.instance._woo_import_one_order(order)
        self.assertEqual(result, 'created')
        so = self.env['sale.order'].search([
            ('woo_order_id', '=', '9101'),
            ('woo_instance_id', '=', self.instance.id),
        ], limit=1)
        self.assertTrue(so)
        line = so.order_line.filtered(lambda l: l.product_id == variant)
        self.assertEqual(line.tax_ids, tax)

        # Missing mapping fails in mapped mode
        order2 = dict(order, id=9102, number='9102', line_items=[{
            **order['line_items'][0],
            'taxes': [{'id': 99, 'total': '10', 'subtotal': '10'}],
        }])
        with self.assertRaises(UserError):
            self.instance._woo_import_one_order(order2)

    def test_zero_tax_and_fallback_clear(self):
        self.env['kits.woo.tax.map'].create({
            'instance_id': self.instance.id,
            'woo_rate_id': '0',
            'woo_tax_class': '',
            'woo_rate': 0.0,
            'is_zero_rated': True,
        })
        self.instance.woo_tax_mapping_mode = 'mapped'
        template = self.env['product.template'].create({
            'name': 'Zero Product',
            'default_code': 'ZERO-SKU',
            'type': 'consu',
            'list_price': 50.0,
            'sale_ok': True,
            'company_id': self.company.id,
        })
        self.instance._woo_bind('product.template', template.id, 902)
        self.instance._woo_bind('product.product', template.product_variant_id.id, 902)
        order = {
            'id': 9201,
            'number': '9201',
            'status': 'processing',
            'currency': self.company.currency_id.name,
            'customer_id': 0,
            'date_created': '2026-01-03T10:00:00',
            'date_created_gmt': '2026-01-03T10:00:00',
            'prices_include_tax': False,
            'discount_total': '0',
            'total': '50.00',
            'total_tax': '0.00',
            'billing': {
                'first_name': 'Z', 'last_name': 'E', 'email': 'z@example.com',
                'address_1': '1', 'city': 'C', 'postcode': '1',
                'country': self.company.country_id.code or 'US',
            },
            'shipping': {},
            'line_items': [{
                'id': 1, 'name': 'Zero', 'product_id': 902, 'variation_id': 0,
                'quantity': 1, 'price': 50.0, 'sku': 'ZERO-SKU', 'total': '50.00',
                'tax_class': '', 'taxes': [],
            }],
            'shipping_lines': [],
            'fee_lines': [],
            'tax_lines': [],
        }
        self.instance._woo_import_one_order(order)
        so = self.env['sale.order'].search([('woo_order_id', '=', '9201')], limit=1)
        self.assertFalse(so.order_line[0].tax_ids)

    def test_multi_instance_tax_isolation(self):
        tax_a = self._sale_tax(10, 'Store A Tax')
        tax_b = self._sale_tax(5, 'Store B Tax')
        other = self.env['kits.connector.instance'].create({
            'name': 'Tax Store B',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://tax-b.test',
            'woo_consumer_key': 'ck',
            'woo_consumer_secret': 'cs',
            'warehouse_id': self.warehouse.id if self.warehouse else False,
            'state': 'connected',
            'woo_tax_mapping_mode': 'mapped',
        })
        self.env['kits.woo.tax.map'].create({
            'instance_id': self.instance.id,
            'woo_rate_id': '1',
            'woo_rate': 10,
            'tax_id': tax_a.id,
        })
        self.env['kits.woo.tax.map'].create({
            'instance_id': other.id,
            'woo_rate_id': '1',
            'woo_rate': 5,
            'tax_id': tax_b.id,
        })
        taxes_a, err_a = self.instance._woo_resolve_line_taxes(
            {'taxes': [{'id': 1, 'total': '1'}], 'tax_class': ''},
            {'prices_include_tax': False},
        )
        taxes_b, err_b = other._woo_resolve_line_taxes(
            {'taxes': [{'id': 1, 'total': '1'}], 'tax_class': ''},
            {'prices_include_tax': False},
        )
        self.assertFalse(err_a)
        self.assertFalse(err_b)
        self.assertEqual(taxes_a, tax_a)
        self.assertEqual(taxes_b, tax_b)


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooCouponRestrictions(WooConnectorCommon):

    def test_coupon_product_restrictions_via_bindings(self):
        tmpl = self.env['product.template'].create({
            'name': 'Coupon Product',
            'default_code': 'CPN-1',
            'type': 'consu',
            'list_price': 10,
            'sale_ok': True,
            'company_id': self.company.id,
        })
        product = tmpl.product_variant_id
        self.instance._woo_bind('product.product', product.id, 44)
        self.instance._woo_bind('product.template', tmpl.id, 44)

        other = self.env['kits.connector.instance'].create({
            'name': 'Coupon Store B',
            'platform_type': 'woocommerce',
            'company_id': self.company.id,
            'base_url': 'https://cpn-b.test',
            'woo_consumer_key': 'ck',
            'woo_consumer_secret': 'cs',
            'state': 'connected',
        })
        other_tmpl = self.env['product.template'].create({
            'name': 'Other Store Product',
            'default_code': 'CPN-OTHER',
            'type': 'consu',
            'list_price': 10,
            'sale_ok': True,
            'company_id': self.company.id,
        })
        other._woo_bind('product.product', other_tmpl.product_variant_id.id, 44)

        woo = {
            'id': 70,
            'code': 'ONLY44',
            'discount_type': 'percent',
            'amount': '5',
            'product_ids': [44],
            'excluded_product_ids': [999],
            'product_categories': [],
            'excluded_product_categories': [],
        }
        self.instance._woo_import_one_coupon(woo)
        coupon = self.env['kits.woo.coupon'].search([
            ('instance_id', '=', self.instance.id),
            ('code', '=', 'ONLY44'),
        ])
        self.assertEqual(coupon.product_ids, product)
        self.assertTrue(coupon.restriction_error)
        self.assertIn('excluded_product:999', coupon.unresolved_restriction_ids)
        self.assertEqual(coupon.product_ids_text, '44')
        self.assertEqual(coupon.excluded_product_ids_text, '999')

        with self.assertRaises(UserError):
            self.instance._woo_export_one_coupon(coupon)

        # Clear unresolved exclusion and export successfully
        coupon.write({
            'excluded_product_ids_text': False,
            'unresolved_restriction_ids': False,
            'restriction_error': False,
            'excluded_product_ids': [Command.clear()],
        })
        api = MagicMock()
        api.update_coupon.return_value = {'id': 70}
        with self._patch_api(api):
            result = self.instance._woo_export_one_coupon(coupon)
        self.assertEqual(result, 'updated')
        payload = api.update_coupon.call_args[0][1]
        self.assertEqual(payload['product_ids'], [44])

        # Same Woo id on other store must not pull store A's product
        other._woo_import_one_coupon(dict(woo, id=71, code='ONLY44B', excluded_product_ids=[]))
        other_coupon = self.env['kits.woo.coupon'].search([
            ('instance_id', '=', other.id), ('code', '=', 'ONLY44B'),
        ])
        self.assertEqual(other_coupon.product_ids, other_tmpl.product_variant_id)
        self.assertNotEqual(other_coupon.product_ids, product)

    def test_coupon_deleted_binding_not_silently_dropped(self):
        tmpl = self.env['product.template'].create({
            'name': 'Deleted Bound',
            'type': 'consu',
            'list_price': 1,
            'sale_ok': True,
        })
        binding = self.instance._woo_bind('product.product', tmpl.product_variant_id.id, 88)
        binding._kits_mark_deleted_remote(message='gone')
        self.instance._woo_import_one_coupon({
            'id': 72,
            'code': 'DEL88',
            'discount_type': 'fixed_cart',
            'amount': '1',
            'product_ids': [88],
            'excluded_product_ids': [],
        })
        coupon = self.env['kits.woo.coupon'].search([
            ('instance_id', '=', self.instance.id), ('code', '=', 'DEL88'),
        ])
        self.assertFalse(coupon.product_ids)
        self.assertTrue(coupon.restriction_error)
        self.assertEqual(coupon.product_ids_text, '88')


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooRefundSync(WooConnectorCommon):

    def _order_with_invoice(self, woo_order_id='9301'):
        tmpl = self.env['product.template'].create({
            'name': 'Refundable',
            'default_code': 'REF-1',
            'type': 'consu',
            'list_price': 100,
            'sale_ok': True,
            'invoice_policy': 'order',
            'company_id': self.company.id,
            'taxes_id': [Command.clear()],
        })
        product = tmpl.product_variant_id
        self.instance._woo_bind('product.product', product.id, 501)
        self.instance._woo_bind('product.template', tmpl.id, 501)
        partner = self.env['res.partner'].create({
            'name': 'Refund Partner',
            'email': f'refund{woo_order_id}@example.com',
        })
        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'company_id': self.company.id,
            'warehouse_id': self.warehouse.id,
            'woo_instance_id': self.instance.id,
            'woo_order_id': str(woo_order_id),
            'order_line': [Command.create({
                'product_id': product.id,
                'product_uom_qty': 1,
                'price_unit': 100,
                'tax_ids': [Command.clear()],
            })],
        })
        order.action_confirm()
        self.instance._woo_bind('sale.order', order.id, woo_order_id)
        moves = order._create_invoices()
        moves.action_post()
        return order, moves

    def test_refund_idempotent_and_no_invoice_skipped(self):
        order, invoices = self._order_with_invoice('9301')
        woo_order = {
            'id': 9301,
            'currency': order.currency_id.name,
            'status': 'refunded',
            'refunds': [{'id': 501, 'total': '-40.00'}],
        }
        api = MagicMock()
        api.get_order_refunds.return_value = iter([{
            'id': 501,
            'amount': '40.00',
            'reason': 'partial',
            'line_items': [],
        }])
        with self._patch_api(api):
            self.instance._woo_sync_order_refunds(order, woo_order)
            self.instance._woo_sync_order_refunds(order, woo_order)
        refunds = self.env['kits.woo.refund'].search([
            ('instance_id', '=', self.instance.id),
            ('woo_refund_id', '=', '501'),
        ])
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds.state, 'done')
        self.assertTrue(refunds.credit_note_id)
        credits = self.env['account.move'].search([
            ('move_type', '=', 'out_refund'),
            ('reversed_entry_id', 'in', invoices.ids),
        ])
        self.assertEqual(len(credits), 1)

        # No invoice → skipped, no invented invoice
        bare = self.env['sale.order'].create({
            'partner_id': order.partner_id.id,
            'company_id': self.company.id,
            'warehouse_id': self.warehouse.id,
            'woo_instance_id': self.instance.id,
            'woo_order_id': '9302',
            'order_line': [Command.create({
                'product_id': order.order_line[0].product_id.id,
                'product_uom_qty': 1,
                'price_unit': 10,
                'tax_ids': [Command.clear()],
            })],
        })
        bare.action_confirm()
        with self._patch_api(api):
            self.instance._woo_sync_order_refunds(bare, {
                'id': 9302, 'currency': bare.currency_id.name, 'status': 'refunded',
                'refunds': [{'id': 777, 'total': '-10'}],
            })
        skipped = self.env['kits.woo.refund'].search([
            ('woo_refund_id', '=', '777'), ('instance_id', '=', self.instance.id),
        ])
        self.assertEqual(skipped.state, 'skipped')
        self.assertFalse(skipped.credit_note_id)

    def test_excessive_refund_goes_to_review(self):
        order, invoices = self._order_with_invoice('9401')
        api = MagicMock()
        api.get_order_refunds.return_value = iter([{
            'id': 601, 'amount': '9999.00', 'line_items': [],
        }])
        with self._patch_api(api):
            self.instance._woo_sync_order_refunds(order, {
                'id': 9401,
                'currency': order.currency_id.name,
                'status': 'refunded',
                'refunds': [{'id': 601, 'total': '-9999'}],
            })
        refund = self.env['kits.woo.refund'].search([
            ('woo_refund_id', '=', '601'), ('instance_id', '=', self.instance.id),
        ])
        self.assertEqual(refund.state, 'review')
        self.assertFalse(refund.credit_note_id)


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooWebhookDbAllowlist(HttpCase):

    def setUp(self):
        super().setUp()
        warehouse = self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1,
        )
        self.instance = self.env['kits.connector.instance'].create({
            'name': 'Allowlist Woo Store',
            'platform_type': 'woocommerce',
            'company_id': self.env.company.id,
            'base_url': 'https://woo.allow.test',
            'woo_consumer_key': 'ck_allow',
            'woo_consumer_secret': 'cs_allow',
            'warehouse_id': warehouse.id if warehouse else False,
            'state': 'connected',
            'woo_webhook_public_base_url': 'http://127.0.0.1',
            'woo_webhook_secret': 'http-secret',
        })

    def _sign(self, body: bytes, secret: str = 'http-secret') -> str:
        digest = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).digest()
        return base64.b64encode(digest).decode('ascii')

    def _set_allowlist(self, value):
        ICP = self.env['ir.config_parameter'].sudo()
        if hasattr(ICP, 'set_str'):
            ICP.set_str('kits_woo.webhook_db_allowlist', value or '')
            self.addCleanup(ICP.set_str, 'kits_woo.webhook_db_allowlist', '')
        else:
            ICP.set_param('kits_woo.webhook_db_allowlist', value or '')
            self.addCleanup(ICP.set_param, 'kits_woo.webhook_db_allowlist', '')

    def test_allowlist_rejects_unknown_database(self):
        self._set_allowlist('definitely-not-this-db')
        url = f'/kits_woo/webhook/{self.instance.id}/products'
        body = json.dumps({'id': 1, 'name': 'X'}).encode()
        response = self.url_open(
            url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-WC-Webhook-Signature': self._sign(body),
                'X-WC-Webhook-Topic': 'product.updated',
                'X-WC-Webhook-Delivery-ID': 'allowlist-1',
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_allowlist_accepts_current_database(self):
        self._set_allowlist(self.env.cr.dbname)
        url = f'/kits_woo/webhook/{self.instance.id}/products'
        body = json.dumps({
            'id': 12345,
            'name': 'Webhook Product',
            'type': 'simple',
            'status': 'publish',
            'sku': 'WH-ALLOW',
            'regular_price': '9',
            'manage_stock': True,
            'stock_quantity': 999,
        }).encode()
        response = self.url_open(
            url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-WC-Webhook-Signature': self._sign(body),
                'X-WC-Webhook-Topic': 'product.updated',
                'X-WC-Webhook-Delivery-ID': 'allowlist-ok',
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('accepted', response.text)

    def test_invalid_signature_still_401(self):
        self._set_allowlist(self.env.cr.dbname)
        url = f'/kits_woo/webhook/{self.instance.id}/orders'
        body = b'{"id": 1}'
        response = self.url_open(
            url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-WC-Webhook-Signature': 'not-valid',
                'X-WC-Webhook-Topic': 'order.updated',
            },
        )
        self.assertEqual(response.status_code, 401)
