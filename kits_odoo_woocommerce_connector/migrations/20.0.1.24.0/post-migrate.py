# -*- coding: utf-8 -*-
"""Rebuild Connected Stores (res.partner.woo_instance_ids) from customer bindings.

The previous Connected Stores Char (kits_connected_instance_names) was computed
display text only. Store relationships always lived in kits.woo.customer.binding.
This migration fills the new stored Many2many from active bindings only.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        SELECT to_regclass('res_partner_woo_instance_rel')
    """)
    if not cr.fetchone()[0]:
        _logger.info(
            'res_partner_woo_instance_rel not ready yet; ORM will create and compute.'
        )
        return

    cr.execute("DELETE FROM res_partner_woo_instance_rel")
    cr.execute("""
        INSERT INTO res_partner_woo_instance_rel (partner_id, instance_id)
        SELECT DISTINCT partner_id, instance_id
          FROM kits_woo_customer_binding
         WHERE active = TRUE
           AND partner_id IS NOT NULL
           AND instance_id IS NOT NULL
    """)
    _logger.info(
        'Rebuilt Connected Stores from active customer bindings: %s rows',
        cr.rowcount,
    )
