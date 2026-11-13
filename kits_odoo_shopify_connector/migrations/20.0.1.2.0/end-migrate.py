# -*- coding: utf-8 -*-
"""Migrate legacy kits.connector.instance (Shopify) rows into kits.shopify.instance."""

import logging

_logger = logging.getLogger(__name__)


def _table_exists(cr, name):
    cr.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = %s AND table_schema = current_schema()",
        (name,),
    )
    return bool(cr.fetchone())


def _columns(cr, table):
    cr.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND table_schema = current_schema()",
        (table,),
    )
    return {row[0] for row in cr.fetchall()}


def migrate(cr, version):
    if not version:
        return

    legacy = None
    for candidate in ('kits_connector_instance_legacy', 'kits_connector_instance'):
        if _table_exists(cr, candidate):
            legacy = candidate
            break
    if not legacy or not _table_exists(cr, 'kits_shopify_instance'):
        _logger.info('Shopify instance migration skipped (missing tables)')
        return

    legacy_cols = _columns(cr, legacy)
    target_cols = _columns(cr, 'kits_shopify_instance')
    shared = sorted((legacy_cols & target_cols) - {'id'})
    if not shared:
        return

    col_sql = ', '.join(f'"{c}"' for c in shared)
    cr.execute(
        f"""
        INSERT INTO kits_shopify_instance (id, {col_sql})
        SELECT i.id, {', '.join(f'i."{c}"' for c in shared)}
          FROM "{legacy}" i
         WHERE i.platform_type = 'shopify'
           AND NOT EXISTS (
                SELECT 1 FROM kits_shopify_instance s WHERE s.id = i.id
           )
        """
    )
    _logger.info('Copied %s Shopify instance row(s)', cr.rowcount)

    cr.execute("SELECT COALESCE(MAX(id), 0) FROM kits_shopify_instance")
    max_id = cr.fetchone()[0] or 0
    cr.execute(
        """
        SELECT setval(
            pg_get_serial_sequence('kits_shopify_instance', 'id'),
            GREATEST(%s, 1),
            true
        )
        """,
        (max_id,),
    )

    cr.execute("SELECT id FROM kits_shopify_instance")
    ids = [row[0] for row in cr.fetchall()]
    if ids:
        cr.execute(
            """
            UPDATE mail_message
               SET model = 'kits.shopify.instance'
             WHERE model = 'kits.connector.instance'
               AND res_id = ANY(%s)
            """,
            (ids,),
        )
        cr.execute(
            """
            UPDATE mail_followers
               SET res_model = 'kits.shopify.instance'
             WHERE res_model = 'kits.connector.instance'
               AND res_id = ANY(%s)
            """,
            (ids,),
        )
        if _table_exists(cr, 'ir_attachment'):
            cr.execute(
                """
                UPDATE ir_attachment
                   SET res_model = 'kits.shopify.instance'
                 WHERE res_model = 'kits.connector.instance'
                   AND res_id = ANY(%s)
                """,
                (ids,),
            )
        if _table_exists(cr, 'ir_model_data'):
            cr.execute(
                """
                UPDATE ir_model_data
                   SET model = 'kits.shopify.instance'
                 WHERE model = 'kits.connector.instance'
                   AND res_id = ANY(%s)
                """,
                (ids,),
            )

    for table in (
        'kits_connector_sync_job',
        'kits_connector_log',
        'kits_connector_binding',
    ):
        if not _table_exists(cr, table):
            continue
        cr.execute(
            f"""
            UPDATE "{table}" t
               SET instance_id = 'kits.shopify.instance,' || split_part(t.instance_id::text, ',', 2),
                   platform_type = COALESCE(t.platform_type, 'shopify')
              FROM kits_shopify_instance s
             WHERE (
                    t.instance_id::text = s.id::text
                 OR t.instance_id::text = 'kits.connector.instance,' || s.id::text
                 OR t.instance_id::text = 'kits.shopify.instance,' || s.id::text
             )
            """
        )

    cr.execute(
        f"""
        DELETE FROM "{legacy}" i
         WHERE i.platform_type = 'shopify'
           AND EXISTS (SELECT 1 FROM kits_shopify_instance s WHERE s.id = i.id)
        """
    )
    _logger.info('Removed %s migrated Shopify rows from legacy table', cr.rowcount)
