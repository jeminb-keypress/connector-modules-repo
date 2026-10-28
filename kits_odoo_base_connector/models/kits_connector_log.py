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
    instance_id = fields.Many2one(
        'kits.connector.instance',
        required=True,
        index=True,
        ondelete='cascade',
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
    platform_type = fields.Selection(
        related='instance_id.platform_type',
        store=True,
        readonly=True,
    )
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

    @api.onchange('instance_id')
    def _onchange_instance_id(self):
        if self.instance_id:
            self.company_id = self.instance_id.company_id

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('instance_id') and not vals.get('company_id'):
                instance = self.env['kits.connector.instance'].browse(vals['instance_id'])
                vals['company_id'] = instance.company_id.id
        return super().create(vals_list)
