# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch

from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooCatalogMenusAndModels(WooConnectorCommon):

    def test_seven_product_menu_actions_open_connector_models(self):
        Action = self.env['ir.actions.act_window']
        expected = {
            'kits_odoo_woocommerce_connector.kits_woo_product_catalog_action': 'kits.woo.product.binding',
            'kits_odoo_woocommerce_connector.kits_woo_variant_catalog_action': 'kits.woo.variant.binding',
            'kits_odoo_woocommerce_connector.kits_woo_category_action': 'kits.woo.category',
            'kits_odoo_woocommerce_connector.kits_woo_attribute_action': 'kits.woo.attribute',
            'kits_odoo_woocommerce_connector.kits_woo_attribute_term_action': 'kits.woo.attribute.term',
            'kits_odoo_woocommerce_connector.kits_woo_tag_action': 'kits.woo.tag',
            'kits_odoo_woocommerce_connector.kits_woo_coupon_action': 'kits.woo.coupon',
        }
        for xmlid, model in expected.items():
            action = self.env.ref(xmlid)
            self.assertEqual(action.res_model, model, xmlid)

    def test_category_tag_attribute_connector_records(self):
        api = MagicMock()
        with self._patch_api(api):
            api.get_categories.side_effect = None
            api.get_tags.side_effect = None
            api.get_attributes.side_effect = None
            api.get_attribute_terms.side_effect = None
            api.get_categories.return_value = iter([
                {'id': 1, 'name': 'Parent', 'parent': 0, 'slug': 'parent', 'display': 'default'},
                {
                    'id': 2, 'name': 'Child', 'parent': 1, 'slug': 'child',
                    'display': 'products', 'description': '<p>Child</p>',
                    'image': {'src': 'https://example.test/c.jpg'},
                },
            ])
            api.get_tags.return_value = iter([
                {'id': 9, 'name': 'Summer', 'slug': 'summer', 'description': 'Hot'},
            ])
            api.get_attributes.return_value = iter([
                {'id': 11, 'name': 'Color', 'slug': 'color', 'type': 'select', 'order_by': 'name'},
            ])
            api.get_attribute_terms.return_value = iter([
                {'id': 21, 'name': 'Red', 'slug': 'red'},
            ])
            self.instance._woo_import_categories()
            self.instance._woo_import_tags()
            self.instance._woo_import_attributes()

        parent = self.env['kits.woo.category']._find(self.instance, external_id='1')
        child = self.env['kits.woo.category']._find(self.instance, external_id='2')
        self.assertTrue(parent)
        self.assertEqual(child.parent_id, parent)
        self.assertEqual(child.display_type, 'products')
        self.assertEqual(child.export_status, 'exported')
        self.assertFalse(child.odoo_category_id)

        tag = self.env['kits.woo.tag']._find(self.instance, external_id='9')
        self.assertEqual(tag.name, 'Summer')
        self.assertEqual(tag.export_status, 'exported')
        self.assertFalse(tag.odoo_tag_id)

        attr = self.env['kits.woo.attribute']._find(self.instance, external_id='11')
        self.assertEqual(attr.name, 'Color')
        self.assertTrue(attr.odoo_attribute_id)
        term = self.env['kits.woo.attribute.term']._find(
            self.instance, attribute=attr, external_id='21',
        )
        self.assertEqual(term.name, 'Red')
        self.assertEqual(term.odoo_attribute_value_id.attribute_id, attr.odoo_attribute_id)

    def test_coupon_waiting_list_and_order_discount_codes(self):
        woo = {
            'id': 77,
            'code': 'SAVE20',
            'discount_type': 'percent',
            'amount': '20',
            'exclude_sale_items': True,
            'usage_count': 3,
            'description': 'Save twenty',
        }
        api = MagicMock()
        api.get_coupons.side_effect = lambda **kw: iter([dict(woo)])
        with self._patch_api(api):
            summary = self.instance._woo_import_coupons()
        self.assertIn('queued=1', summary)
        coupon = self.env['kits.woo.coupon'].search([
            ('instance_id', '=', self.instance.id),
            ('code', '=', 'SAVE20'),
        ])
        self.assertEqual(len(coupon), 1)
        self.assertTrue(coupon.exclude_sale_items)
        self.assertEqual(coupon.usage_count, 3)
        waiting = self.env['kits.woo.coupon.waiting'].search([
            ('instance_id', '=', self.instance.id),
            ('external_id', '=', '77'),
            ('state', '=', 'done'),
        ])
        self.assertTrue(waiting)

        line = self.instance._woo_prepare_discount_line_vals(
            5.0,
            coupon_lines=[{'code': 'SAVE20'}, {'code': 'EXTRA'}],
        )
        self.assertIn('SAVE20', line['name'])
        self.assertIn('EXTRA', line['name'])
