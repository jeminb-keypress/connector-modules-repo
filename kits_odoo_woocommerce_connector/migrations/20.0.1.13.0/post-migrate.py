# -*- coding: utf-8 -*-
"""Copy legacy generic bindings for customers and products into the dedicated models."""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})
    Binding = env['kits.connector.binding'].sudo().with_context(active_test=False)
    Customer = env['kits.woo.customer.binding'].sudo()
    Product = env['kits.woo.product.binding'].sudo().with_context(active_test=False)
    Variant = env['kits.woo.variant.binding'].sudo().with_context(active_test=False)

    partners = Binding.search([
        ('platform_type', '=', 'woocommerce'),
        ('odoo_model', '=', 'res.partner'),
    ])
    for binding in partners:
        partner = env['res.partner'].browse(binding.odoo_record_id).exists()
        if not partner:
            continue
        if Customer.search_count([
            ('instance_id', '=', binding.instance_id.id),
            ('external_id', '=', binding.external_id),
        ]):
            continue
        Customer.create({
            'instance_id': binding.instance_id.id,
            'company_id': binding.company_id.id,
            'partner_id': partner.id,
            'external_id': binding.external_id,
            'active': binding.active and binding.sync_state not in ('deleted', 'deleted_local'),
        })
    if partners:
        partners.unlink()

    templates = Binding.search([
        ('platform_type', '=', 'woocommerce'),
        ('odoo_model', '=', 'product.template'),
    ])
    for binding in templates:
        template = env['product.template'].browse(binding.odoo_record_id).exists()
        if not template:
            continue
        if Product.search_count([
            ('instance_id', '=', binding.instance_id.id),
            ('external_id', '=', binding.external_id),
        ]):
            continue
        data = binding.external_data or {}
        Product.create({
            'instance_id': binding.instance_id.id,
            'company_id': binding.company_id.id,
            'product_tmpl_id': template.id,
            'external_id': binding.external_id,
            'title': template.name,
            'product_type': data.get('type') if data.get('type') in (
                'simple', 'variable', 'grouped', 'external', 'bundle',
            ) else 'simple',
            'published': binding.sync_state != 'deleted',
            'exists_in_store': binding.sync_state != 'deleted',
            'active': binding.active and binding.sync_state != 'deleted_local',
            'sync_state': 'deleted' if binding.sync_state == 'deleted' else (
                'error' if binding.sync_state == 'error' else 'synced'
            ),
            'sync_version': binding.sync_version or 1,
            'external_data': data,
        })

    variants = Binding.search([
        ('platform_type', '=', 'woocommerce'),
        ('odoo_model', '=', 'product.product'),
    ])
    for binding in variants:
        variant = env['product.product'].browse(binding.odoo_record_id).exists()
        if not variant:
            continue
        if Variant.search_count([
            ('instance_id', '=', binding.instance_id.id),
            ('external_id', '=', binding.external_id),
        ]):
            continue
        data = binding.external_data or {}
        parent_external = str(data.get('parent_id') or binding.external_id)
        parent = Product.search([
            ('instance_id', '=', binding.instance_id.id),
            ('external_id', '=', parent_external),
        ], limit=1)
        if not parent:
            parent = Product.search([
                ('instance_id', '=', binding.instance_id.id),
                ('product_tmpl_id', '=', variant.product_tmpl_id.id),
            ], limit=1)
        if not parent:
            parent = Product.create({
                'instance_id': binding.instance_id.id,
                'company_id': binding.company_id.id,
                'product_tmpl_id': variant.product_tmpl_id.id,
                'external_id': parent_external,
                'title': variant.product_tmpl_id.name,
                'product_type': 'variable' if parent_external != binding.external_id else 'simple',
                'exists_in_store': binding.sync_state != 'deleted',
                'active': binding.sync_state != 'deleted_local',
            })
        Variant.create({
            'product_binding_id': parent.id,
            'instance_id': binding.instance_id.id,
            'company_id': binding.company_id.id,
            'product_id': variant.id,
            'external_id': binding.external_id,
            'sku': data.get('sku') or variant.default_code,
            'exists_in_store': binding.sync_state != 'deleted',
            'active': binding.active and binding.sync_state != 'deleted_local',
            'sync_state': 'deleted' if binding.sync_state == 'deleted' else 'synced',
            'sync_version': binding.sync_version or 1,
            'external_data': data,
        })
    if templates:
        templates.unlink()
    if variants:
        variants.unlink()
    _logger.info('WooCommerce dedicated bindings migration finished.')
