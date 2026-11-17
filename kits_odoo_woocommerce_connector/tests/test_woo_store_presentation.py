# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch

from odoo.fields import Command
from odoo.tests import tagged

from .common import WooConnectorCommon


@tagged('post_install', '-at_install', 'kits_woo')
class TestWooStorePresentation(WooConnectorCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_instance = cls.env['kits.woo.instance'].create({
            'name': 'Other Woo Store',
            'platform_type': 'woocommerce',
            'company_id': cls.company.id,
            'base_url': 'https://woo-other.test',
            'woo_consumer_key': 'ck_other',
            'woo_consumer_secret': 'cs_other',
            'warehouse_id': cls.warehouse.id if cls.warehouse else False,
            'woo_import_images': False,
            'woo_match_by_sku': True,
            'woo_create_products': True,
            'state': 'connected',
        })
        en = cls.env.ref('base.lang_en')
        cls.instance.lang_id = en
        cls.other_instance.lang_id = en

    def _make_template(self, name='Blue Cotton Shirt', **extra):
        vals = {
            'name': name,
            'default_code': extra.pop('default_code', 'BLUE-SHIRT'),
            'type': 'consu',
            'is_storable': True,
            'list_price': 25.0,
            'sale_ok': True,
            'description': '<p>Native long</p>',
            'description_sale': 'Native short',
            'company_id': self.company.id,
        }
        vals.update(extra)
        return self.env['product.template'].create(vals)

    def test_prepare_copies_native_name_and_isolates_stores(self):
        template = self._make_template()
        binding_a = self.instance._woo_prepare_product_binding_for_store(template)
        binding_b = self.other_instance._woo_prepare_product_binding_for_store(template)

        self.assertEqual(binding_a.title, 'Blue Cotton Shirt')
        self.assertEqual(binding_b.title, 'Blue Cotton Shirt')
        self.assertFalse(binding_a.external_id)
        self.assertIn(binding_a, self.env['kits.woo.product.binding'].search([
            ('title', 'ilike', 'Blue Cotton'),
        ]))

        binding_a.title = 'Blue Shirt'
        self.assertEqual(template.name, 'Blue Cotton Shirt')
        self.assertEqual(binding_b.title, 'Blue Cotton Shirt')

        # Re-prepare must not overwrite customized store name.
        self.instance._woo_prepare_product_binding_for_store(template)
        self.assertEqual(binding_a.title, 'Blue Shirt')

    def test_empty_store_title_falls_back_to_native(self):
        template = self._make_template()
        binding = self.instance._woo_prepare_product_binding_for_store(template)
        binding.with_context(kits_woo_skip_presentation_dirty=True).write({'title': False})
        self.assertEqual(
            self.instance._woo_store_product_title(binding, template),
            'Blue Cotton Shirt',
        )
        payload = self.instance._woo_build_product_export_payload(template, binding=binding)
        self.assertEqual(payload['name'], 'Blue Cotton Shirt')
        self.assertTrue(payload['name'].strip())

    def test_import_updates_store_note_preserves_native_and_other_store(self):
        template = self._make_template(default_code='SKU-PRES-1')
        binding_a = self.instance._woo_prepare_product_binding_for_store(template)
        binding_a.write({
            'external_id': '501',
            'exists_in_store': True,
            'title': 'Old Store A',
            'sync_state': 'synced',
        })
        binding_b = self.other_instance._woo_prepare_product_binding_for_store(template)
        binding_b.write({
            'external_id': '601',
            'exists_in_store': True,
            'title': 'Store B Name',
            'sync_state': 'synced',
        })

        woo_product = {
            'id': 501,
            'name': 'Remote Store A Name',
            'type': 'simple',
            'status': 'publish',
            'sku': 'SKU-PRES-1',
            'description': '<p>Remote long</p>',
            'short_description': 'Remote short',
            'regular_price': '30.00',
            'categories': [],
            'tags': [],
            'images': [],
        }
        api = MagicMock()
        api.get_products.return_value = iter([woo_product])
        with self._patch_api(api):
            self.instance._woo_import_products()

        self.assertEqual(template.name, 'Blue Cotton Shirt')
        self.assertEqual(binding_a.title, 'Remote Store A Name')
        self.assertEqual(binding_b.title, 'Store B Name')
        self.assertIn('Remote long', binding_a.description or '')

    def test_import_new_product_sets_native_and_store_title(self):
        woo_product = {
            'id': 701,
            'name': 'Brand New Woo Tee',
            'type': 'simple',
            'status': 'publish',
            'sku': 'NEW-TEE-701',
            'description': '<p>Desc</p>',
            'short_description': 'Short',
            'regular_price': '11.00',
            'categories': [],
            'tags': [],
            'images': [],
        }
        api = MagicMock()
        api.get_products.return_value = iter([woo_product])
        with self._patch_api(api):
            self.instance._woo_import_products()

        binding = self.env['kits.woo.product.binding']._find(self.instance, external_id='701')
        self.assertTrue(binding)
        self.assertEqual(binding.product_tmpl_id.name, 'Brand New Woo Tee')
        self.assertEqual(binding.title, 'Brand New Woo Tee')

    def test_import_warns_on_unexported_local_title(self):
        template = self._make_template(default_code='SKU-WARN-1')
        binding = self.instance._woo_prepare_product_binding_for_store(template)
        binding.write({
            'external_id': '801',
            'exists_in_store': True,
            'title': 'Local Unexported',
            'sync_state': 'not_synced',
        })
        woo_product = {
            'id': 801,
            'name': 'Incoming Remote',
            'type': 'simple',
            'status': 'publish',
            'sku': 'SKU-WARN-1',
            'categories': [],
            'tags': [],
            'images': [],
        }
        with patch.object(type(self.instance), '_kits_log_operation') as log_mock:
            api = MagicMock()
            api.get_products.return_value = iter([woo_product])
            with self._patch_api(api):
                self.instance._woo_import_products()
        warning_calls = [
            call for call in log_mock.call_args_list
            if call.kwargs.get('level') == 'warning'
            or (call.args and len(call.args) > 2)
        ]
        # At least one warning log about replacing unexported title.
        messages = []
        for call in log_mock.call_args_list:
            kwargs = call.kwargs
            if kwargs.get('level') == 'warning':
                messages.append(kwargs.get('message') or '')
        self.assertTrue(any('unexported store title' in (m or '') for m in messages), messages)
        self.assertEqual(binding.title, 'Incoming Remote')
        self.assertEqual(template.name, 'Blue Cotton Shirt')

    def test_full_export_sends_store_title(self):
        template = self._make_template(default_code='EXP-PRES-1')
        template.woo_instance_ids = self.instance
        binding = self.instance._woo_prepare_product_binding_for_store(template)
        binding.title = 'Store Export Name'

        api = MagicMock()
        api.create_product.return_value = {'id': 9001, 'images': []}
        with self._patch_api(api):
            self.instance._woo_export_products()

        payload = api.create_product.call_args.args[0]
        self.assertEqual(payload['name'], 'Store Export Name')
        self.assertEqual(template.name, 'Blue Cotton Shirt')

    def test_update_groups_basic_price_images_status(self):
        template = self._make_template(default_code='EXP-GROUP-1')
        binding = self.instance._woo_prepare_product_binding_for_store(template)
        binding.write({
            'external_id': '9100',
            'exists_in_store': True,
            'title': 'Grouped Title',
            'description': '<p>Store long</p>',
            'description_short': 'Store short',
            'sync_state': 'synced',
        })
        template.woo_instance_ids = self.instance

        basic = self.instance._woo_build_product_export_payload(
            template, binding=binding, update_groups=['basic_details'],
        )
        self.assertEqual(basic['name'], 'Grouped Title')
        self.assertIn('Store long', basic['description'] or '')
        self.assertIn('Store short', basic['short_description'] or '')
        self.assertNotIn('regular_price', basic)
        self.assertNotIn('images', basic)
        self.assertNotIn('status', basic)

        price = self.instance._woo_build_product_export_payload(
            template, binding=binding, update_groups=['price'],
        )
        self.assertNotIn('name', price)
        self.assertNotIn('description', price)
        self.assertIn('regular_price', price)

        images = self.instance._woo_build_product_export_payload(
            template, binding=binding, update_groups=['images'],
        )
        self.assertNotIn('name', images)
        self.assertNotIn('description', images)

        status = self.instance._woo_build_product_export_payload(
            template, binding=binding, update_groups=['status'],
        )
        self.assertNotIn('name', status)
        self.assertIn('status', status)

        api = MagicMock()
        api.update_product.return_value = {'id': 9100, 'images': []}
        with self._patch_api(api):
            self.instance._woo_export_one_template(
                template, api, update_groups=['price'],
            )
        sent = api.update_product.call_args.args[1]
        self.assertNotIn('name', sent)
        self.assertIn('regular_price', sent)

    def test_variant_title_store_only_not_in_api_payload(self):
        attribute = self.env['product.attribute'].create({'name': 'Color Pres'})
        red = self.env['product.attribute.value'].create({
            'name': 'Red', 'attribute_id': attribute.id,
        })
        blue = self.env['product.attribute.value'].create({
            'name': 'Blue', 'attribute_id': attribute.id,
        })
        template = self._make_template(
            name='Variable Pres',
            default_code='',
            attribute_line_ids=[Command.create({
                'attribute_id': attribute.id,
                'value_ids': [Command.set([red.id, blue.id])],
            })],
        )
        template.woo_instance_ids = self.instance
        self.assertEqual(len(template.product_variant_ids), 2)

        api = MagicMock()
        api.create_product.return_value = {'id': 9200, 'images': []}
        api.create_variation.side_effect = [
            {'id': 9201},
            {'id': 9202},
        ]
        with self._patch_api(api):
            self.instance._woo_export_products()

        for call in api.create_variation.call_args_list:
            variation_payload = call.args[1] if len(call.args) > 1 else call.kwargs.get('data')
            if variation_payload is None and call.args:
                # create_variation(parent_id, payload)
                variation_payload = call.args[-1]
            self.assertNotIn('name', variation_payload or {})

        variant = template.product_variant_ids[0]
        native_name = variant.display_name
        vbind = self.env['kits.woo.variant.binding']._find(
            self.instance, odoo_record_id=variant.id,
        )
        self.assertTrue(vbind)
        self.assertTrue(vbind.title)
        vbind.title = 'Custom Variant Label'
        self.assertEqual(variant.display_name, native_name)

        # Import updates store title, not native variant name.
        row = {
            'external_id': vbind.external_id,
            'sku': variant.default_code or 'V-SKU',
            'parent_id': '9200',
            'payload': {
                'name': 'Imported Variation Title',
                'regular_price': '10.00',
                'attributes': [],
            },
        }
        product_binding = self.env['kits.woo.product.binding']._find(
            self.instance, odoo_record_id=template.id,
        )
        self.instance._woo_upsert_variant_binding(product_binding, variant, row)
        self.assertEqual(vbind.title, 'Imported Variation Title')
        self.assertEqual(variant.display_name, native_name)

    def test_translations_use_store_language(self):
        Lang = self.env['res.lang']
        fr = Lang.with_context(active_test=False).search([('code', '=', 'fr_FR')], limit=1)
        if not fr:
            self.skipTest('fr_FR language not available')
        if not fr.active:
            self.env['base.language.install'].create({
                'lang_ids': [Command.set(fr.ids)],
                'overwrite': False,
            }).lang_install()
            fr.invalidate_recordset()
        self.instance.lang_id = fr

        template = self._make_template(name='English Shirt')
        template.with_context(lang='fr_FR').write({'name': 'Chemise Anglaise'})
        binding = self.instance._woo_prepare_product_binding_for_store(template)
        self.assertEqual(binding.with_context(lang='fr_FR').title, 'Chemise Anglaise')

        binding.with_context(lang='fr_FR').write({'title': 'Chemise Boutique'})
        binding.with_context(lang='en_US').write({'title': 'Boutique Shirt'})
        self.assertEqual(template.with_context(lang='en_US').name, 'English Shirt')
        self.assertEqual(template.with_context(lang='fr_FR').name, 'Chemise Anglaise')
        self.assertEqual(
            self.instance._woo_store_product_title(binding, template),
            'Chemise Boutique',
        )

    def test_smart_button_opens_bindings(self):
        template = self._make_template()
        self.instance._woo_prepare_product_binding_for_store(template)
        self.other_instance._woo_prepare_product_binding_for_store(template)
        template.invalidate_recordset(['woo_binding_count'])
        self.assertEqual(template.woo_binding_count, 2)
        action = template.action_open_woo_product_bindings()
        self.assertEqual(action['res_model'], 'kits.woo.product.binding')
        self.assertEqual(action['domain'], [('product_tmpl_id', '=', template.id)])
