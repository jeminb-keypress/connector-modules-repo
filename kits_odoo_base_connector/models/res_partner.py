# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResPartner(models.Model):
    """Expose connector instance links on customers across all platforms."""

    _inherit = 'res.partner'

    kits_connected_instance_ids = fields.Many2many(
        'kits.connector.instance',
        string='Connected Instances',
        compute='_compute_kits_connector_info',
        help='Connector instances that have an active binding for this contact. '
             'Each instance keeps its own external customer ID and sync status.',
    )
    kits_binding_ids = fields.Many2many(
        'kits.connector.binding',
        string='Connector Bindings',
        compute='_compute_kits_connector_info',
        help='Per-instance external ID mappings and sync status for this contact.',
    )
    kits_binding_count = fields.Integer(
        string='Binding Count',
        compute='_compute_kits_connector_info',
    )

    def _compute_kits_connector_info(self):
        Binding = self.env['kits.connector.binding']
        for partner in self:
            if not partner.id:
                partner.kits_binding_ids = Binding
                partner.kits_connected_instance_ids = self.env['kits.connector.instance']
                partner.kits_binding_count = 0
                continue
            bindings = Binding.search([
                ('odoo_model', '=', 'res.partner'),
                ('odoo_record_id', '=', partner.id),
                ('active', '=', True),
            ])
            partner.kits_binding_ids = bindings
            partner.kits_connected_instance_ids = bindings.mapped('instance_id')
            partner.kits_binding_count = len(bindings)

    def action_kits_open_bindings(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'kits_odoo_base_connector.kits_connector_binding_action'
        )
        action['domain'] = [
            ('odoo_model', '=', 'res.partner'),
            ('odoo_record_id', '=', self.id),
        ]
        action['context'] = {
            'default_odoo_model': 'res.partner',
            'default_odoo_record_id': self.id,
            'search_default_filter_active': 1,
        }
        return action

    @api.model
    def _kits_partners_for_bindings(self, bindings):
        """Browse partners referenced by the given bindings (exists only)."""
        ids = [b.odoo_record_id for b in bindings if b.odoo_model == 'res.partner']
        return self.browse(ids).exists()
