# -*- coding: utf-8 -*-
from unittest.mock import MagicMock

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooProductWaiting(WooConnectorCommon):

    def _simple_product(self, woo_id=501, sku='WAIT-501', **extra):
        data = {
            'id': woo_id,
            'name': f'Waiting Product {woo_id}',
            'type': 'simple',
            'status': 'publish',
            'sku': sku,
            'regular_price': '25.00',
            'price': '25.00',
            'description': '<p>Long</p>',
            'short_description': 'Short',
            'categories': [],
            'tags': [],
            'images': [],
            'date_modified': '2026-11-13T10:00:00',
        }
        data.update(extra)
        return data

    def test_waiting_dedupe_and_retry(self):
        Waiting = self.env['kits.woo.product.waiting']
        payload = self._simple_product()
        row1 = Waiting._enqueue(
            self.instance, external_id=501, payload=payload, source='bulk_import',
            trigger_batch=False,
        )
        row2 = Waiting._enqueue(
            self.instance, external_id=501, payload=payload, source='webhook',
            trigger_batch=False,
        )
        self.assertEqual(row1, row2)
        self.assertEqual(
            Waiting.search_count([
                ('instance_id', '=', self.instance.id),
                ('external_id', '=', '501'),
                ('state', '=', 'draft'),
            ]),
            1,
        )
        row1.write({'state': 'failed', 'attempt_count': 3, 'last_error': 'boom'})
        api = MagicMock()
        with self._patch_api(api):
            row1.action_retry_failed()
        self.assertEqual(row1.state, 'done')
        self.assertTrue(row1.product_tmpl_id)

    def test_bulk_import_queues_and_creates(self):
        api = MagicMock()
        api.get_products.side_effect = lambda **_kw: iter([self._simple_product()])
        api.get_variations.side_effect = lambda *_a, **_kw: iter([])
        with self._patch_api(api):
            summary = self.instance._woo_import_products()
        self.assertIn('queued=1', summary)
        self.assertIn('created=1', summary)
        self.assertEqual(
            self.env['kits.woo.product.binding'].search_count([
                ('instance_id', '=', self.instance.id),
                ('external_id', '=', '501'),
            ]),
            1,
        )

    def test_auto_create_disabled_fails_waiting_row(self):
        self.instance.woo_create_products = False
        Waiting = self.env['kits.woo.product.waiting']
        row = Waiting._enqueue(
            self.instance,
            external_id=503,
            payload=self._simple_product(503, 'NO-MATCH-503'),
            trigger_batch=False,
        )
        api = MagicMock()
        with self._patch_api(api):
            Waiting._process_pending_for_instance(self.instance, limit=10)
        row.invalidate_recordset()
        self.assertEqual(row.state, 'failed')
        self.assertIn('Automatically Create Products', row.last_error or '')

    def test_weight_conversion_kg_to_lbs(self):
        self.env['ir.config_parameter'].sudo().set_str('kits_woo.odoo_weight_unit', 'lbs')
        self.instance.woo_weight_unit = 'kg'
        self.instance.woo_sync_weight = True
        converted = self.instance._woo_convert_weight(1.0)
        self.assertAlmostEqual(converted, 2.20462, places=3)

    def test_pricelist_regular_and_sale(self):
        regular = self.env['product.pricelist'].create({
            'name': 'Woo Regular',
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
        })
        sale = self.env['product.pricelist'].create({
            'name': 'Woo Sale',
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
        })
        self.instance.woo_regular_pricelist_id = regular
        self.instance.woo_sale_pricelist_id = sale
        self.instance.woo_sync_price = True
        api = MagicMock()
        product = self._simple_product(
            601, 'PRICE-601',
            regular_price='40.00',
            sale_price='30.00',
            date_on_sale_from='2026-11-01T00:00:00',
            date_on_sale_to='2026-11-30T23:59:59',
        )
        api.get_products.side_effect = lambda **_kw: iter([product])
        with self._patch_api(api):
            self.instance._woo_import_products()
        variant = self.env['kits.woo.variant.binding']._find(
            self.instance, external_id='601',
        ).product_id
        regular_item = self.env['product.pricelist.item'].search([
            ('pricelist_id', '=', regular.id),
            ('product_id', '=', variant.id),
        ], limit=1)
        sale_item = self.env['product.pricelist.item'].search([
            ('pricelist_id', '=', sale.id),
            ('product_id', '=', variant.id),
        ], limit=1)
        self.assertTrue(regular_item)
        self.assertAlmostEqual(regular_item.fixed_price, 40.0)
        self.assertTrue(sale_item)
        self.assertAlmostEqual(sale_item.fixed_price, 30.0)
        self.assertTrue(sale_item.date_start)
        self.assertTrue(sale_item.date_end)

    def test_extra_field_mapping(self):
        self.env['kits.woo.product.field.mapping'].create({
            'instance_id': self.instance.id,
            'woo_source': 'field',
            'woo_field': 'slug',
            'odoo_model': 'product.template',
            'odoo_field': 'barcode',
        })
        api = MagicMock()
        api.get_products.side_effect = lambda **_kw: iter([
            self._simple_product(701, 'MAP-701', slug='mapped-slug-value'),
        ])
        with self._patch_api(api):
            self.instance._woo_import_products()
        template = self.env['kits.woo.product.binding']._find(
            self.instance, external_id='701',
        ).product_tmpl_id
        self.assertEqual(template.barcode, 'mapped-slug-value')

    def test_webhook_eligibility_ignores_unknown_draft(self):
        payload = {
            'topic': 'product.created',
            'woo_id': 801,
            'product': self._simple_product(801, 'DRAFT-801', status='draft'),
        }

        class _Job:
            pass

        job = _Job()
        job.payload = payload
        api = MagicMock()
        with self._patch_api(api):
            summary = self.instance._woo_process_product_webhook(job=job)
        self.assertIn('Ignored', summary)
        self.assertEqual(
            self.env['kits.woo.product.waiting'].search_count([
                ('instance_id', '=', self.instance.id),
                ('external_id', '=', '801'),
            ]),
            0,
        )

    def test_webhook_queues_published_product(self):
        payload = {
            'topic': 'product.updated',
            'woo_id': 802,
            'product': self._simple_product(802, 'WH-802'),
            'delivery_id': 'd-802',
        }

        class _Job:
            pass

        job = _Job()
        job.payload = payload
        api = MagicMock()
        with self._patch_api(api):
            summary = self.instance._woo_process_product_webhook(job=job)
        self.assertIn('queued', summary)
        row = self.env['kits.woo.product.waiting'].search([
            ('instance_id', '=', self.instance.id),
            ('external_id', '=', '802'),
        ], limit=1)
        self.assertEqual(row.state, 'draft')
        self.assertEqual(row.source, 'webhook')

    def test_daily_checkpoint_advances_only_on_success(self):
        self.instance.woo_last_product_import = False
        api = MagicMock()
        api.get_products.side_effect = lambda **_kw: iter([self._simple_product(901, 'AUTO-901')])
        with self._patch_api(api):
            self.instance._woo_auto_import_products()
        self.assertTrue(self.instance.woo_last_product_import)

    def test_order_imports_unknown_product(self):
        remote = self._simple_product(1001, 'ORD-1001')
        api = MagicMock()
        api.get_product.return_value = remote
        api.get_variations.side_effect = lambda *_a, **_kw: iter([])
        with self._patch_api(api):
            product = self.instance._woo_match_order_product({
                'product_id': 1001,
                'variation_id': 0,
                'sku': 'ORD-1001',
                'name': 'Order line',
            })
        self.assertTrue(product)
        self.assertEqual(product.default_code, 'ORD-1001')
        self.assertTrue(
            self.env['kits.woo.product.binding']._find(self.instance, external_id='1001')
        )

    def test_image_deferred_then_processed(self):
        self.instance.woo_import_images = True
        Waiting = self.env['kits.woo.product.waiting']
        payload = self._simple_product(
            1101, 'IMG-1101',
            images=[{'id': 9, 'src': 'https://example.test/a.png', 'name': 'a'}],
        )
        row = Waiting._enqueue(
            self.instance, external_id=1101, payload=payload, trigger_batch=False,
        )
        # Avoid real download during product apply
        original = type(self.instance)._woo_sync_template_images

        def _noop_images(instance, template, images, binding=None):
            return True

        type(self.instance)._woo_sync_template_images = _noop_images
        try:
            Waiting._process_pending_for_instance(self.instance, limit=5)
            row.invalidate_recordset()
            self.assertEqual(row.state, 'done')
            self.assertEqual(row.image_state, 'pending')
            Waiting._process_images_for_instance(self.instance, limit=5)
            row.invalidate_recordset()
            self.assertEqual(row.image_state, 'done')
        finally:
            type(self.instance)._woo_sync_template_images = original

    def test_customer_waiting_untouched_regression(self):
        """Ensure customer waiting model and constants remain available."""
        CustomerWaiting = self.env['kits.woo.customer.waiting']
        self.assertTrue(CustomerWaiting._name)
        from odoo.addons.kits_odoo_woocommerce_connector.models.kits_woo_customer_waiting import (
            WAITING_BATCH_TRIGGER,
            WAITING_MAX_ATTEMPTS,
        )
        self.assertEqual(WAITING_BATCH_TRIGGER, 50)
        self.assertEqual(WAITING_MAX_ATTEMPTS, 3)
