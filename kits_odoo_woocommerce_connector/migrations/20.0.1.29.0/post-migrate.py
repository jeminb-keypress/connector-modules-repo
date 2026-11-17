# -*- coding: utf-8 -*-
"""Clear invalid connector catalog external_id values like 'False'/'None'."""

import logging

_logger = logging.getLogger(__name__)

_TABLES = (
    'kits_woo_category',
    'kits_woo_tag',
)

_BAD = ('False', 'false', 'None', 'none', 'null', 'NULL')


def migrate(cr, version):
    for table in _TABLES:
        cr.execute(
            f"""
            UPDATE {table}
               SET external_id = NULL
             WHERE external_id IS NOT NULL
               AND btrim(external_id) = ANY(%s)
            """,
            (list(_BAD),),
        )
        if cr.rowcount:
            _logger.info(
                'Cleared %s invalid external_id value(s) on %s',
                cr.rowcount, table,
            )
