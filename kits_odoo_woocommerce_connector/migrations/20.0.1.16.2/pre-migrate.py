# -*- coding: utf-8 -*-
"""Strip removed field from stored view arches before model reload.

``woo_update_customer_on_order_import`` is removed in 20.0.1.16.2. Existing
inherit views in the DB still reference it and would fail combined-view
validation during upgrade if not cleaned first.

``ir_ui_view.arch_db`` is jsonb, so ``::text`` may escape quotes as ``\\"``.
"""


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        r"""
        UPDATE ir_ui_view
           SET arch_db = regexp_replace(
               arch_db::text,
               '<field name=\\?"woo_update_customer_on_order_import\\?"/>',
               '',
               'g'
           )::jsonb
         WHERE arch_db::text LIKE %s
        """,
        ('%woo_update_customer_on_order_import%',),
    )
