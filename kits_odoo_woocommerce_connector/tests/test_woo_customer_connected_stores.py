# -*- coding: utf-8 -*-
from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooCustomerConnectedStores(WooConnectorCommon):
    """Connected Stores Many2many on res.partner derived from customer bindings."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_instance = cls.env['kits.woo.instance'].create({
            'name': 'Second Woo Store',
            'platform_type': 'woocommerce',
            'company_id': cls.company.id,
            'base_url': 'https://woo-b.test',
            'woo_consumer_key': 'ck_b',
            'woo_consumer_secret': 'cs_b',
            'warehouse_id': cls.warehouse.id if cls.warehouse else False,
            'state': 'connected',
        })

    def _partner(self, name, email):
        return self.env['res.partner'].create({
            'name': name,
            'email': email,
            'company_id': self.company.id,
        })

    def test_no_binding_has_empty_connected_stores(self):
        partner = self._partner('No Store', 'nostore@example.com')
        self.assertFalse(partner.woo_instance_ids)

    def test_one_store_binding(self):
        partner = self._partner('One Store', 'one@example.com')
        self.instance._woo_bind('res.partner', partner.id, '101')
        partner.invalidate_recordset(['woo_instance_ids'])
        self.assertEqual(partner.woo_instance_ids, self.instance)

    def test_multiple_store_bindings(self):
        partner = self._partner('Multi Store', 'multi@example.com')
        self.instance._woo_bind('res.partner', partner.id, '201')
        self.other_instance._woo_bind('res.partner', partner.id, '301')
        partner.invalidate_recordset(['woo_instance_ids'])
        self.assertEqual(partner.woo_instance_ids, self.instance | self.other_instance)

    def test_two_customers_same_store(self):
        first = self._partner('First', 'first@example.com')
        second = self._partner('Second', 'second@example.com')
        self.instance._woo_bind('res.partner', first.id, '401')
        self.instance._woo_bind('res.partner', second.id, '402')
        first.invalidate_recordset(['woo_instance_ids'])
        second.invalidate_recordset(['woo_instance_ids'])
        self.assertEqual(first.woo_instance_ids, self.instance)
        self.assertEqual(second.woo_instance_ids, self.instance)

    def test_create_binding_updates_connected_stores(self):
        partner = self._partner('Create Bind', 'create@example.com')
        self.assertFalse(partner.woo_instance_ids)
        self.env['kits.woo.customer.binding'].create({
            'instance_id': self.instance.id,
            'partner_id': partner.id,
            'external_id': '501',
        })
        self.assertEqual(partner.woo_instance_ids, self.instance)

    def test_remove_binding_updates_connected_stores(self):
        partner = self._partner('Remove Bind', 'remove@example.com')
        binding = self.instance._woo_bind('res.partner', partner.id, '601')
        self.assertEqual(partner.woo_instance_ids, self.instance)
        binding.unlink()
        partner.invalidate_recordset(['woo_instance_ids'])
        self.assertFalse(partner.woo_instance_ids)

    def test_soft_delete_clears_connected_store(self):
        partner = self._partner('Soft Delete', 'softdel@example.com')
        binding = self.instance._woo_bind('res.partner', partner.id, '701')
        self.assertEqual(partner.woo_instance_ids, self.instance)
        binding._kits_mark_deleted_remotely()
        partner.invalidate_recordset(['woo_instance_ids'])
        self.assertFalse(partner.woo_instance_ids)

    def test_reassign_binding_updates_both_partners(self):
        first = self._partner('Old Owner', 'old@example.com')
        second = self._partner('New Owner', 'new@example.com')
        binding = self.instance._woo_bind('res.partner', first.id, '801')
        self.assertEqual(first.woo_instance_ids, self.instance)
        self.assertFalse(second.woo_instance_ids)
        # Direct reassignment is rare but must keep Connected Stores consistent.
        binding.write({'partner_id': second.id})
        first.invalidate_recordset(['woo_instance_ids'])
        second.invalidate_recordset(['woo_instance_ids'])
        self.assertFalse(first.woo_instance_ids)
        self.assertEqual(second.woo_instance_ids, self.instance)

    def test_no_duplicate_store_relationships(self):
        partner = self._partner('Dup Store', 'dup@example.com')
        self.instance._woo_bind('res.partner', partner.id, '901')
        self.instance._woo_bind('res.partner', partner.id, '901')
        partner.invalidate_recordset(['woo_instance_ids'])
        self.assertEqual(len(partner.woo_instance_ids), 1)
        self.assertEqual(partner.woo_instance_ids, self.instance)
        self.env.cr.execute(
            """
            SELECT COUNT(*) FROM res_partner_woo_instance_rel
             WHERE partner_id = %s AND instance_id = %s
            """,
            (partner.id, self.instance.id),
        )
        self.assertEqual(self.env.cr.fetchone()[0], 1)

    def test_filter_has_woocommerce_store(self):
        linked = self._partner('Has Store', 'has@example.com')
        orphan = self._partner('No Store Filter', 'nofilter@example.com')
        self.instance._woo_bind('res.partner', linked.id, '1001')
        found = self.env['res.partner'].search([
            ('id', 'in', [linked.id, orphan.id]),
            ('woo_instance_ids', '!=', False),
        ])
        self.assertEqual(found, linked)

    def test_filter_no_woocommerce_store(self):
        linked = self._partner('Has Store 2', 'has2@example.com')
        orphan = self._partner('No Store Filter 2', 'nofilter2@example.com')
        self.instance._woo_bind('res.partner', linked.id, '1002')
        found = self.env['res.partner'].search([
            ('id', 'in', [linked.id, orphan.id]),
            ('woo_instance_ids', '=', False),
        ])
        self.assertEqual(found, orphan)

    def test_group_by_connected_store(self):
        first = self._partner('Group A', 'groupa@example.com')
        second = self._partner('Group B', 'groupb@example.com')
        shared = self._partner('Group Shared', 'groupshared@example.com')
        self.instance._woo_bind('res.partner', first.id, '1101')
        self.other_instance._woo_bind('res.partner', second.id, '1102')
        self.instance._woo_bind('res.partner', shared.id, '1103')
        self.other_instance._woo_bind('res.partner', shared.id, '1104')
        groups = self.env['res.partner'].read_group(
            [('id', 'in', [first.id, second.id, shared.id])],
            groupby=['woo_instance_ids'],
            aggregates=['__count'],
        )
        by_store = {}
        for store, count in groups:
            store_id = store.id if hasattr(store, 'id') else store
            if store_id:
                by_store[store_id] = count
        self.assertEqual(by_store[self.instance.id], 2)
        self.assertEqual(by_store[self.other_instance.id], 2)

    def test_customer_menu_search_view_fields(self):
        action = self.env.ref(
            'kits_odoo_woocommerce_connector.kits_woo_customer_action'
        )
        search = self.env.ref(
            'kits_odoo_woocommerce_connector.kits_woo_customer_view_search'
        )
        self.assertEqual(action.search_view_id, search)
        arch = search.arch_db if isinstance(search.arch_db, str) else search.get_combined_arch()
        self.assertIn('filter_has_woo_store', arch)
        self.assertIn('filter_no_woo_store', arch)
        self.assertIn('woo_instance_ids', arch)
        self.assertIn('group_woo_connected_store', arch)
        self.assertNotIn('kits_connected_instance_names', arch)
