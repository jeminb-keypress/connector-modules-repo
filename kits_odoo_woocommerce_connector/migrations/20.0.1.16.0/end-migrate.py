# -*- coding: utf-8 -*-
"""Migrate legacy kits.connector.instance (Woo) rows into kits.woo.instance."""

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


def _copy_woo_instances(cr):
    legacy = None
    for candidate in ('kits_connector_instance_legacy', 'kits_connector_instance'):
        if _table_exists(cr, candidate):
            legacy = candidate
            break
    if not legacy or not _table_exists(cr, 'kits_woo_instance'):
        _logger.info('Woo instance migration skipped (missing tables)')
        return

    legacy_cols = _columns(cr, legacy)
    target_cols = _columns(cr, 'kits_woo_instance')
    shared = sorted((legacy_cols & target_cols) - {'id'})
    if not shared:
        _logger.warning('No shared columns between %s and kits_woo_instance', legacy)
        return

    col_sql = ', '.join(f'"{c}"' for c in shared)
    cr.execute(
        f"""
        INSERT INTO kits_woo_instance (id, {col_sql})
        SELECT i.id, {', '.join(f'i."{c}"' for c in shared)}
          FROM "{legacy}" i
         WHERE COALESCE(i.platform_type, 'woocommerce') = 'woocommerce'
           AND NOT EXISTS (
                SELECT 1 FROM kits_woo_instance w WHERE w.id = i.id
           )
        """
    )
    _logger.info('Copied %s WooCommerce instance row(s)', cr.rowcount)

    # Keep sequence ahead of migrated ids.
    cr.execute("SELECT COALESCE(MAX(id), 0) FROM kits_woo_instance")
    max_id = cr.fetchone()[0] or 0
    cr.execute(
        """
        SELECT setval(
            pg_get_serial_sequence('kits_woo_instance', 'id'),
            GREATEST(%s, 1),
            true
        )
        """,
        (max_id,),
    )


def _remap_mail_and_attachments(cr):
    if not _table_exists(cr, 'kits_woo_instance'):
        return
    cr.execute("SELECT id FROM kits_woo_instance")
    ids = [row[0] for row in cr.fetchall()]
    if not ids:
        return
    cr.execute(
        """
        UPDATE mail_message
           SET model = 'kits.woo.instance'
         WHERE model = 'kits.connector.instance'
           AND res_id = ANY(%s)
        """,
        (ids,),
    )
    cr.execute(
        """
        UPDATE mail_followers
           SET res_model = 'kits.woo.instance'
         WHERE res_model = 'kits.connector.instance'
           AND res_id = ANY(%s)
        """,
        (ids,),
    )
    if _table_exists(cr, 'ir_attachment'):
        cr.execute(
            """
            UPDATE ir_attachment
               SET res_model = 'kits.woo.instance'
             WHERE res_model = 'kits.connector.instance'
               AND res_id = ANY(%s)
            """,
            (ids,),
        )
    if _table_exists(cr, 'ir_model_data'):
        cr.execute(
            """
            UPDATE ir_model_data
               SET model = 'kits.woo.instance'
             WHERE model = 'kits.connector.instance'
               AND res_id = ANY(%s)
            """,
            (ids,),
        )


def _ensure_reference_strings(cr):
    """Normalize job/log/binding instance refs for Woo rows."""
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
               SET instance_id = 'kits.woo.instance,' || split_part(t.instance_id::text, ',', 2),
                   platform_type = COALESCE(t.platform_type, 'woocommerce')
              FROM kits_woo_instance w
             WHERE (
                    t.instance_id::text = w.id::text
                 OR t.instance_id::text = 'kits.connector.instance,' || w.id::text
                 OR t.instance_id::text = 'kits.woo.instance,' || w.id::text
             )
            """
        )


def _drop_migrated_legacy_rows(cr):
    legacy = 'kits_connector_instance_legacy'
    if not _table_exists(cr, legacy) or not _table_exists(cr, 'kits_woo_instance'):
        return
    cr.execute(
        f"""
        DELETE FROM "{legacy}" i
         WHERE COALESCE(i.platform_type, 'woocommerce') = 'woocommerce'
           AND EXISTS (SELECT 1 FROM kits_woo_instance w WHERE w.id = i.id)
        """
    )
    _logger.info('Removed %s migrated Woo rows from legacy table', cr.rowcount)


def migrate(cr, version):
    if not version:
        return
    _copy_woo_instances(cr)
    _remap_mail_and_attachments(cr)
    _ensure_reference_strings(cr)
    _drop_migrated_legacy_rows(cr)
