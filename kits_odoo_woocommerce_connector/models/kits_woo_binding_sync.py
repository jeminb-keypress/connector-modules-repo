# -*- coding: utf-8 -*-
from odoo import fields, models


class KitsWooBindingSync(models.AbstractModel):
    """Sync bookkeeping shared by WooCommerce product and variant bindings.

    Orders, coupons, and catalog records stay on ``kits.connector.binding``.
    These fields keep the existing export, stock, and stale-job behavior on the
    dedicated product and variant bindings.
    """

    _name = 'kits.woo.binding.sync'
    _description = 'WooCommerce Binding Sync'

    sync_state = fields.Selection(
        selection=[
            ('synced', 'Synced'),
            ('not_synced', 'Not Synced'),
            ('error', 'Error'),
            ('deleted', 'Deleted Remote'),
        ],
        default='synced',
        required=True,
        index=True,
    )
    sync_error = fields.Text(copy=False)
    sync_version = fields.Integer(default=1, copy=False)
    sync_date = fields.Datetime(default=fields.Datetime.now)
    active = fields.Boolean(default=True)
    exists_in_store = fields.Boolean(string='Exists in Store', default=True)
    external_data = fields.Json(
        help='Technical snapshot used by image hashes and stale webhook checks. '
             'Store-facing title, descriptions, and image URLs have their own fields.',
    )

    def _kits_mark_synced(self, *, sync_date=False):
        self.write({
            'sync_state': 'synced',
            'sync_error': False,
            'sync_date': sync_date or fields.Datetime.now(),
        })
        return True

    def _kits_mark_not_synced(self, *, error=False):
        for binding in self:
            if binding.sync_state == 'deleted' or not binding.active:
                continue
            binding.write({
                'sync_state': 'error' if error else 'not_synced',
                'sync_error': error or False,
                'sync_version': binding.sync_version + 1,
            })
        return True

    def _kits_mark_error(self, message):
        self.write({
            'sync_state': 'error',
            'sync_error': message or False,
        })
        return True

    def _kits_mark_deleted_remote(self, *, message=False):
        """Archive the binding. The Odoo product or variant is left in place."""
        self.write({
            'active': False,
            'exists_in_store': False,
            'sync_state': 'deleted',
            'sync_error': message or False,
            'sync_date': fields.Datetime.now(),
        })
        return True

    def _kits_is_stale_job(self, expected_version):
        self.ensure_one()
        try:
            expected = int(expected_version)
        except (TypeError, ValueError):
            return False
        return self.sync_version > expected
