# -*- coding: utf-8 -*-
"""Rename optional public webhook URL column without dropping existing values."""


def migrate(cr, version):
    cr.execute(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_name = 'kits_woo_instance'
           AND column_name IN (
                'woo_webhook_public_base_url',
                'public_webhook_url'
           )
        """
    )
    cols = {row[0] for row in cr.fetchall()}
    if 'public_webhook_url' not in cols and 'woo_webhook_public_base_url' in cols:
        cr.execute(
            'ALTER TABLE kits_woo_instance '
            'RENAME COLUMN woo_webhook_public_base_url TO public_webhook_url'
        )
    elif 'public_webhook_url' in cols and 'woo_webhook_public_base_url' in cols:
        cr.execute(
            """
            UPDATE kits_woo_instance
               SET public_webhook_url = woo_webhook_public_base_url
             WHERE (public_webhook_url IS NULL OR public_webhook_url = '')
               AND woo_webhook_public_base_url IS NOT NULL
               AND woo_webhook_public_base_url <> ''
            """
        )
