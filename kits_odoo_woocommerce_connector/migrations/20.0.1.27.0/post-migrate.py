# -*- coding: utf-8 -*-
"""Drop unused kits.woo.product.binding snapshot / legacy fields."""

import logging

_logger = logging.getLogger(__name__)

_DROP_COLUMNS = (
    'product_type',
    'taxable',
    'virtual',
    'sku',
    'regular_price',
    'sale_price',
    'variant_count',
    'store_create_date',
)

_DROP_TABLES = (
    'kits_woo_product_binding_category_rel',
    'kits_woo_product_binding_tag_rel',
)


def migrate(cr, version):
    for table in _DROP_TABLES:
        cr.execute(f'DROP TABLE IF EXISTS {table} CASCADE')
        _logger.info('Dropped table %s', table)

    for column in _DROP_COLUMNS:
        cr.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'kits_woo_product_binding'
              AND column_name = %s
            """,
            (column,),
        )
        if cr.fetchone():
            cr.execute(
                f'ALTER TABLE kits_woo_product_binding DROP COLUMN IF EXISTS {column}'
            )
            _logger.info('Dropped kits_woo_product_binding.%s', column)
