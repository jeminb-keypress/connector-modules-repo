# -*- coding: utf-8 -*-
"""Backfill kits.woo.category / tag / attribute / term from generic bindings where possible."""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'kits_woo_category'
        """
    )
    if not cr.fetchone():
        return

    # Categories: create connector rows from product.category bindings
    cr.execute(
        """
        INSERT INTO kits_woo_category (
            instance_id, company_id, external_id, name, odoo_category_id,
            display_type, sync_state, active, exists_in_store,
            create_uid, write_uid, create_date, write_date
        )
        SELECT
            CAST(split_part(b.instance_id, ',', 2) AS integer),
            b.company_id,
            b.external_id,
            COALESCE(c.name->>'en_US', c.name::text, 'Category ' || b.external_id),
            b.odoo_record_id,
            'default',
            COALESCE(b.sync_state, 'synced'),
            COALESCE(b.active, true),
            true,
            1, 1, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC'
        FROM kits_connector_binding b
        LEFT JOIN product_category c ON c.id = b.odoo_record_id
        WHERE b.odoo_model = 'product.category'
          AND b.platform_type = 'woocommerce'
          AND b.instance_id LIKE 'kits.woo.instance,%%'
          AND NOT EXISTS (
              SELECT 1 FROM kits_woo_category w
              WHERE w.instance_id = CAST(split_part(b.instance_id, ',', 2) AS integer)
                AND w.external_id = b.external_id
          )
        """
    )
    _logger.info('kits.woo.category backfill rows: %s', cr.rowcount)

    # Tags
    cr.execute(
        """
        SELECT 1 FROM information_schema.tables WHERE table_name = 'product_tag'
        """
    )
    if cr.fetchone():
        cr.execute(
            """
            INSERT INTO kits_woo_tag (
                instance_id, company_id, external_id, name, odoo_tag_id,
                sync_state, active, exists_in_store,
                create_uid, write_uid, create_date, write_date
            )
            SELECT
                CAST(split_part(b.instance_id, ',', 2) AS integer),
                b.company_id,
                b.external_id,
                COALESCE(t.name->>'en_US', t.name::text, 'Tag ' || b.external_id),
                b.odoo_record_id,
                COALESCE(b.sync_state, 'synced'),
                COALESCE(b.active, true),
                true,
                1, 1, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC'
            FROM kits_connector_binding b
            LEFT JOIN product_tag t ON t.id = b.odoo_record_id
            WHERE b.odoo_model = 'product.tag'
              AND b.platform_type = 'woocommerce'
              AND b.instance_id LIKE 'kits.woo.instance,%%'
              AND NOT EXISTS (
                  SELECT 1 FROM kits_woo_tag w
                  WHERE w.instance_id = CAST(split_part(b.instance_id, ',', 2) AS integer)
                    AND w.external_id = b.external_id
              )
            """
        )
        _logger.info('kits.woo.tag backfill rows: %s', cr.rowcount)
