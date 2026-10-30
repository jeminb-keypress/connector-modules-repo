# -*- coding: utf-8 -*-
"""Module install/upgrade hooks."""


def post_init_hook(env):
    """Migrate legacy Sync-to-Woo flag into per-store Many2many selection.

    - Products already bound to a Woo store get that store selected.
    - Products with legacy ``woo_sync_to_store`` and no bindings: if the company
      has exactly one Woo store, select it; otherwise leave empty (admin chooses).
    """
    Binding = env['kits.connector.binding'].sudo()
    Product = env['product.template'].sudo()
    Instance = env['kits.connector.instance'].sudo()

    bindings = Binding.search([
        ('odoo_model', '=', 'product.template'),
        ('platform_type', '=', 'woocommerce'),
        ('active', '=', True),
    ])
    by_template = {}
    for binding in bindings:
        by_template.setdefault(binding.odoo_record_id, set()).add(binding.instance_id.id)

    for tmpl_id, instance_ids in by_template.items():
        template = Product.browse(tmpl_id).exists()
        if not template:
            continue
        missing = instance_ids - set(template.woo_instance_ids.ids)
        if missing:
            template.with_context(kits_woo_skip_dirty=True).write({
                'woo_instance_ids': [(4, iid) for iid in missing],
            })

    # Legacy boolean column may already be recomputed; use bindings-first above.
    # Also cover unbound products that still show sync intent via a single store.
    lone_by_company = {}
    for instance in Instance.search([
        ('platform_type', '=', 'woocommerce'),
        ('active', '=', True),
    ]):
        lone_by_company.setdefault(instance.company_id.id, [])
        lone_by_company[instance.company_id.id].append(instance)

    for company_id, instances in lone_by_company.items():
        if len(instances) != 1:
            continue
        instance = instances[0]
        # Products with no store selected yet but already sold/synced intent via
        # existing simple flag recompute is False — skip mass assign.
        # Only attach the lone store when product has a variant binding on it.
        variant_bindings = Binding.search([
            ('instance_id', '=', instance.id),
            ('odoo_model', '=', 'product.product'),
            ('active', '=', True),
        ])
        tmpl_ids = set()
        for binding in variant_bindings:
            variant = env['product.product'].browse(binding.odoo_record_id).exists()
            if variant:
                tmpl_ids.add(variant.product_tmpl_id.id)
        for tmpl_id in tmpl_ids:
            template = Product.browse(tmpl_id).exists()
            if template and instance not in template.woo_instance_ids:
                template.with_context(kits_woo_skip_dirty=True).write({
                    'woo_instance_ids': [(4, instance.id)],
                })
