# -*- coding: utf-8 -*-
"""Safe customer binding / waiting-list migration.

Existing customer bindings stay linked. New sync fields default to synced.
No remote API calls. Soft-delete replaces unlink going forward only.
"""


def migrate(cr, version):
    cr.execute(
        """
        UPDATE kits_woo_customer_binding
           SET sync_state = COALESCE(sync_state, 'synced'),
               sync_date = COALESCE(sync_date, NOW() AT TIME ZONE 'UTC')
         WHERE sync_state IS NULL
            OR sync_date IS NULL
        """
    )
