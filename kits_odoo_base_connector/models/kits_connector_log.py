# -*- coding: utf-8 -*-
from odoo import api, fields, models


class KitsConnectorLog(models.Model):
    _name = 'kits.connector.log'
    _description = 'Connector Operation Log'
    _order = 'create_date desc, id desc'
    _check_company_auto = True

    name = fields.Char(
        string='Operation',
        required=True,
        index=True,
        help='Technical operation name, e.g. test_connection or import_orders.',
    )
    instance_id = fields.Reference(
        selection='_kits_selection_instance_models',
        string='Store / Instance',
        required=True,
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        index=True,
        default=lambda self: self.env.company,
    )
    job_id = fields.Many2one(
        'kits.connector.sync.job',
        index=True,
        ondelete='set null',
    )
    platform_type = fields.Char(string='Platform', index=True)
    level = fields.Selection(
        selection=[
            ('debug', 'Debug'),
            ('info', 'Info'),
            ('warning', 'Warning'),
            ('error', 'Error'),
        ],
        default='info',
        required=True,
        index=True,
    )
    status = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('success', 'Success'),
            ('failure', 'Failure'),
        ],
        default='success',
        required=True,
        index=True,
    )
    message = fields.Char(required=True)
    details = fields.Text(
        help='Extended error details, stack traces, or diagnostic notes.',
    )
    request_data = fields.Text(
        help='Optional truncated request payload for diagnostics. Never store secrets.',
    )
    response_data = fields.Text(
        help='Optional truncated response payload for diagnostics. Never store secrets.',
    )

    @api.model
    def _kits_selection_instance_models(self):
        return self.env['kits.connector.sync.job']._kits_selection_instance_models()

    def _kits_get_instance(self):
        self.ensure_one()
        return self.env['kits.connector.instance.mixin']._kits_browse_instance_ref(
            self.instance_id,
        )

    @api.onchange('instance_id')
    def _onchange_instance_id(self):
        instance = self._kits_get_instance() if self.instance_id else False
        if instance:
            self.company_id = instance.company_id
            self.platform_type = getattr(instance, 'platform_type', False)

    @api.model_create_multi
    def create(self, vals_list):
        Mixin = self.env['kits.connector.instance.mixin']
        for vals in vals_list:
            ref = vals.get('instance_id')
            instance = Mixin._kits_browse_instance_ref(ref) if ref else Mixin.browse()
            if instance:
                vals['instance_id'] = instance._kits_instance_ref()
                vals.setdefault('company_id', instance.company_id.id)
                vals.setdefault('platform_type', instance.platform_type)
        return super().create(vals_list)
