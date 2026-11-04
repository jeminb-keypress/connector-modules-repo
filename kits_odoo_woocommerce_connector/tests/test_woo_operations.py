# -*- coding: utf-8 -*-
import base64
import io
from unittest.mock import MagicMock

from openpyxl import Workbook

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.kits_odoo_base_connector.services.exceptions import ConnectorAPIError

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooOperations(WooConnectorCommon):

    def _simple_remote(self, woo_id, sku, modified):
        return {
            'id': woo_id,
            'name': f'Product {sku}',
            'type': 'simple',
            'status': 'publish',
            'sku': sku,
            'regular_price': '12.00',
            'virtual': False,
            'tax_status': 'taxable',
            'date_modified_gmt': modified,
            'date_created_gmt': modified,
            'categories': [],
            'tags': [],
            'images': [],
        }

    def test_product_import_modes(self):
        old = '2026-01-01 00:00:00'
        new = '2026-02-01 00:00:00'
        template = self.env['product.template'].create({
            'name': 'Already Linked',
            'default_code': 'MAP-OLD',
            'type': 'consu',
            'list_price': 1,
            'company_id': self.company.id,
        })
        binding = self.instance._woo_bind('product.template', template.id, 41)
        binding.store_write_date = fields.Datetime.to_datetime('2026-01-15 00:00:00')
        api = MagicMock()
        api.get_products.return_value = iter([
            self._simple_remote(41, 'MAP-OLD', old),
            self._simple_remote(42, 'MAP-NEW', new),
        ])
        api.get_variations.return_value = iter([])
        job = self.env['kits.connector.sync.job'].create({
            'name': 'new and updated',
            'instance_id': self.instance.id,
            'job_type': 'woo_import_products',
            'payload': {'product_mode': 'new_and_updated'},
            'state': 'pending',
        })
        with self._patch_api(api):
            summary = self.instance._woo_import_products(job=job)
        self.assertIn('skipped=1', summary)
        self.assertTrue(self.env['kits.woo.product.binding']._find(self.instance, external_id='42'))

        api.get_products.return_value = iter([
            self._simple_remote(41, 'MAP-OLD', new),
            self._simple_remote(43, 'MAP-SKIP', new),
        ])
        skip_job = self.env['kits.connector.sync.job'].create({
            'name': 'skip existing',
            'instance_id': self.instance.id,
            'job_type': 'woo_import_products',
            'payload': {'skip_existing': True},
            'state': 'pending',
        })
        with self._patch_api(api):
            summary = self.instance._woo_import_products(job=skip_job)
        self.assertIn('skipped=1', summary)
        self.assertTrue(self.env['kits.woo.product.binding']._find(self.instance, external_id='43'))
        self.assertEqual(template.name, 'Already Linked')

    def test_order_scopes_and_cancel_guards(self):
        variant = self.env['product.product'].create({
            'name': 'Order Item',
            'default_code': 'OP-1',
            'type': 'consu',
            'is_storable': False,
            'list_price': 10,
            'company_id': self.company.id,
        })
        self.instance._woo_bind('product.template', variant.product_tmpl_id.id, 70)
        self.instance._woo_bind('product.product', variant.id, 70, external_data={'sku': 'OP-1'})
        partner = self.env['res.partner'].create({
            'name': 'Buyer',
            'company_id': self.company.id,
        })
        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'company_id': self.company.id,
            'woo_instance_id': self.instance.id,
            'woo_order_id': '900',
            'woo_order_status': 'processing',
            'order_line': [(0, 0, {
                'product_id': variant.id,
                'product_uom_qty': 1,
                'price_unit': 10,
            })],
        })
        self.instance._woo_bind('sale.order', order.id, 900)
        order.action_confirm()
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.out_type_id.id,
            'location_id': self.warehouse.lot_stock_id.id,
            'location_dest_id': self.env.ref('stock.stock_location_customers').id,
            'sale_id': order.id,
            'company_id': self.company.id,
        })
        picking.state = 'done'
        api = MagicMock()
        api.get_orders.return_value = iter([{
            'id': 900,
            'number': '900',
            'status': 'cancelled',
            'date_created_gmt': '2026-03-01T00:00:00',
            'line_items': [],
        }])
        job = self.env['kits.connector.sync.job'].create({
            'name': 'cancel',
            'instance_id': self.instance.id,
            'job_type': 'woo_import_orders',
            'payload': {'order_scope': 'cancelled'},
            'state': 'pending',
        })
        with self._patch_api(api):
            summary = self.instance._woo_import_orders(job=job)
        self.assertEqual(order.state, 'sale')
        self.assertIn('delivery is already done', order.woo_sync_conflict or '')
        self.assertIn('skipped=', summary)
        self.assertEqual(api.get_orders.call_args.kwargs['status'], 'cancelled')

    def test_order_date_filter_and_specific_update(self):
        api = MagicMock()
        api.get_orders.return_value = iter([
            {
                'id': 1, 'status': 'processing', 'date_created_gmt': '2026-01-01T00:00:00',
                'line_items': [], 'customer_id': 0, 'number': '1', 'total': '0',
                'currency': 'USD', 'prices_include_tax': False,
            },
            {
                'id': 2, 'status': 'completed', 'date_created_gmt': '2026-04-01T00:00:00',
                'line_items': [], 'customer_id': 0, 'number': '2', 'total': '0',
                'currency': 'USD', 'prices_include_tax': False,
            },
        ])
        job = self.env['kits.connector.sync.job'].create({
            'name': 'shipped window',
            'instance_id': self.instance.id,
            'job_type': 'woo_import_orders',
            'payload': {
                'order_scope': 'shipped',
                'date_from': '2026-03-01 00:00:00',
                'date_to': '2026-05-01 00:00:00',
            },
            'state': 'pending',
        })
        with self._patch_api(api):
            self.instance._woo_import_orders(job=job)
        self.assertEqual(api.get_orders.call_args.kwargs['status'], 'completed')
        self.assertFalse(self.instance._woo_find_odoo('sale.order', 1))

    def test_product_mapping_csv(self):
        template = self.env['product.template'].create({
            'name': 'Mapped',
            'default_code': 'CSV-1',
            'type': 'consu',
            'list_price': 5,
            'company_id': self.company.id,
        })
        other = self.env['product.template'].create({
            'name': 'Taken',
            'default_code': 'CSV-2',
            'type': 'consu',
            'list_price': 5,
            'company_id': self.company.id,
        })
        self.instance._woo_bind('product.template', other.id, 77)
        content = base64.b64encode(
            b'odoo_id,sku,woo_product_id\n'
            b',CSV-1,88\n'
            b',MISSING,89\n'
            b',CSV-2,78\n'
        )
        action = self.instance._woo_map_products(content, 'map.csv')
        message = action['params']['message']
        self.assertIn('linked 1', message.lower())
        self.assertIn('failed', message.lower())
        linked = self.env['kits.woo.product.binding']._find(self.instance, external_id='88')
        self.assertEqual(linked.product_tmpl_id, template)
        self.assertEqual(
            self.env['kits.woo.product.binding']._find(self.instance, external_id='77').product_tmpl_id,
            other,
        )
        self.assertFalse(self.env['product.template'].search([('name', '=', 'MISSING')]))

    def test_product_mapping_xlsx(self):
        template = self.env['product.template'].create({
            'name': 'Sheet Product',
            'default_code': 'XLS-1',
            'type': 'consu',
            'list_price': 5,
            'company_id': self.company.id,
        })
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['sku', 'woo_id'])
        sheet.append(['XLS-1', 91])
        buffer = io.BytesIO()
        workbook.save(buffer)
        content = base64.b64encode(buffer.getvalue())
        self.instance._woo_map_products(content, 'map.xlsx')
        linked = self.env['kits.woo.product.binding']._find(self.instance, external_id='91')
        self.assertEqual(linked.product_tmpl_id, template)

    def test_category_tag_coupon_modes(self):
        category = self.env['product.category'].create({'name': 'Ops Category'})
        tag = self.env['product.tag'].create({'name': 'Ops Tag'})
        coupon = self.env['kits.woo.coupon'].create({
            'name': 'Ops Coupon',
            'code': 'OPS-10',
            'instance_id': self.instance.id,
            'discount_type': 'percent',
            'amount': 10,
            'sync_to_woo': True,
        })
        api = MagicMock()
        api.create_category.return_value = {'id': 5, 'name': category.name}
        api.update_category.return_value = {'id': 5, 'name': category.name}
        api.get_tags.return_value = iter([])
        api.create_tag.return_value = {'id': 6, 'name': tag.name}
        api.update_tag.return_value = {'id': 6, 'name': tag.name}
        api.create_coupon.return_value = {'id': 7, 'code': coupon.code}
        api.update_coupon.return_value = {'id': 7, 'code': coupon.code}
        with self._patch_api(api):
            self.instance._woo_export_categories(job=self._job('woo_export_categories', {
                'category_mode': 'export', 'category_ids': [category.id],
            }))
            self.instance._woo_export_categories(job=self._job('woo_export_categories', {
                'category_mode': 'update', 'category_ids': [category.id],
            }))
            self.instance._woo_export_tags(job=self._job('woo_export_tags', {
                'tag_mode': 'export', 'tag_ids': [tag.id],
            }))
            self.instance._woo_export_tags(job=self._job('woo_export_tags', {
                'tag_mode': 'update', 'tag_ids': [tag.id],
            }))
            self.instance._woo_export_coupons(job=self._job('woo_export_coupons', {'coupon_mode': 'export'}))
            self.instance._woo_export_coupons(job=self._job('woo_export_coupons', {'coupon_mode': 'update'}))
        api.create_category.assert_called()
        api.update_category.assert_called()
        api.create_tag.assert_called()
        api.update_tag.assert_called()
        api.create_coupon.assert_called_once()
        api.update_coupon.assert_called()

    def test_shipment_stock_publish_cancel_refund_and_duplicate_job(self):
        template = self.env['product.template'].create({
            'name': 'Published',
            'type': 'consu',
            'is_storable': True,
            'list_price': 3,
            'company_id': self.company.id,
            'woo_instance_ids': [(4, self.instance.id)],
        })
        variant = template.product_variant_id
        self.instance._woo_bind('product.template', template.id, 15)
        self.instance._woo_bind('product.product', variant.id, 15)
        api = MagicMock()
        api.update_product.return_value = {'id': 15, 'status': 'draft'}
        with self._patch_api(api):
            template.action_woo_unpublish()
        self.assertTrue(template.active)
        self.assertFalse(self.env['kits.woo.product.binding']._find(
            self.instance, external_id='15',
        ).published)

        partner = self.env['res.partner'].create({'name': 'Ship', 'company_id': self.company.id})
        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'company_id': self.company.id,
            'woo_instance_id': self.instance.id,
            'woo_order_id': '501',
            'woo_order_status': 'processing',
            'order_line': [(0, 0, {
                'product_id': variant.id,
                'product_uom_qty': 1,
                'price_unit': 3,
            })],
        })
        order.action_confirm()
        for picking in order.picking_ids:
            picking.state = 'done'
        api.update_order.return_value = {'id': 501, 'status': 'completed'}
        with self._patch_api(api):
            summary = self.instance._woo_export_shipments()
        self.assertIn('updated=1', summary)
        self.assertEqual(order.woo_order_status, 'completed')
        api.create_order_note.assert_not_called()

        api.update_order.return_value = {'id': 501, 'status': 'cancelled'}
        order.woo_order_status = 'processing'
        with self._patch_api(api):
            order.action_woo_cancel_order()
        self.assertEqual(order.state, 'sale')
        self.assertEqual(order.woo_order_status, 'cancelled')

        api.update_product.reset_mock()
        future = '2099-01-01 00:00:00'
        with self._patch_api(api):
            summary = self.instance._woo_export_inventory(job=self._job(
                'woo_export_inventory', {'stock_from': future},
            ))
        self.assertIn('updated=0', summary)
        api.update_product.assert_not_called()

        journal = self.env['account.journal'].search([
            ('type', '=', 'sale'), ('company_id', '=', self.company.id),
        ], limit=1)
        credit = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': partner.id,
            'journal_id': journal.id,
            'invoice_origin': order.name,
            'invoice_line_ids': [(0, 0, {
                'name': 'Refund',
                'quantity': 1,
                'price_unit': 3,
            })],
        })
        api.create_order_refund.return_value = {'id': 9, 'amount': '3.00'}
        with self._patch_api(api):
            credit.action_woo_export_refund()
        refund = self.env['kits.woo.refund'].search([
            ('credit_note_id', '=', credit.id), ('woo_refund_id', '=', '9'),
        ])
        self.assertEqual(refund.state, 'done')
        with self.assertRaises(UserError):
            with self._patch_api(api):
                credit.action_woo_export_refund()
        self.assertEqual(api.create_order_refund.call_count, 1)

        wizard = self.env['kits.woo.sync.wizard'].create({
            'instance_id': self.instance.id,
            'operation': 'import_customers',
        })
        first = wizard.action_execute()
        second = wizard.action_execute()
        jobs = self.env['kits.connector.sync.job'].search([
            ('instance_id', '=', self.instance.id),
            ('job_type', '=', 'woo_import_customers'),
            ('state', 'in', ('pending', 'running')),
        ])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(first['type'], 'ir.actions.act_window')
        self.assertEqual(second['domain'], [('id', 'in', jobs.ids)])

    def test_api_failure_is_logged_for_remote_cancel(self):
        partner = self.env['res.partner'].create({'name': 'Fail', 'company_id': self.company.id})
        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'company_id': self.company.id,
            'woo_instance_id': self.instance.id,
            'woo_order_id': '777',
            'woo_order_status': 'processing',
        })
        api = MagicMock()
        api.update_order.side_effect = ConnectorAPIError('cannot cancel', status_code=400)
        caught = None
        with self._patch_api(api):
            try:
                order.action_woo_cancel_order()
            except UserError as exc:
                caught = exc
        self.assertTrue(caught)
        self.assertEqual(order.state, 'draft')
        log = self.env['kits.connector.log'].sudo().search([
            ('instance_id', '=', self.instance.id),
            ('name', '=', 'woo_cancel_order'),
            ('status', '=', 'failure'),
        ], limit=1)
        self.assertTrue(log)

    def test_wizard_operation_payloads(self):
        wizard = self.env['kits.woo.sync.wizard'].create({
            'instance_id': self.instance.id,
            'operation': 'import_products',
            'product_import_mode': 'new_and_updated',
            'skip_existing': True,
        })
        job_type, _name, payload = wizard._prepare_job()
        self.assertEqual(job_type, 'woo_import_products')
        self.assertEqual(payload['product_mode'], 'new_and_updated')
        self.assertTrue(payload['skip_existing'])
        wizard.operation = 'import_unshipped_orders'
        wizard.date_from = fields.Datetime.to_datetime('2026-01-01 00:00:00')
        wizard.date_to = fields.Datetime.to_datetime('2026-01-31 00:00:00')
        job_type, _name, payload = wizard._prepare_job()
        self.assertEqual(payload['order_scope'], 'unshipped')
        self.assertIn('date_from', payload)
        wizard.operation = 'export_stock'
        wizard.stock_from = fields.Datetime.to_datetime('2026-06-01 00:00:00')
        job_type, _name, payload = wizard._prepare_job()
        self.assertEqual(job_type, 'woo_export_inventory')
        self.assertFalse(payload['full'])
        with self.assertRaises(ValidationError):
            self.env['kits.woo.sync.wizard'].create({
                'instance_id': self.instance.id,
                'operation': 'map_products',
            })

    def _job(self, job_type, payload):
        return self.env['kits.connector.sync.job'].create({
            'name': job_type,
            'instance_id': self.instance.id,
            'job_type': job_type,
            'payload': payload,
            'state': 'pending',
        })
