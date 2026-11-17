# -*- coding: utf-8 -*-
"""Make connector categories/tags the WooCommerce source of truth.

- Allow NULL external_id (not yet exported).
- Drop case-sensitive tag name unique constraint (Python validates case-insensitively).
- Migrate product binding native category/tag M2Ms to connector-side M2Ms when a
  reliable same-store mapping exists via odoo_category_id / odoo_tag_id.
- Do not create, update, or delete native product.category / product.tag rows.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        SELECT column_name, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'kits_woo_category' AND column_name = 'external_id'
    """)
    row = cr.fetchone()
    if row and row[1] == 'NO':
        cr.execute('ALTER TABLE kits_woo_category ALTER COLUMN external_id DROP NOT NULL')
        _logger.info('kits.woo.category.external_id is now optional')

    cr.execute("""
        SELECT column_name, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'kits_woo_tag' AND column_name = 'external_id'
    """)
    row = cr.fetchone()
    if row and row[1] == 'NO':
        cr.execute('ALTER TABLE kits_woo_tag ALTER COLUMN external_id DROP NOT NULL')
        _logger.info('kits.woo.tag.external_id is now optional')

    cr.execute("""
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'kits_woo_tag'::regclass
          AND conname LIKE '%name%'
          AND contype = 'u'
    """)
    for (conname,) in cr.fetchall():
        cr.execute(f'ALTER TABLE kits_woo_tag DROP CONSTRAINT IF EXISTS "{conname}"')
        _logger.info('Dropped kits.woo.tag unique constraint %s', conname)

    # Product binding: native categories → connector categories (same store via odoo link)
    cr.execute("""
        INSERT INTO kits_woo_product_binding_woo_category_rel (binding_id, woo_category_id)
        SELECT DISTINCT rel.binding_id, wc.id
        FROM kits_woo_product_binding_category_rel rel
        JOIN kits_woo_product_binding pb ON pb.id = rel.binding_id
        JOIN kits_woo_category wc
          ON wc.odoo_category_id = rel.category_id
         AND wc.instance_id = pb.instance_id
         AND wc.external_id IS NOT NULL
         AND wc.external_id != ''
        WHERE NOT EXISTS (
            SELECT 1 FROM kits_woo_product_binding_woo_category_rel existing
            WHERE existing.binding_id = rel.binding_id
              AND existing.woo_category_id = wc.id
        )
    """)
    _logger.info(
        'Migrated product binding category assignments: %s rows',
        cr.rowcount,
    )

    cr.execute("""
        SELECT COUNT(*) FROM kits_woo_product_binding_category_rel rel
        JOIN kits_woo_product_binding pb ON pb.id = rel.binding_id
        WHERE NOT EXISTS (
            SELECT 1 FROM kits_woo_category wc
            WHERE wc.odoo_category_id = rel.category_id
              AND wc.instance_id = pb.instance_id
              AND wc.external_id IS NOT NULL
              AND wc.external_id != ''
        )
    """)
    unmapped_cats = cr.fetchone()[0]
    if unmapped_cats:
        _logger.warning(
            'Unmappable native product.category assignments on product bindings '
            '(left for manual review; native rows unchanged): %s',
            unmapped_cats,
        )

    # Product binding: native tags → connector tags
    cr.execute("""
        SELECT to_regclass('kits_woo_product_binding_tag_rel')
    """)
    if cr.fetchone()[0]:
        cr.execute("""
            INSERT INTO kits_woo_product_binding_woo_tag_rel (binding_id, woo_tag_id)
            SELECT DISTINCT rel.binding_id, wt.id
            FROM kits_woo_product_binding_tag_rel rel
            JOIN kits_woo_product_binding pb ON pb.id = rel.binding_id
            JOIN kits_woo_tag wt
              ON wt.odoo_tag_id = rel.tag_id
             AND wt.instance_id = pb.instance_id
             AND wt.external_id IS NOT NULL
             AND wt.external_id != ''
            WHERE NOT EXISTS (
                SELECT 1 FROM kits_woo_product_binding_woo_tag_rel existing
                WHERE existing.binding_id = rel.binding_id
                  AND existing.woo_tag_id = wt.id
            )
        """)
        _logger.info(
            'Migrated product binding tag assignments: %s rows',
            cr.rowcount,
        )
        cr.execute("""
            SELECT COUNT(*) FROM kits_woo_product_binding_tag_rel rel
            JOIN kits_woo_product_binding pb ON pb.id = rel.binding_id
            WHERE NOT EXISTS (
                SELECT 1 FROM kits_woo_tag wt
                WHERE wt.odoo_tag_id = rel.tag_id
                  AND wt.instance_id = pb.instance_id
                  AND wt.external_id IS NOT NULL
                  AND wt.external_id != ''
            )
        """)
        unmapped_tags = cr.fetchone()[0]
        if unmapped_tags:
            _logger.warning(
                'Unmappable native product.tag assignments on product bindings '
                '(left for manual review; native rows unchanged): %s',
                unmapped_tags,
            )

    # Ensure sync_state for pending local records
    cr.execute("""
        UPDATE kits_woo_category
        SET sync_state = 'not_synced'
        WHERE (external_id IS NULL OR external_id = '')
          AND sync_state = 'synced'
    """)
    cr.execute("""
        UPDATE kits_woo_tag
        SET sync_state = 'not_synced'
        WHERE (external_id IS NULL OR external_id = '')
          AND sync_state = 'synced'
    """)
