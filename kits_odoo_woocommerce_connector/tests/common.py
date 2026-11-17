# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install', 'kits_woo')
class WooConnectorCommon(TransactionCase):
    """Shared helpers for WooCommerce connector tests (mocked HTTP)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1,
        )
        cls.instance = cls.env['kits.woo.instance'].create({
            'name': 'Test Woo Store',
            'platform_type': 'woocommerce',
            'company_id': cls.company.id,
            'base_url': 'https://woo.test',
            'woo_consumer_key': 'ck_test',
            'woo_consumer_secret': 'cs_test',
            'warehouse_id': cls.warehouse.id if cls.warehouse else False,
            'woo_import_images': False,
            'woo_match_by_sku': True,
            'woo_create_products': True,
            'woo_allow_unmapped_order_lines': False,
            'state': 'connected',
        })

    def _patch_api(self, api):
        self._stub_catalog_api(api)
        return patch.object(type(self.instance), '_woo_api', return_value=api)

    @staticmethod
    def _stub_catalog_api(api):
        """Ensure product-import catalog prerequisites iterate empty lists."""
        if not getattr(api, '_kits_catalog_stubbed', False):
            api.get_categories.side_effect = lambda **_kw: iter([])
            api.get_tags.side_effect = lambda **_kw: iter([])
            api.get_attributes.side_effect = lambda **_kw: iter([])
            api.get_attribute_terms.side_effect = lambda *_a, **_kw: iter([])
            api._kits_catalog_stubbed = True
        return api

    @staticmethod
    def _iter_api(records):
        api = MagicMock()
        api.get_products.return_value = iter(records)
        api.get_customers.return_value = iter(records)
        api.get_orders.return_value = iter(records)
        api.get_variations.return_value = iter([])
        api.get_categories.side_effect = lambda **_kw: iter([])
        api.get_tags.side_effect = lambda **_kw: iter([])
        api.get_attributes.side_effect = lambda **_kw: iter([])
        api.get_attribute_terms.side_effect = lambda *_a, **_kw: iter([])
        return api
