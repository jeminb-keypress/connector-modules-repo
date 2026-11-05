# -*- coding: utf-8 -*-
"""Read-only WooCommerce connector dashboard data (no sync side effects)."""

from datetime import datetime, time, timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.fields import Datetime as DatetimeField
from odoo.tools.misc import format_datetime

_PRODUCT_JOB_TYPES = frozenset({
    'woo_import_products',
    'woo_export_products',
    'woo_webhook_product',
    'woo_import_categories',
    'woo_export_categories',
    'woo_import_tags',
    'woo_export_tags',
    'woo_import_attributes',
})
_CUSTOMER_JOB_TYPES = frozenset({
    'woo_import_customers',
    'woo_webhook_customer',
})
_ORDER_JOB_TYPES = frozenset({
    'woo_import_orders',
    'woo_webhook_order',
    'woo_export_shipments',
})
_PENDING_STATES = ('draft', 'pending', 'running')
_DONE_STATE = 'done'
_FAILED_STATE = 'failed'


class KitsConnectorInstanceDashboard(models.Model):
    _inherit = 'kits.connector.instance'

    @api.model
    def get_woo_dashboard_data(self, date_range='last_30', date_from=False, date_to=False):
        """Return JSON-serializable dashboard payload for the OWL client action."""
        if not self.env.user.has_group('kits_odoo_base_connector.group_kits_connector_user'):
            raise AccessError(self.env._(
                'You need Connector User access to open the WooCommerce dashboard.'
            ))
        start, end, prev_start, prev_end = self._woo_dashboard_period(
            date_range, date_from, date_to,
        )
        stores = self._woo_dashboard_stores()
        store_ids = stores.ids

        customers_total = self._woo_dashboard_customer_count(store_ids)
        customers_period = self._woo_dashboard_customer_created(store_ids, start, end)
        customers_prev = self._woo_dashboard_customer_created(store_ids, prev_start, prev_end)

        products_total = self._woo_dashboard_product_count(store_ids)
        products_period = self._woo_dashboard_product_created(store_ids, start, end)
        products_prev = self._woo_dashboard_product_created(store_ids, prev_start, prev_end)

        orders_period = self._woo_dashboard_order_count(store_ids, start, end)
        orders_prev = self._woo_dashboard_order_count(store_ids, prev_start, prev_end)

        job_stats = self._woo_dashboard_job_stats(store_ids, start, end)
        sync_series = self._woo_dashboard_sync_series(store_ids, start, end)
        store_rows = self._woo_dashboard_store_rows(stores, start, end)
        top_products = self._woo_dashboard_top_products(store_ids, start, end)
        pending_jobs = self._woo_dashboard_pending_jobs(store_ids)

        return {
            'date_range': date_range,
            'date_from': fields.Datetime.to_string(start) if start else False,
            'date_to': fields.Datetime.to_string(end) if end else False,
            'kpis': {
                'connected_stores': {
                    'value': len(stores),
                    'change_label': False,
                    'change_positive': None,
                },
                'customers': {
                    'value': customers_total,
                    'change_label': self._woo_dashboard_change_label(
                        customers_period, customers_prev,
                    ),
                    'change_positive': self._woo_dashboard_change_positive(
                        customers_period, customers_prev,
                    ),
                },
                'products': {
                    'value': products_total,
                    'change_label': self._woo_dashboard_change_label(
                        products_period, products_prev,
                    ),
                    'change_positive': self._woo_dashboard_change_positive(
                        products_period, products_prev,
                    ),
                },
                'orders': {
                    'value': orders_period,
                    'change_label': self._woo_dashboard_change_label(
                        orders_period, orders_prev,
                    ),
                    'change_positive': self._woo_dashboard_change_positive(
                        orders_period, orders_prev,
                    ),
                },
                'success_rate': {
                    'value': job_stats['success_rate'],
                    'change_label': False,
                    'change_positive': None,
                    'done': job_stats['done'],
                    'failed': job_stats['failed'],
                    'pending': job_stats['pending'],
                },
            },
            'sync_overview': sync_series,
            'stores': store_rows,
            'sync_status': {
                'done': job_stats['done'],
                'failed': job_stats['failed'],
                'pending': job_stats['pending'],
                'success_rate': job_stats['success_rate'],
            },
            'top_products': top_products,
            'pending_jobs': pending_jobs,
            'actions': self._woo_dashboard_action_refs(),
        }

    @api.model
    def action_woo_dashboard_quick_action(self, action_key, instance_id=False):
        """Return an existing act_window / notify for dashboard quick actions."""
        if not self.env.user.has_group('kits_odoo_base_connector.group_kits_connector_user'):
            raise AccessError(self.env._(
                'You need Connector User access for WooCommerce dashboard actions.'
            ))
        stores = self._woo_dashboard_stores()
        if action_key == 'manage_stores':
            return self.env.ref(
                'kits_odoo_woocommerce_connector.kits_woo_instance_action'
            ).sudo().read()[0]
        if action_key == 'view_logs':
            return self.env.ref(
                'kits_odoo_woocommerce_connector.kits_woo_log_action'
            ).sudo().read()[0]
        if action_key == 'view_jobs':
            return self.env.ref(
                'kits_odoo_woocommerce_connector.kits_woo_sync_job_action'
            ).sudo().read()[0]
        if action_key == 'view_products':
            return self.env.ref(
                'kits_odoo_woocommerce_connector.kits_woo_product_template_action'
            ).sudo().read()[0]
        if action_key == 'sync_now':
            return self._woo_dashboard_open_sync_wizard(stores, instance_id)
        if action_key == 'import_products':
            if not self.env.user.has_group(
                'kits_odoo_base_connector.group_kits_connector_manager'
            ):
                raise UserError(self.env._(
                    'Only Connector Administrators can import products.'
                ))
            return self._woo_dashboard_open_sync_wizard(
                stores, instance_id, default_operation='import_products',
            )
        if action_key == 'export_products':
            if not self.env.user.has_group(
                'kits_odoo_base_connector.group_kits_connector_manager'
            ):
                raise UserError(self.env._(
                    'Only Connector Administrators can export products.'
                ))
            target = stores.browse(instance_id) if instance_id else stores
            target = target.filtered(lambda s: s.id in stores.ids)
            if len(target) == 1:
                return target.action_woo_export_products()
            return self._woo_dashboard_open_sync_wizard(stores, instance_id)
        raise UserError(self.env._('Unknown dashboard action: %s', action_key))

    # -------------------------------------------------------------------------
    # Period helpers
    # -------------------------------------------------------------------------

    @api.model
    def _woo_dashboard_period(self, date_range, date_from, date_to):
        now = fields.Datetime.now()
        today = fields.Date.context_today(self)
        day_start = datetime.combine(today, time.min)
        # Datetime fields are UTC-naive in Odoo storage; use server now for ranges.
        if date_range == 'today':
            start = DatetimeField.to_datetime(day_start)
            end = now
            length = timedelta(days=1)
        elif date_range == 'last_7':
            start = now - timedelta(days=7)
            end = now
            length = timedelta(days=7)
        elif date_range == 'last_90':
            start = now - timedelta(days=90)
            end = now
            length = timedelta(days=90)
        elif date_range == 'custom':
            if not date_from or not date_to:
                raise UserError(self.env._(
                    'Choose both From and To dates for a custom range.'
                ))
            start = DatetimeField.to_datetime(date_from)
            end = DatetimeField.to_datetime(date_to)
            if end < start:
                raise UserError(self.env._('Date To must be on or after Date From.'))
            # Inclusive end-of-day if a date-only string was passed
            if isinstance(date_to, str) and ' ' not in date_to.strip():
                end = end + timedelta(days=1) - timedelta(seconds=1)
            length = end - start or timedelta(days=1)
        else:  # last_30 default
            start = now - timedelta(days=30)
            end = now
            length = timedelta(days=30)
        prev_end = start - timedelta(seconds=1)
        prev_start = prev_end - length
        return start, end, prev_start, prev_end

    @api.model
    def _woo_dashboard_stores(self):
        return self.search([
            ('platform_type', '=', 'woocommerce'),
            ('active', '=', True),
        ])

    @api.model
    def _woo_dashboard_change_label(self, current, previous):
        if previous in (None, False) and not current:
            return False
        if previous in (0, None, False):
            if current:
                return self.env._('+%(count)s new', count=current)
            return False
        delta_pct = ((current - previous) / float(previous)) * 100.0
        sign = '+' if delta_pct >= 0 else ''
        return f'{sign}{delta_pct:.0f}%'

    @api.model
    def _woo_dashboard_change_positive(self, current, previous):
        if previous in (None, False) and not current:
            return None
        if previous in (0, None, False):
            return True if current else None
        return current >= previous

    # -------------------------------------------------------------------------
    # Metric queries
    # -------------------------------------------------------------------------

    @api.model
    def _woo_dashboard_customer_count(self, store_ids):
        if not store_ids:
            return 0
        return self.env['kits.woo.customer.binding'].search_count([
            ('instance_id', 'in', store_ids),
            ('active', '=', True),
        ])

    @api.model
    def _woo_dashboard_customer_created(self, store_ids, start, end):
        if not store_ids:
            return 0
        return self.env['kits.woo.customer.binding'].search_count([
            ('instance_id', 'in', store_ids),
            ('active', '=', True),
            ('create_date', '>=', start),
            ('create_date', '<=', end),
        ])

    @api.model
    def _woo_dashboard_product_count(self, store_ids):
        """Distinct Odoo templates bound to any Woo store (no multi-store double count)."""
        if not store_ids:
            return 0
        self.env.cr.execute("""
            SELECT COUNT(DISTINCT product_tmpl_id)
              FROM kits_woo_product_binding
             WHERE instance_id = ANY(%s)
               AND active IS TRUE
        """, [list(store_ids)])
        return self.env.cr.fetchone()[0] or 0

    @api.model
    def _woo_dashboard_product_created(self, store_ids, start, end):
        if not store_ids:
            return 0
        self.env.cr.execute("""
            SELECT COUNT(DISTINCT product_tmpl_id)
              FROM kits_woo_product_binding
             WHERE instance_id = ANY(%s)
               AND active IS TRUE
               AND create_date >= %s
               AND create_date <= %s
        """, [list(store_ids), start, end])
        return self.env.cr.fetchone()[0] or 0

    @api.model
    def _woo_dashboard_order_count(self, store_ids, start, end):
        if not store_ids:
            return 0
        return self.env['sale.order'].search_count([
            ('woo_instance_id', 'in', store_ids),
            ('woo_order_id', '!=', False),
            ('date_order', '>=', start),
            ('date_order', '<=', end),
        ])

    @api.model
    def _woo_dashboard_job_domain(self, store_ids):
        return [
            ('platform_type', '=', 'woocommerce'),
            ('instance_id', 'in', store_ids),
        ]

    @api.model
    def _woo_dashboard_job_stats(self, store_ids, start, end):
        Job = self.env['kits.connector.sync.job']
        if not store_ids:
            return {'done': 0, 'failed': 0, 'pending': 0, 'success_rate': 0.0}
        base = self._woo_dashboard_job_domain(store_ids)
        done = Job.search_count(base + [
            ('state', '=', _DONE_STATE),
            ('finished_at', '>=', start),
            ('finished_at', '<=', end),
        ])
        failed = Job.search_count(base + [
            ('state', '=', _FAILED_STATE),
            ('finished_at', '>=', start),
            ('finished_at', '<=', end),
        ]) + Job.search_count(base + [
            ('state', '=', _FAILED_STATE),
            ('finished_at', '=', False),
            ('write_date', '>=', start),
            ('write_date', '<=', end),
        ])
        pending = Job.search_count(base + [('state', 'in', list(_PENDING_STATES))])
        terminal = done + failed
        rate = round((done / float(terminal)) * 100.0, 1) if terminal else 0.0
        return {
            'done': done,
            'failed': failed,
            'pending': pending,
            'success_rate': rate,
        }

    @api.model
    def _woo_dashboard_sync_series(self, store_ids, start, end):
        """Daily counts of finished jobs by products/customers/orders series."""
        labels = []
        cursor = start.date() if hasattr(start, 'date') else start
        end_date = end.date() if hasattr(end, 'date') else end
        day = cursor
        while day <= end_date:
            labels.append(day.isoformat())
            day += timedelta(days=1)

        series = {
            'labels': labels,
            'products': [0] * len(labels),
            'customers': [0] * len(labels),
            'orders': [0] * len(labels),
        }
        if not store_ids or not labels:
            return series

        index = {label: idx for idx, label in enumerate(labels)}
        Job = self.env['kits.connector.sync.job']
        jobs = Job.search(self._woo_dashboard_job_domain(store_ids) + [
            ('state', '=', _DONE_STATE),
            ('finished_at', '>=', start),
            ('finished_at', '<=', end),
        ])
        for job in jobs:
            stamp = job.finished_at or job.write_date
            if not stamp:
                continue
            key = stamp.date().isoformat()
            idx = index.get(key)
            if idx is None:
                continue
            if job.job_type in _PRODUCT_JOB_TYPES:
                series['products'][idx] += 1
            elif job.job_type in _CUSTOMER_JOB_TYPES:
                series['customers'][idx] += 1
            elif job.job_type in _ORDER_JOB_TYPES:
                series['orders'][idx] += 1
        return series

    @api.model
    def _woo_dashboard_store_rows(self, stores, start, end):
        rows = []
        Job = self.env['kits.connector.sync.job']
        Binding = self.env['kits.woo.product.binding']
        Order = self.env['sale.order']
        for store in stores:
            last_candidates = [
                store.woo_last_product_sync,
                store.woo_last_customer_sync,
                store.woo_last_order_sync,
                store.woo_last_stock_sync,
                store.write_date,
            ]
            last_sync = max((d for d in last_candidates if d), default=False)
            done = Job.search_count([
                ('instance_id', '=', store.id),
                ('state', '=', _DONE_STATE),
                ('finished_at', '>=', start),
                ('finished_at', '<=', end),
            ])
            failed = Job.search_count([
                ('instance_id', '=', store.id),
                ('state', '=', _FAILED_STATE),
                ('finished_at', '>=', start),
                ('finished_at', '<=', end),
            ]) + Job.search_count([
                ('instance_id', '=', store.id),
                ('state', '=', _FAILED_STATE),
                ('finished_at', '=', False),
                ('write_date', '>=', start),
                ('write_date', '<=', end),
            ])
            terminal = done + failed
            rate = round((done / float(terminal)) * 100.0, 1) if terminal else 0.0
            state_labels = {
                'draft': self.env._('Draft'),
                'connected': self.env._('Connected'),
                'error': self.env._('Error'),
                'disconnected': self.env._('Disconnected'),
            }
            rows.append({
                'id': store.id,
                'name': store.name,
                'url': store.base_url or '',
                'state': store.state,
                'state_label': state_labels.get(store.state, store.state),
                'last_sync': fields.Datetime.to_string(last_sync) if last_sync else False,
                'last_sync_display': format_datetime(self.env, last_sync) if last_sync else self.env._('Never'),
                'product_count': Binding.search_count([
                    ('instance_id', '=', store.id),
                    ('active', '=', True),
                ]),
                'order_count': Order.search_count([
                    ('woo_instance_id', '=', store.id),
                    ('woo_order_id', '!=', False),
                    ('date_order', '>=', start),
                    ('date_order', '<=', end),
                ]),
                'success_rate': rate,
            })
        return rows

    @api.model
    def _woo_dashboard_top_products(self, store_ids, start, end):
        if not store_ids:
            return []
        self.env.cr.execute("""
            SELECT pt.id,
                   pt.name,
                   COALESCE(SUM(sol.product_uom_qty), 0) AS qty
              FROM sale_order_line sol
              JOIN sale_order so ON so.id = sol.order_id
              JOIN product_product pp ON pp.id = sol.product_id
              JOIN product_template pt ON pt.id = pp.product_tmpl_id
             WHERE so.woo_instance_id = ANY(%s)
               AND so.woo_order_id IS NOT NULL
               AND so.date_order >= %s
               AND so.date_order <= %s
               AND sol.display_type IS NULL
             GROUP BY pt.id, pt.name
             ORDER BY qty DESC
             LIMIT 5
        """, [list(store_ids), start, end])
        rows = self.env.cr.fetchall()
        if not rows:
            return []
        templates = self.env['product.template'].browse([r[0] for r in rows])
        by_id = {t.id: t for t in templates}
        result = []
        for tmpl_id, name, qty in rows:
            tmpl = by_id.get(tmpl_id)
            # name may be JSON/translated dict depending on Odoo version
            display = name
            if isinstance(name, dict):
                display = name.get(self.env.lang) or name.get('en_US') or next(iter(name.values()), '')
            image = False
            if tmpl and tmpl.image_128:
                image = f'/web/image/product.template/{tmpl.id}/image_128'
            result.append({
                'id': tmpl_id,
                'name': display,
                'qty': int(qty),
                'image_url': image,
            })
        return result

    @api.model
    def _woo_dashboard_pending_jobs(self, store_ids, limit=8):
        if not store_ids:
            return []
        jobs = self.env['kits.connector.sync.job'].search(
            self._woo_dashboard_job_domain(store_ids) + [
                ('state', 'in', list(_PENDING_STATES)),
            ],
            order='scheduled_date asc, id asc',
            limit=limit,
        )
        rows = []
        for job in jobs:
            rows.append({
                'id': job.id,
                'job_type': job.job_type,
                'name': job.name,
                'records': self._woo_dashboard_job_record_hint(job),
                'scheduled_at': fields.Datetime.to_string(job.scheduled_date) if job.scheduled_date else False,
                'scheduled_display': format_datetime(self.env, job.scheduled_date) if job.scheduled_date else '',
                'state': job.state,
                'state_label': {
                    'draft': self.env._('Draft'),
                    'pending': self.env._('Pending'),
                    'running': self.env._('Running'),
                }.get(job.state, job.state),
            })
        return rows

    @api.model
    def _woo_dashboard_job_record_hint(self, job):
        payload = job.payload or {}
        for key in ('product_ids', 'order_ids', 'customer_ids', 'ids', 'woo_ids'):
            value = payload.get(key)
            if isinstance(value, (list, tuple)):
                return len(value)
            if value:
                return 1
        return False

    @api.model
    def _woo_dashboard_action_refs(self):
        return {
            'stores': 'kits_odoo_woocommerce_connector.kits_woo_instance_action',
            'jobs': 'kits_odoo_woocommerce_connector.kits_woo_sync_job_action',
            'logs': 'kits_odoo_woocommerce_connector.kits_woo_log_action',
            'products': 'kits_odoo_woocommerce_connector.kits_woo_product_template_action',
            'sync_wizard': 'kits_odoo_woocommerce_connector.kits_woo_sync_wizard_action',
        }

    @api.model
    def _woo_dashboard_open_sync_wizard(self, stores, instance_id=False, default_operation=False):
        context = {}
        if default_operation:
            context['default_operation'] = default_operation
        target = False
        if instance_id:
            target = stores.filtered(lambda s: s.id == instance_id)[:1]
        elif len(stores) == 1:
            target = stores
        if target:
            context['default_instance_id'] = target.id
        action = self.env.ref(
            'kits_odoo_woocommerce_connector.kits_woo_sync_wizard_action'
        ).sudo().read()[0]
        action['context'] = {
            **(action.get('context') or {}),
            **context,
        }
        return action
