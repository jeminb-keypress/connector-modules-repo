# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError

from ..services.exceptions import ConnectorError
from ..services.http_client import DEFAULT_TIMEOUT, ConnectorHttpClient


class KitsConnectorInstance(models.Model):
    _name = 'kits.connector.instance'
    _description = 'Connector Instance'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'kits.connector.api.mixin']
    _order = 'name, id'
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    platform_type = fields.Selection(
        selection=[('generic', 'Generic')],
        string='Platform',
        required=True,
        default='generic',
        tracking=True,
        help='Technical platform key. Platform modules extend this via selection_add.',
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
        tracking=True,
    )
    active = fields.Boolean(default=True, tracking=True)
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('connected', 'Connected'),
            ('error', 'Error'),
            ('disconnected', 'Disconnected'),
        ],
        default='draft',
        required=True,
        copy=False,
        tracking=True,
        help='Connection lifecycle state. Updated by connection tests and sync operations.',
    )
    base_url = fields.Char(
        string='Base URL',
        help='API root URL used by the HTTP client. Platform modules may ignore this '
             'when endpoints are derived differently.',
    )
    timeout = fields.Integer(
        string='HTTP Timeout (seconds)',
        default=DEFAULT_TIMEOUT,
        help='Default timeout applied to outbound HTTP requests.',
    )
    api_key = fields.Char(
        string='API Key',
        copy=False,
        groups='kits_odoo_base_connector.group_kits_connector_manager',
        help='Generic credential slot. Prefer platform-specific fields in dependent modules.',
    )
    api_secret = fields.Char(
        string='API Secret',
        copy=False,
        groups='kits_odoo_base_connector.group_kits_connector_manager',
        help='Generic secret slot. Prefer platform-specific fields in dependent modules.',
    )
    config_json = fields.Json(
        string='Extra Configuration',
        help='Optional JSON bag for non-secret connector settings.',
    )
    last_connection_test = fields.Datetime(readonly=True, copy=False)
    last_error = fields.Text(readonly=True, copy=False)
    sync_job_ids = fields.One2many('kits.connector.sync.job', 'instance_id', string='Sync Jobs')
    log_ids = fields.One2many('kits.connector.log', 'instance_id', string='Logs')
    binding_ids = fields.One2many('kits.connector.binding', 'instance_id', string='Bindings')
    sync_job_count = fields.Integer(compute='_compute_counts')
    log_count = fields.Integer(compute='_compute_counts')
    binding_count = fields.Integer(compute='_compute_counts')
    notes = fields.Text()

    _name_company_uniq = models.Constraint(
        'UNIQUE(name, company_id)',
        'A connector instance with this name already exists for the company.',
    )

    @api.depends('sync_job_ids', 'log_ids', 'binding_ids')
    def _compute_counts(self):
        job_data = self.env['kits.connector.sync.job']._read_group(
            [('instance_id', 'in', self.ids)],
            ['instance_id'],
            ['__count'],
        )
        log_data = self.env['kits.connector.log']._read_group(
            [('instance_id', 'in', self.ids)],
            ['instance_id'],
            ['__count'],
        )
        binding_data = self.env['kits.connector.binding']._read_group(
            [('instance_id', 'in', self.ids)],
            ['instance_id'],
            ['__count'],
        )
        job_map = {instance.id: count for instance, count in job_data}
        log_map = {instance.id: count for instance, count in log_data}
        binding_map = {instance.id: count for instance, count in binding_data}
        for instance in self:
            instance.sync_job_count = job_map.get(instance.id, 0)
            instance.log_count = log_map.get(instance.id, 0)
            instance.binding_count = binding_map.get(instance.id, 0)

    @api.constrains('timeout')
    def _check_timeout(self):
        for instance in self:
            if instance.timeout is not None and instance.timeout <= 0:
                raise UserError(self.env._('HTTP timeout must be a positive number of seconds.'))

    # -------------------------------------------------------------------------
    # Connection management
    # -------------------------------------------------------------------------

    def action_test_connection(self):
        """Run the platform connection test and update connection state."""
        for instance in self:
            try:
                instance._kits_test_connection()
            except ConnectorError as exc:
                info = instance._kits_format_connector_error(exc)
                instance.write({
                    'state': 'error',
                    'last_connection_test': fields.Datetime.now(),
                    'last_error': info['message'],
                })
                instance._kits_log_operation(
                    operation='test_connection',
                    status='failure',
                    level='error',
                    message=info['message'],
                    details=info.get('details'),
                )
                raise UserError(info['message']) from exc
            except Exception as exc:  # noqa: BLE001
                instance.write({
                    'state': 'error',
                    'last_connection_test': fields.Datetime.now(),
                    'last_error': str(exc),
                })
                instance._kits_log_operation(
                    operation='test_connection',
                    status='failure',
                    level='error',
                    message=str(exc),
                )
                raise UserError(str(exc)) from exc
            else:
                instance.write({
                    'state': 'connected',
                    'last_connection_test': fields.Datetime.now(),
                    'last_error': False,
                })
                instance._kits_log_operation(
                    operation='test_connection',
                    status='success',
                    level='info',
                    message=self.env._('Connection test succeeded.'),
                )
        return True

    def action_set_disconnected(self):
        self.write({'state': 'disconnected'})
        return True

    def _kits_test_connection(self):
        """Platform hook for connection testing.

        The generic implementation only validates that a base URL is configured.
        Platform modules must override this with a real lightweight API call.
        """
        self.ensure_one()
        if not self.base_url:
            raise ConnectorError(self.env._('Base URL is required to test the connection.'))
        # Generic connector does not call remote APIs. Dependent modules override.
        return True

    # -------------------------------------------------------------------------
    # Sync helpers
    # -------------------------------------------------------------------------

    def action_create_sync_job(self, job_type, *, name=False, payload=None,
                               scheduled_date=False, max_retries=3, priority=10,
                               enqueue=True):
        """Create a sync job for this instance.

        Returns the created ``kits.connector.sync.job`` recordset (one record).
        """
        self.ensure_one()
        vals = {
            'name': name or self.env._('Sync: %(job_type)s', job_type=job_type),
            'instance_id': self.id,
            'company_id': self.company_id.id,
            'job_type': job_type,
            'payload': payload or {},
            'scheduled_date': scheduled_date or fields.Datetime.now(),
            'max_retries': max_retries,
            'priority': priority,
            'state': 'draft',
        }
        job = self.env['kits.connector.sync.job'].create(vals)
        if enqueue:
            job.action_enqueue()
        return job

    def _kits_log_operation(self, operation, status='success', level='info',
                            message='', details=False, job=None,
                            request_data=False, response_data=False):
        """Create an operation log linked to this instance."""
        self.ensure_one()
        return self.env['kits.connector.log'].sudo().create({
            'name': operation,
            'instance_id': self.id,
            'company_id': self.company_id.id,
            'job_id': job.id if job else False,
            'level': level,
            'status': status,
            'message': message or operation,
            'details': details or False,
            'request_data': (
                ConnectorHttpClient.truncate_for_log(request_data) if request_data else False
            ),
            'response_data': (
                ConnectorHttpClient.truncate_for_log(response_data) if response_data else False
            ),
        })

    def action_view_sync_jobs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Sync Jobs'),
            'res_model': 'kits.connector.sync.job',
            'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.id)],
            'context': {
                'default_instance_id': self.id,
                'default_company_id': self.company_id.id,
            },
        }

    def action_view_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Connector Logs'),
            'res_model': 'kits.connector.log',
            'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.id)],
            'context': {
                'default_instance_id': self.id,
                'default_company_id': self.company_id.id,
            },
        }

    def action_view_bindings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('External Bindings'),
            'res_model': 'kits.connector.binding',
            'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.id)],
            'context': {
                'default_instance_id': self.id,
                'default_company_id': self.company_id.id,
            },
        }
