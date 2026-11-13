# -*- coding: utf-8 -*-
"""Abstract connector instance mixin — Common Connector Library.

Platform modules define their own concrete instance models
(``kits.woo.instance``, ``kits.shopify.instance``, …) that inherit this mixin.
Base never owns a concrete store/instance model or connection UI.
"""

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..services.exceptions import ConnectorError
from ..services.http_client import DEFAULT_TIMEOUT, ConnectorHttpClient


class KitsConnectorInstanceMixin(models.AbstractModel):
    _name = 'kits.connector.instance.mixin'
    _description = 'Kits Connector Instance Mixin'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'kits.connector.api.mixin']
    _order = 'name, id'
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    platform_type = fields.Char(
        string='Platform',
        required=True,
        index=True,
        tracking=True,
        help='Technical platform key set by the concrete instance model '
             '(e.g. woocommerce, shopify).',
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
        help='Optional generic credential slot. Prefer platform-specific fields.',
    )
    api_secret = fields.Char(
        string='API Secret',
        copy=False,
        groups='kits_odoo_base_connector.group_kits_connector_manager',
        help='Optional generic secret slot. Prefer platform-specific fields.',
    )
    config_json = fields.Json(
        string='Extra Configuration',
        help='Optional JSON bag for non-secret connector settings.',
    )
    last_connection_test = fields.Datetime(readonly=True, copy=False)
    last_error = fields.Text(readonly=True, copy=False)
    notes = fields.Text()
    sync_job_count = fields.Integer(compute='_compute_kits_related_counts')
    log_count = fields.Integer(compute='_compute_kits_related_counts')
    binding_count = fields.Integer(compute='_compute_kits_related_counts')

    def _compute_kits_related_counts(self):
        Job = self.env['kits.connector.sync.job']
        Log = self.env['kits.connector.log']
        Binding = self.env['kits.connector.binding']
        for instance in self:
            ref = instance._kits_instance_ref()
            instance.sync_job_count = Job.search_count([('instance_id', '=', ref)])
            instance.log_count = Log.search_count([('instance_id', '=', ref)])
            instance.binding_count = Binding.search_count([('instance_id', '=', ref)])

    @api.constrains('timeout')
    def _check_timeout(self):
        for instance in self:
            if instance.timeout is not None and instance.timeout <= 0:
                raise UserError(self.env._('HTTP timeout must be a positive number of seconds.'))

    # -------------------------------------------------------------------------
    # Reference helpers (jobs / logs / generic bindings)
    # -------------------------------------------------------------------------

    def _kits_instance_ref(self):
        """Return the Reference string ``model,id`` for shared job/log/binding rows."""
        self.ensure_one()
        return f'{self._name},{self.id}'

    @api.model
    def _kits_browse_instance_ref(self, ref):
        """Browse an instance from a Reference string, recordset, or bare id."""
        if not ref:
            return self.browse()
        if isinstance(ref, models.BaseModel):
            return ref.exists() if hasattr(ref, 'exists') else ref
        if isinstance(ref, int):
            for model_name in ('kits.woo.instance', 'kits.shopify.instance'):
                if model_name in self.env:
                    record = self.env[model_name].browse(ref).exists()
                    if record:
                        return record
            return self.browse()
        text = str(ref).strip()
        if text.isdigit():
            return self._kits_browse_instance_ref(int(text))
        if ',' not in text:
            return self.browse()
        model_name, _, res_id = text.partition(',')
        if model_name not in self.env or not res_id.isdigit():
            return self.browse()
        return self.env[model_name].browse(int(res_id)).exists()

    @api.model
    def _kits_normalize_instance_ref(self, ref):
        """Return a ``model,id`` Reference string from any accepted instance value."""
        instance = self._kits_browse_instance_ref(ref)
        return instance._kits_instance_ref() if instance else False

    def unlink(self):
        """Remove shared jobs/logs/bindings that Reference this instance."""
        refs = [rec._kits_instance_ref() for rec in self]
        if refs:
            self.env['kits.connector.sync.job'].sudo().search([
                ('instance_id', 'in', refs),
            ]).unlink()
            self.env['kits.connector.log'].sudo().search([
                ('instance_id', 'in', refs),
            ]).unlink()
            self.env['kits.connector.binding'].sudo().search([
                ('instance_id', 'in', refs),
            ]).unlink()
        return super().unlink()

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
        """Platform hook for connection testing."""
        self.ensure_one()
        if not self.base_url:
            raise ConnectorError(self.env._('Base URL is required to test the connection.'))
        return True

    # -------------------------------------------------------------------------
    # Sync helpers
    # -------------------------------------------------------------------------

    def action_create_sync_job(self, job_type, *, name=False, payload=None,
                               scheduled_date=False, max_retries=3, priority=10,
                               enqueue=True):
        """Create a sync job linked to this platform instance via Reference."""
        self.ensure_one()
        vals = {
            'name': name or self.env._('Sync: %(job_type)s', job_type=job_type),
            'instance_id': self._kits_instance_ref(),
            'company_id': self.company_id.id,
            'platform_type': self.platform_type,
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
            'instance_id': self._kits_instance_ref(),
            'company_id': self.company_id.id,
            'platform_type': self.platform_type,
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
        ref = self._kits_instance_ref()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Sync Jobs'),
            'res_model': 'kits.connector.sync.job',
            'view_mode': 'list,form',
            'domain': [('instance_id', '=', ref)],
            'context': {
                'default_instance_id': ref,
                'default_company_id': self.company_id.id,
                'default_platform_type': self.platform_type,
            },
        }

    def action_view_logs(self):
        self.ensure_one()
        ref = self._kits_instance_ref()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Connector Logs'),
            'res_model': 'kits.connector.log',
            'view_mode': 'list,form',
            'domain': [('instance_id', '=', ref)],
            'context': {
                'default_instance_id': ref,
                'default_company_id': self.company_id.id,
                'default_platform_type': self.platform_type,
            },
        }

    def action_view_bindings(self):
        self.ensure_one()
        ref = self._kits_instance_ref()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('External Bindings'),
            'res_model': 'kits.connector.binding',
            'view_mode': 'list,form',
            'domain': [('instance_id', '=', ref)],
            'context': {
                'default_instance_id': ref,
                'default_company_id': self.company_id.id,
                'default_platform_type': self.platform_type,
            },
        }

    # -------------------------------------------------------------------------
    # Scheduler helpers
    # -------------------------------------------------------------------------

    def _kits_has_pending_job(self, job_type):
        """Return True when this instance already has a pending/running job of this type."""
        self.ensure_one()
        return bool(self.env['kits.connector.sync.job'].search_count([
            ('instance_id', '=', self._kits_instance_ref()),
            ('job_type', '=', job_type),
            ('state', 'in', ('pending', 'running')),
        ]))

    def _kits_enqueue_if_enabled(self, flag_field, job_type, *, name=False, payload=None):
        """Enqueue ``job_type`` when ``flag_field`` is set and no duplicate is pending."""
        self.ensure_one()
        if not getattr(self, flag_field, False):
            return self.env['kits.connector.sync.job']
        if self._kits_has_pending_job(job_type):
            return self.env['kits.connector.sync.job']
        return self.action_create_sync_job(
            job_type,
            name=name or self.env._('%(job)s (%(store)s)', job=job_type, store=self.name),
            payload=payload or {},
        )
