# -*- coding: utf-8 -*-
"""Initialize missing store-specific presentation fields on product/variant bindings.

Does not overwrite customized store titles or descriptions.
Makes product binding external_id nullable for pending (not yet exported) notes.
"""

import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # Allow pending product notes without a WooCommerce id (same pattern as categories).
    cr.execute(
        """
        SELECT is_nullable
        FROM information_schema.columns
        WHERE table_name = 'kits_woo_product_binding' AND column_name = 'external_id'
        """
    )
    row = cr.fetchone()
    if row and row[0] == 'NO':
        cr.execute(
            'ALTER TABLE kits_woo_product_binding ALTER COLUMN external_id DROP NOT NULL'
        )
        _logger.info('kits.woo.product.binding.external_id is now optional')

    ProductBinding = env['kits.woo.product.binding'].with_context(active_test=False)
    VariantBinding = env['kits.woo.variant.binding'].with_context(active_test=False)
    filled_titles = filled_descs = filled_shorts = filled_variant_titles = 0

    for binding in ProductBinding.search([]):
        template = binding.product_tmpl_id
        if not template:
            continue
        instance = binding.instance_id
        lang = instance._woo_store_lang_code() if hasattr(instance, '_woo_store_lang_code') else (
            instance.lang_id.code if instance.lang_id else env.lang or 'en_US'
        )
        binding_l = binding.with_context(lang=lang)
        template_l = template.with_context(lang=lang)
        updates = {}
        if not (binding_l.title or '').strip() and template_l.name:
            updates['title'] = template_l.name
            filled_titles += 1
        if not (binding_l.description or '').strip() and template_l.description:
            # Only fill when the stored value is empty in this language.
            if not binding.description:
                updates['description'] = template_l.description
                filled_descs += 1
        if not (binding_l.description_short or '').strip() and template_l.description_sale:
            if not binding.description_short:
                updates['description_short'] = template_l.description_sale
                filled_shorts += 1
        if updates:
            binding.with_context(
                lang=lang,
                kits_woo_skip_presentation_dirty=True,
                kits_woo_skip_dirty=True,
            ).write(updates)

    for binding in VariantBinding.search([]):
        variant = binding.product_id
        if not variant:
            continue
        instance = binding.instance_id
        lang = instance._woo_store_lang_code() if hasattr(instance, '_woo_store_lang_code') else (
            instance.lang_id.code if instance.lang_id else env.lang or 'en_US'
        )
        binding_l = binding.with_context(lang=lang)
        if (binding_l.title or '').strip():
            continue
        binding.with_context(
            lang=lang,
            kits_woo_skip_presentation_dirty=True,
            kits_woo_skip_dirty=True,
        ).write({'title': variant.with_context(lang=lang).display_name})
        filled_variant_titles += 1

    _logger.info(
        'kits_odoo_woocommerce_connector 20.0.1.26.0: filled product titles=%s '
        'descriptions=%s shorts=%s variant titles=%s',
        filled_titles, filled_descs, filled_shorts, filled_variant_titles,
    )
