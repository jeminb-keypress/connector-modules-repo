# -*- coding: utf-8 -*-
"""Preserve legacy instance rows and convert shared FKs to Reference strings."""

import logging

_logger = logging.getLogger(__name__)

REF_TABLES = (
    'kits_connector_sync_job',
    'kits_connector_log',
    'kits_connector_binding',
)


def _table_exists(cr, name):
    cr.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = %s AND table_schema = current_schema()",
        (name,),
    )
    return bool(cr.fetchone())


def _column_type(cr, table, column):
    cr.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s AND table_schema = current_schema()",
        (table, column),
    )
    row = cr.fetchone()
    return row[0] if row else None


def _drop_instance_fks(cr, table):
    cr.execute(
        """
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE nsp.nspname = current_schema()
          AND rel.relname = %s
          AND con.contype = 'f'
          AND pg_get_constraintdef(con.oid) ILIKE '%%instance_id%%'
        """,
        (table,),
    )
    for (conname,) in cr.fetchall():
        cr.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{conname}"')


def _ensure_platform_type_column(cr, table):
    cr.execute(
        f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS platform_type varchar'
    )


def _convert_instance_id_to_reference(cr, table, legacy_table):
    col_type = _column_type(cr, table, 'instance_id')
    if not col_type:
        return
    if col_type in ('character varying', 'text', 'varchar'):
        # Already Reference-like; normalize bare integers if any remain.
        cr.execute(
            f"""
            UPDATE "{table}" t
               SET instance_id = CASE i.platform_type
                    WHEN 'shopify' THEN 'kits.shopify.instance,' || regexp_replace(t.instance_id, '^[^,]*,?', '')
                    ELSE 'kits.woo.instance,' || regexp_replace(t.instance_id, '^[^,]*,?', '')
               END
              FROM "{legacy_table}" i
             WHERE t.instance_id ~ '^[0-9]+$'
               AND i.id = t.instance_id::integer
            """
        )
        return

    _drop_instance_fks(cr, table)
    _ensure_platform_type_column(cr, table)
    cr.execute(
        f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS instance_id_ref varchar'
    )
    cr.execute(
        f"""
        UPDATE "{table}" t
           SET instance_id_ref = CASE COALESCE(i.platform_type, 'woocommerce')
                WHEN 'shopify' THEN 'kits.shopify.instance,' || t.instance_id::text
                ELSE 'kits.woo.instance,' || t.instance_id::text
           END,
               platform_type = COALESCE(t.platform_type, i.platform_type)
          FROM "{legacy_table}" i
         WHERE i.id = t.instance_id
        """
    )
    # Orphans (deleted instance): keep a woo-shaped ref so rows are not discarded.
    cr.execute(
        f"""
        UPDATE "{table}"
           SET instance_id_ref = 'kits.woo.instance,' || instance_id::text
         WHERE instance_id_ref IS NULL AND instance_id IS NOT NULL
        """
    )
    cr.execute(f'ALTER TABLE "{table}" DROP COLUMN instance_id')
    cr.execute(
        f'ALTER TABLE "{table}" RENAME COLUMN instance_id_ref TO instance_id'
    )
    cr.execute(
        f'ALTER TABLE "{table}" ALTER COLUMN instance_id TYPE varchar '
        f'USING instance_id::varchar'
    )


def migrate(cr, version):
    if not version:
        return

    legacy = None
    if _table_exists(cr, 'kits_connector_instance_legacy'):
        legacy = 'kits_connector_instance_legacy'
    elif _table_exists(cr, 'kits_connector_instance'):
        cr.execute(
            'ALTER TABLE kits_connector_instance '
            'RENAME TO kits_connector_instance_legacy'
        )
        legacy = 'kits_connector_instance_legacy'
        _logger.info(
            'Renamed kits_connector_instance → kits_connector_instance_legacy'
        )
    else:
        _logger.info('No legacy connector instance table to migrate')
        return

    for table in REF_TABLES:
        if not _table_exists(cr, table):
            continue
        _logger.info('Converting %s.instance_id to Reference strings', table)
        _convert_instance_id_to_reference(cr, table, legacy)
