# -*- coding: utf-8 -*-
import logging
import traceback

from odoo import api, fields, models, tools
from odoo.exceptions import UserError

from ..services.exceptions import ConnectorError

_logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 20


class KitsConnectorSyncJob(models.Model):
    _name = 'kits.connector.sync.job'
    _description = 'Connector Sync Job'
    _inherit = ['mail.thread']
    _order = 'priority asc, scheduled_date asc, id asc'
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    instance_id = fields.Reference(
        selection='_kits_selection_instance_models',
        string='Store / Instance',
        required=True,
        index=True,
        tracking=True,
        help='Platform instance as model,id (e.g. kits.woo.instance,12).',
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        index=True,
        default=lambda self: self.env.company,
    )
    platform_type = fields.Char(
        string='Platform',
        index=True,
        help='Copied from the platform instance for menu filters and reporting.',
    )
    job_type = fields.Char(
        required=True,
        index=True,
        help='Technical job key used to dispatch handlers, e.g. import_orders.',
    )
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('pending', 'Pending'),
            ('running', 'Running'),
            ('done', 'Done'),
            ('failed', 'Failed'),
            ('cancelled', 'Cancelled'),
        ],
        default='draft',
        required=True,
        copy=False,
        index=True,
        tracking=True,
    )
    priority = fields.Integer(
        default=10,
        help='Lower values run first.',
    )
    scheduled_date = fields.Datetime(
        default=fields.Datetime.now,
        index=True,
        help='Job becomes eligible for processing at this datetime.',
    )
    started_at = fields.Datetime(readonly=True, copy=False)
    finished_at = fields.Datetime(readonly=True, copy=False)
    attempt_count = fields.Integer(default=0, readonly=True, copy=False)
    max_retries = fields.Integer(
        default=3,
        help='Maximum execution attempts before the job stays failed.',
    )
    payload = fields.Json(
        help='Job input payload consumed by the platform handler.',
    )
    result_summary = fields.Text(readonly=True, copy=False)
    error_message = fields.Text(readonly=True, copy=False)
    log_ids = fields.One2many('kits.connector.log', 'job_id', string='Logs')

    @api.model
    def _kits_selection_instance_models(self):
        selection = []
        if 'kits.woo.instance' in self.env:
            selection.append(('kits.woo.instance', 'WooCommerce Store'))
        if 'kits.shopify.instance' in self.env:
            selection.append(('kits.shopify.instance', 'Shopify Store'))
        return selection

    def _kits_get_instance(self):
        self.ensure_one()
        return self.env['kits.connector.instance.mixin']._kits_browse_instance_ref(
            self.instance_id,
        )

    @api.onchange('instance_id')
    def _onchange_instance_id(self):
        instance = self._kits_get_instance() if self.instance_id else self.env['res.company']
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

    @api.constrains('max_retries')
    def _check_max_retries(self):
        for job in self:
            if job.max_retries < 0:
                raise UserError(self.env._('Max retries cannot be negative.'))

    def action_enqueue(self):
        for job in self:
            if job.state in ('running', 'done'):
                raise UserError(self.env._(
                    'Job "%(name)s" cannot be enqueued from state %(state)s.',
                    name=job.name,
                    state=job.state,
                ))
            job.write({
                'state': 'pending',
                'error_message': False,
                'scheduled_date': job.scheduled_date or fields.Datetime.now(),
            })
        return True

    def action_cancel(self):
        for job in self:
            if job.state == 'running':
                raise UserError(self.env._(
                    'Running job "%(name)s" cannot be cancelled.',
                    name=job.name,
                ))
            if job.state == 'done':
                raise UserError(self.env._(
                    'Completed job "%(name)s" cannot be cancelled.',
                    name=job.name,
                ))
            job.state = 'cancelled'
        return True

    def action_reset_to_draft(self):
        for job in self:
            if job.state == 'running':
                raise UserError(self.env._(
                    'Running job "%(name)s" cannot be reset.',
                    name=job.name,
                ))
            job.write({
                'state': 'draft',
                'error_message': False,
                'result_summary': False,
                'started_at': False,
                'finished_at': False,
            })
        return True

    def action_run_now(self):
        for job in self:
            job._process_job()
        return True

    def _process_job(self):
        self.ensure_one()
        if self.state == 'cancelled':
            return False
        instance = self._kits_get_instance()
        if not instance or not instance.active:
            self.write({
                'state': 'failed',
                'error_message': self.env._('Connector instance is missing or archived.'),
                'finished_at': fields.Datetime.now(),
            })
            return False

        self.write({
            'state': 'running',
            'started_at': fields.Datetime.now(),
            'attempt_count': self.attempt_count + 1,
            'error_message': False,
        })
        if not tools.config['test_enable']:
            self.env.cr.commit()

        try:
            result = self._execute()
        except Exception as exc:  # noqa: BLE001
            return self._handle_failure(exc)
        return self._handle_success(result)

    def _execute(self):
        self.ensure_one()
        method_name = f'_run_{self.job_type}'
        handler = getattr(self, method_name, None)
        if not callable(handler):
            raise UserError(self.env._(
                'No sync handler implemented for job type "%(job_type)s". '
                'Platform modules must define %(method)s().',
                job_type=self.job_type,
                method=method_name,
            ))
        return handler()

    def _handle_success(self, result):
        self.ensure_one()
        summary = result if isinstance(result, str) else (result or self.env._('Done'))
        self.write({
            'state': 'done',
            'result_summary': summary,
            'finished_at': fields.Datetime.now(),
            'error_message': False,
        })
        instance = self._kits_get_instance()
        if instance:
            instance._kits_log_operation(
                operation=self.job_type,
                status='success',
                level='info',
                message=summary,
                job=self,
            )
        return True

    def _handle_failure(self, exc):
        self.ensure_one()
        instance = self._kits_get_instance()
        if isinstance(exc, ConnectorError) and instance:
            info = instance._kits_format_connector_error(exc)
            message = info['message']
            details = info.get('details') or traceback.format_exc()
        else:
            message = str(exc)
            details = traceback.format_exc()

        can_retry = self.attempt_count < self.max_retries
        new_state = 'pending' if can_retry else 'failed'
        self.write({
            'state': new_state,
            'error_message': message,
            'finished_at': fields.Datetime.now() if not can_retry else False,
            'scheduled_date': fields.Datetime.add(
                fields.Datetime.now(),
                minutes=min(self.attempt_count * 2, 30),
            ) if can_retry else self.scheduled_date,
        })
        if instance:
            instance._kits_log_operation(
                operation=self.job_type,
                status='failure',
                level='error',
                message=message,
                details=details,
                job=self,
            )
            if instance.state == 'connected':
                instance.write({
                    'state': 'error',
                    'last_error': message,
                })
        _logger.exception(
            'Connector sync job %s (%s) failed on attempt %s/%s',
            self.id,
            self.job_type,
            self.attempt_count,
            self.max_retries,
        )
        return False

    @api.model
    def _cron_process_pending_jobs(self, batch_size=DEFAULT_BATCH_SIZE):
        now = fields.Datetime.now()
        jobs = self.search(
            [
                ('state', '=', 'pending'),
                ('scheduled_date', '<=', now),
            ],
            order='priority asc, scheduled_date asc, id asc',
            limit=batch_size * 2,
        )
        processed = 0
        for job in jobs:
            if processed >= batch_size:
                break
            instance = job._kits_get_instance()
            if not instance or not instance.active:
                continue
            try:
                job._process_job()
                processed += 1
            except Exception:  # noqa: BLE001
                _logger.exception('Unexpected error while processing sync job %s', job.id)
                self.env.cr.rollback()
        return processed
