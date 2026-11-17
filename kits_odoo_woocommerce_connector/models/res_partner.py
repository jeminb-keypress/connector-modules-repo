# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # Base Char keeps a display-only name list; rename its label so the Woo
    # Many2many owns the "Connected Stores" string without a field-label clash.
    kits_connected_instance_names = fields.Char(
        string='Connected Store Names',
    )
    woo_customer_binding_ids = fields.One2many(
        'kits.woo.customer.binding',
        'partner_id',
        string='WooCommerce Customer Bindings',
        help='Technical inverse of kits.woo.customer.binding. Used to keep '
             'Connected Stores (woo_instance_ids) in sync with bindings.',
    )
    woo_instance_ids = fields.Many2many(
        'kits.woo.instance',
        'res_partner_woo_instance_rel',
        'partner_id',
        'instance_id',
        string='Connected Stores',
        compute='_compute_woo_instance_ids',
        store=True,
        readonly=True,
        copy=False,
        help='WooCommerce stores linked to this contact through active customer '
             'bindings. Derived from kits.woo.customer.binding; not edited manually.',
    )
    woo_is_store_customer = fields.Boolean(
        string='WooCommerce Customer',
        compute='_compute_woo_sync_info',
        search='_search_woo_is_store_customer',
        help='On while this contact has an active customer binding for at least one '
             'WooCommerce store. Soft-deleted remote bindings do not keep this flag on.',
    )
    woo_company_name = fields.Char(
        string='Billing Company',
        help='WooCommerce billing company for this customer. Kept on the same contact so import does not create a second partner.',
    )
    woo_binding_count = fields.Integer(
        string='Woo Bindings',
        compute='_compute_woo_sync_info',
        help='Active WooCommerce customer bindings for this contact.',
    )
    woo_sync_state = fields.Selection(
        selection=[
            ('none', 'Not Linked'),
            ('synced', 'Synced'),
            ('not_synced', 'Not Synced'),
            ('error', 'Error'),
            ('deleted', 'Deleted Remotely'),
            ('manual_review', 'Manual Review'),
            ('mixed', 'Mixed'),
        ],
        string='Woo Sync Status',
        compute='_compute_woo_sync_info',
        help='Aggregated status across WooCommerce store bindings for this contact. '
             'When every binding is soft-deleted remotely, status is Deleted Remotely '
             '(not Not Linked).',
    )

    @api.depends(
        'woo_customer_binding_ids.instance_id',
        'woo_customer_binding_ids.active',
        'woo_customer_binding_ids.partner_id',
    )
    def _compute_woo_instance_ids(self):
        """Connected Stores = distinct stores from active customer bindings only."""
        Binding = self.env['kits.woo.customer.binding']
        for partner in self:
            if not partner.id:
                partner.woo_instance_ids = Binding.env['kits.woo.instance']
                continue
            # One2many excludes inactive by default; search active bindings explicitly
            # so soft-deleted bindings never leave a stale store link.
            instances = Binding.search([
                ('partner_id', '=', partner.id),
                ('active', '=', True),
            ]).mapped('instance_id')
            partner.woo_instance_ids = instances

    def _kits_refresh_partner_connected_stores(self):
        """Force store recompute after customer binding create/write/unlink."""
        self.invalidate_recordset(['woo_instance_ids', 'woo_customer_binding_ids'])
        self._compute_woo_instance_ids()

    def _compute_woo_sync_info(self):
        Binding = self.env['kits.woo.customer.binding'].with_context(active_test=False)
        for partner in self:
            all_bindings = Binding.search([('partner_id', '=', partner.id)])
            active = all_bindings.filtered('active')
            partner.woo_binding_count = len(active)
            partner.woo_is_store_customer = bool(active)
            if not all_bindings:
                partner.woo_sync_state = 'none'
                continue
            if not active:
                # All bindings archived — typically deleted_remotely.
                if all(
                    binding.sync_state == 'deleted_remotely'
                    for binding in all_bindings
                ):
                    partner.woo_sync_state = 'deleted'
                else:
                    partner.woo_sync_state = 'mixed'
                continue
            states = set(active.mapped('sync_state'))
            if states == {'synced'}:
                partner.woo_sync_state = 'synced'
            elif states == {'not_synced'}:
                partner.woo_sync_state = 'not_synced'
            elif states == {'error'}:
                partner.woo_sync_state = 'error'
            elif states == {'manual_review'}:
                partner.woo_sync_state = 'manual_review'
            elif states == {'deleted_remotely'}:
                partner.woo_sync_state = 'deleted'
            else:
                partner.woo_sync_state = 'mixed'

    def _search_woo_is_store_customer(self, operator, value):
        if operator == 'in':
            selected = {bool(item) for item in value}
            if selected == {True, False}:
                return []
            operator, value = '=', True in selected
        elif operator == 'not in':
            selected = {bool(item) for item in value}
            if selected == {True, False}:
                return [('id', '=', 0)]
            operator, value = '!=', True in selected
        if operator not in ('=', '!='):
            return NotImplemented
        partner_ids = self.env['kits.woo.customer.binding'].search([
            ('active', '=', True),
        ]).partner_id.ids
        wants_customers = (operator == '=' and bool(value)) or (operator == '!=' and not value)
        if wants_customers:
            return [('id', 'in', partner_ids or [0])]
        return [('id', 'not in', partner_ids or [0])]

    def _compute_kits_connector_info(self):
        super()._compute_kits_connector_info()
        Binding = self.env['kits.woo.customer.binding']
        for partner in self:
            extra = Binding.search([
                ('partner_id', '=', partner.id),
                ('active', '=', True),
            ])
            if not extra:
                continue
            names = [
                name for name in (partner.kits_connected_instance_names or '').split(', ')
                if name
            ]
            for instance in extra.mapped('instance_id'):
                if instance.display_name not in names:
                    names.append(instance.display_name)
            partner.kits_connected_instance_names = ', '.join(names) if names else False
            partner.kits_binding_count += len(extra)

    def action_kits_open_bindings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Customer Bindings'),
            'res_model': 'kits.woo.customer.binding',
            'view_mode': 'list,form',
            'domain': [('partner_id', '=', self.id)],
        }

    def unlink(self):
        bindings = self.env['kits.woo.customer.binding'].sudo().search([
            ('partner_id', 'in', self.ids),
        ])
        if bindings:
            bindings.unlink()
        return super().unlink()

