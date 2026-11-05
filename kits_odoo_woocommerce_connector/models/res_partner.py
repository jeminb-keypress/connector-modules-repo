# -*- coding: utf-8 -*-
from odoo import fields, models
from odoo.exceptions import UserError


class ResPartner(models.Model):
    _inherit = 'res.partner'

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
            partner.kits_connected_instance_ids |= extra.instance_id
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

    def _woo_get_instances_for_company(self):
        return self.env['kits.connector.instance'].search([
            ('platform_type', '=', 'woocommerce'),
            ('active', '=', True),
            ('company_id', '=', self.env.company.id),
        ])

    def _woo_resolve_instance(self, instance=None):
        if instance:
            return instance
        instances = self._woo_get_instances_for_company()
        if not instances:
            raise UserError(self.env._(
                'No active WooCommerce store exists for the current company.'
            ))
        if len(instances) > 1:
            return False
        return instances

    def _check_woo_sync_access(self):
        if not self.env.user.has_group(
            'kits_odoo_base_connector.group_kits_connector_manager'
        ):
            raise UserError(self.env._(
                'Only Connector Administrators can synchronize WooCommerce customers.'
            ))

    def action_woo_fetch_customer(self):
        """Open Fetch Customer wizard (import by WooCommerce customer ID)."""
        self._check_woo_sync_access()
        self.ensure_one()
        default_instance = self._woo_resolve_instance() or self.env['kits.connector.instance']
        default_woo_id = False
        if default_instance:
            binding = self.env['kits.woo.customer.binding']._find(
                default_instance, odoo_record_id=self.id,
            )
            if binding:
                default_woo_id = binding.external_id
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Fetch Customer from WooCommerce'),
            'res_model': 'kits.woo.customer.fetch',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_source_partner_id': self.id,
                'default_instance_id': default_instance.id if default_instance else False,
                'default_woo_customer_id': default_woo_id,
            },
        }

    def action_woo_import_from_store(self):
        """Pull already-linked customers from WooCommerce into Odoo."""
        self._check_woo_sync_access()
        commercial = self.filtered(lambda p: not p.parent_id)
        if not commercial:
            raise UserError(self.env._(
                'Select top-level contacts (not invoice/delivery address children).'
            ))
        instance = commercial._woo_resolve_instance()
        if not instance:
            return commercial._woo_open_instance_picker('import')
        return commercial._woo_enqueue_import(instance)

    def _woo_open_instance_picker(self, operation):
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Choose WooCommerce Store'),
            'res_model': 'kits.woo.instance.picker',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_operation': operation,
                'default_partner_ids': [(6, 0, self.ids)],
            },
        }

    def _woo_enqueue_import(self, instance):
        instance._ensure_woo_ready()
        Binding = self.env['kits.woo.customer.binding']
        woo_ids = []
        missing = []
        for partner in self:
            binding = Binding._find(instance, odoo_record_id=partner.id)
            if not binding:
                missing.append(partner.display_name)
                continue
            woo_ids.append(int(binding.external_id))
        if missing and not woo_ids:
            raise UserError(self.env._(
                'None of the selected contacts are linked to %(store)s. '
                'Use Fetch Customer or import them from the store first.',
                store=instance.name,
            ))
        instance.action_create_sync_job(
            'woo_import_customers',
            name=self.env._(
                'WooCommerce: Import Customers (%(store)s) — %(count)s selected',
                store=instance.name,
                count=len(woo_ids),
            ),
            payload={'woo_ids': woo_ids},
        )
        message = self.env._(
            'Queued update of %(count)s linked customer(s) from %(store)s.',
            count=len(woo_ids),
            store=instance.name,
        )
        if missing:
            message = self.env._(
                '%(message)s Skipped %(skipped)s unlinked contact(s).',
                message=message,
                skipped=len(missing),
            )
        return instance._woo_notify(message)
