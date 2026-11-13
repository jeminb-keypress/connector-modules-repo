# -*- coding: utf-8 -*-
"""Per-topic WooCommerce webhook registration records."""

import logging
import secrets

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.addons.kits_odoo_base_connector.services.exceptions import ConnectorError

_logger = logging.getLogger(__name__)

WEBHOOK_TOPICS = [
    ('product.created', 'Product Created'),
    ('product.updated', 'Product Updated'),
    ('product.deleted', 'Product Deleted'),
    ('customer.created', 'Customer Created'),
    ('customer.updated', 'Customer Updated'),
    ('customer.deleted', 'Customer Deleted'),
    ('order.created', 'Order Created'),
    ('order.updated', 'Order Updated'),
    ('order.deleted', 'Order Deleted'),
    ('coupon.created', 'Coupon Created'),
    ('coupon.updated', 'Coupon Updated'),
    ('coupon.deleted', 'Coupon Deleted'),
]

WEBHOOK_STATUSES = [
    ('active', 'Active'),
    ('paused', 'Paused'),
    ('disabled', 'Disabled'),
]


class KitsWooWebhook(models.Model):
    _name = 'kits.woo.webhook'
    _description = 'WooCommerce Webhook'
    _order = 'instance_id, topic'
    _check_company_auto = True
    _rec_name = 'name'

    name = fields.Char(required=True, copy=False)
    instance_id = fields.Many2one(
        'kits.woo.instance',
        string='WooCommerce Store',
        required=True,
        index=True,
        ondelete='cascade',
        domain="[('platform_type', '=', 'woocommerce')]",
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='instance_id.company_id',
        store=True,
        index=True,
        readonly=True,
        required=True,
        precompute=True,
    )
    topic = fields.Selection(
        selection=WEBHOOK_TOPICS,
        required=True,
        index=True,
        copy=False,
    )
    webhook_id = fields.Char(
        string='Remote Webhook ID',
        copy=False,
        index=True,
        help='WooCommerce webhook id. Empty until remote registration succeeds.',
    )
    status = fields.Selection(
        selection=WEBHOOK_STATUSES,
        default='paused',
        required=True,
        copy=False,
        index=True,
        help='Last known WooCommerce remote status (not the store switch).',
    )
    delivery_url = fields.Char(required=True, copy=False)
    secret = fields.Char(
        copy=False,
        groups='kits_odoo_base_connector.group_kits_connector_manager',
        help='Per-webhook HMAC secret. Visible only to connector managers.',
    )
    last_delivery_id = fields.Char(
        string='Last Delivery ID',
        copy=False,
        help='Most recent accepted WooCommerce delivery id (idempotency).',
    )
    last_delivery_date = fields.Datetime(copy=False)
    last_error = fields.Text(copy=False)
    active = fields.Boolean(default=True)

    _instance_topic_uniq = models.Constraint(
        'UNIQUE(instance_id, topic)',
        'Only one webhook is allowed per store and topic.',
    )

    @api.model
    def _woo_generate_secret(self):
        return secrets.token_urlsafe(32)

    @api.model
    def _woo_normalize_remote_status(self, remote_status):
        status = (remote_status or '').strip().lower()
        if status in ('active', 'paused', 'disabled'):
            return status
        return 'disabled'

    @api.model
    def _woo_resource_for_topic(self, topic):
        prefix = (topic or '').split('.', 1)[0]
        return {
            'product': 'products',
            'customer': 'customers',
            'order': 'orders',
            'coupon': 'coupons',
        }.get(prefix)

    def _woo_ensure_secret(self):
        self.ensure_one()
        if not self.secret:
            self.sudo().secret = self._woo_generate_secret()
        return self.secret

    def _woo_assert_instance_ready(self):
        self.ensure_one()
        instance = self.instance_id
        if not instance:
            raise UserError(self.env._('Webhook has no store.'))
        instance._ensure_woo_ready()
        if not instance._woo_get_public_base_url():
            raise UserError(self.env._(
                'Odoo has no public base URL. Set Public Webhook URL on the '
                'store, or configure web.base.url / get_base_url() first.'
            ))

    def _woo_is_name_topic_locked(self):
        self.ensure_one()
        return bool(self.webhook_id)

    def write(self, vals):
        vals = dict(vals)
        for record in self:
            if record.webhook_id:
                if 'name' in vals and vals['name'] != record.name:
                    raise UserError(self.env._(
                        'Name cannot be changed after the webhook is registered remotely.'
                    ))
                if 'topic' in vals and vals['topic'] != record.topic:
                    raise UserError(self.env._(
                        'Topic cannot be changed after the webhook is registered remotely.'
                    ))
        return super().write(vals)

    def action_activate(self):
        for record in self:
            record._woo_set_remote_status('active')
        return True

    def action_stop(self):
        for record in self:
            record._woo_set_remote_status('paused')
        return True

    def action_toggle_status(self):
        for record in self:
            if record.status == 'active':
                record._woo_set_remote_status('paused')
            else:
                record._woo_set_remote_status('active')
        return True

    def action_delete_webhook(self):
        for record in self:
            record._woo_delete_remote_and_local()
        return True

    def _woo_rebuild_delivery_url(self):
        """PUT a new delivery_url onto the existing remote webhook.

        Preserves remote id, secret, and topic. Returns ``updated`` or
        ``unchanged``. Raises on failure without changing local delivery_url.
        """
        self.ensure_one()
        instance = self.instance_id
        new_url = instance._get_webhook_delivery_url(self.topic)
        if (self.delivery_url or '').rstrip('/') == new_url.rstrip('/'):
            return 'unchanged'
        if not self.webhook_id:
            raise UserError(self.env._(
                'Webhook %(topic)s has no remote id. Refresh or re-enable the '
                'resource switch before rebuilding its delivery URL.',
                topic=self.topic,
            ))

        secret = self._woo_ensure_secret()
        payload = {
            'name': self.name,
            'topic': self.topic,
            'delivery_url': new_url,
            'secret': secret,
            'status': self.status if self.status in ('active', 'paused') else 'paused',
        }
        api = instance._woo_api()
        try:
            remote = api.update_webhook(int(self.webhook_id), payload)
        except ConnectorError as exc:
            if getattr(exc, 'status_code', None) == 404:
                self.sudo().last_error = self.env._(
                    'Remote webhook %(rid)s no longer exists. Refresh or '
                    're-enable registration instead of rebuild.',
                    rid=self.webhook_id,
                )
                raise UserError(self.last_error) from exc
            self.sudo().last_error = str(exc)
            raise UserError(self.env._(
                'Could not update delivery URL for %(topic)s: %(err)s',
                topic=self.topic,
                err=exc,
            )) from exc
        except Exception as exc:  # noqa: BLE001
            self.sudo().last_error = str(exc)
            raise UserError(self.env._(
                'Could not update delivery URL for %(topic)s: %(err)s',
                topic=self.topic,
                err=exc,
            )) from exc

        remote_url = (remote.get('delivery_url') or new_url).rstrip('/')
        status = self._woo_normalize_remote_status(
            remote.get('status') or self.status,
        )
        self.sudo().write({
            'delivery_url': remote_url,
            'status': status,
            'last_error': False,
            'webhook_id': str(int(remote.get('id') or self.webhook_id)),
        })
        instance._kits_log_operation(
            operation='woo_webhook_rebuild_delivery_url',
            status='success',
            message=self.env._(
                'Updated delivery URL for %(topic)s (remote %(rid)s) on %(store)s.',
                topic=self.topic,
                rid=self.webhook_id,
                store=instance.display_name,
            ),
        )
        return 'updated'

    def _woo_set_remote_status(self, target_status):
        self.ensure_one()
        if target_status not in ('active', 'paused'):
            raise UserError(self.env._('Unsupported webhook status %(s)s.', s=target_status))
        self._woo_assert_instance_ready()
        if not self.webhook_id:
            raise UserError(self.env._(
                'Webhook "%(name)s" has no remote id. Use Refresh or enable the resource switch.',
                name=self.name,
            ))
        instance = self.instance_id
        api = instance._woo_api()
        try:
            remote = api.update_webhook(int(self.webhook_id), {'status': target_status})
            new_status = self._woo_normalize_remote_status(
                remote.get('status') or target_status,
            )
            self.sudo().write({
                'status': new_status,
                'last_error': False,
            })
            instance._kits_log_operation(
                operation='woo_webhook_status',
                status='success',
                message=self.env._(
                    'Webhook %(topic)s (remote %(rid)s) set to %(status)s on %(store)s.',
                    topic=self.topic,
                    rid=self.webhook_id,
                    status=new_status,
                    store=instance.display_name,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self.sudo().last_error = str(exc)
            instance._kits_log_operation(
                operation='woo_webhook_status',
                status='failure',
                level='error',
                message=self.env._(
                    'Failed to set webhook %(topic)s (remote %(rid)s) to %(status)s: %(err)s',
                    topic=self.topic,
                    rid=self.webhook_id,
                    status=target_status,
                    err=exc,
                ),
            )
            raise UserError(self.env._(
                'Could not update webhook %(topic)s: %(err)s',
                topic=self.topic,
                err=exc,
            )) from exc

    def _woo_delete_remote_and_local(self, *, force_missing_ok=True):
        self.ensure_one()
        instance = self.instance_id
        if instance.active:
            instance._ensure_woo_ready()
        remote_id = self.webhook_id
        if remote_id:
            try:
                instance._woo_api().delete_webhook(int(remote_id), force=True)
            except ConnectorError as exc:
                if force_missing_ok and getattr(exc, 'status_code', None) == 404:
                    _logger.info(
                        'Remote webhook %s already missing for instance %s',
                        remote_id, instance.id,
                    )
                else:
                    self.sudo().last_error = str(exc)
                    instance._kits_log_operation(
                        operation='woo_webhook_delete',
                        status='failure',
                        level='error',
                        message=self.env._(
                            'Failed to delete remote webhook %(rid)s (%(topic)s): %(err)s',
                            rid=remote_id,
                            topic=self.topic,
                            err=exc,
                        ),
                    )
                    raise UserError(self.env._(
                        'Could not delete remote webhook %(topic)s: %(err)s',
                        topic=self.topic,
                        err=exc,
                    )) from exc
            except Exception as exc:  # noqa: BLE001
                self.sudo().last_error = str(exc)
                raise UserError(self.env._(
                    'Could not delete remote webhook %(topic)s: %(err)s',
                    topic=self.topic,
                    err=exc,
                )) from exc
        topic = self.topic
        rid = remote_id
        self.sudo().unlink()
        instance._kits_log_operation(
            operation='woo_webhook_delete',
            status='success',
            message=self.env._(
                'Deleted webhook %(topic)s (remote %(rid)s) for %(store)s.',
                topic=topic,
                rid=rid or '—',
                store=instance.display_name,
            ),
        )

    def _woo_register_remote(self):
        """Create or update the remote webhook from this local record."""
        self.ensure_one()
        self._woo_assert_instance_ready()
        secret = self._woo_ensure_secret()
        instance = self.instance_id
        delivery_url = instance._get_webhook_delivery_url(self.topic)
        payload = {
            'name': self.name,
            'topic': self.topic,
            'delivery_url': delivery_url,
            'secret': secret,
            'status': 'active',
        }
        api = instance._woo_api()
        try:
            if self.webhook_id:
                remote = api.update_webhook(int(self.webhook_id), payload)
            else:
                remote = api.create_webhook(payload)
            remote_id = remote.get('id')
            if not remote_id:
                raise UserError(self.env._('WooCommerce did not return a webhook id.'))
            status = self._woo_normalize_remote_status(remote.get('status') or 'active')
            self.sudo().write({
                'webhook_id': str(int(remote_id)),
                'delivery_url': remote.get('delivery_url') or delivery_url,
                'status': status,
                'last_error': False,
            })
            instance._kits_log_operation(
                operation='woo_webhook_register',
                status='success',
                message=self.env._(
                    'Registered webhook %(topic)s as remote %(rid)s on %(store)s.',
                    topic=self.topic,
                    rid=remote_id,
                    store=instance.display_name,
                ),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.sudo().last_error = str(exc)
            instance._kits_log_operation(
                operation='woo_webhook_register',
                status='failure',
                level='error',
                message=self.env._(
                    'Failed to register webhook %(topic)s on %(store)s: %(err)s',
                    topic=self.topic,
                    store=instance.display_name,
                    err=exc,
                ),
            )
            raise

    def _woo_refresh_from_remote(self):
        """Synchronize one webhook with WooCommerce. Returns 'ok'|'removed'|'error'."""
        self.ensure_one()
        instance = self.instance_id
        if not self.webhook_id:
            try:
                self._woo_register_remote()
                return 'ok'
            except Exception as exc:  # noqa: BLE001
                self.sudo().last_error = str(exc)
                return 'error'

        try:
            remote = instance._woo_api().get_webhook(int(self.webhook_id))
            if not remote or not remote.get('id'):
                raise ConnectorError('Webhook not found', status_code=404)
            status = self._woo_normalize_remote_status(remote.get('status'))
            vals = {
                'status': status,
                'last_error': False,
            }
            if remote.get('delivery_url'):
                vals['delivery_url'] = remote['delivery_url']
            self.sudo().write(vals)
            if status == 'disabled':
                instance._kits_log_operation(
                    operation='woo_webhook_refresh',
                    status='success',
                    level='warning',
                    message=self.env._(
                        'Remote webhook %(topic)s (%(rid)s) is disabled on WooCommerce '
                        '(often after five consecutive delivery failures).',
                        topic=self.topic,
                        rid=self.webhook_id,
                    ),
                )
            return 'ok'
        except ConnectorError as exc:
            if getattr(exc, 'status_code', None) == 404:
                topic = self.topic
                rid = self.webhook_id
                self.sudo().unlink()
                instance._kits_log_operation(
                    operation='woo_webhook_refresh',
                    status='success',
                    message=self.env._(
                        'Removed stale local webhook %(topic)s (remote %(rid)s missing).',
                        topic=topic,
                        rid=rid,
                    ),
                )
                return 'removed'
            self.sudo().last_error = str(exc)
            instance._kits_log_operation(
                operation='woo_webhook_refresh',
                status='failure',
                level='error',
                message=self.env._(
                    'Refresh failed for webhook %(topic)s (%(rid)s): %(err)s',
                    topic=self.topic,
                    rid=self.webhook_id,
                    err=exc,
                ),
            )
            return 'error'
        except Exception as exc:  # noqa: BLE001
            self.sudo().last_error = str(exc)
            return 'error'

    def _woo_note_delivery(self, delivery_id):
        self.ensure_one()
        vals = {'last_delivery_date': fields.Datetime.now(), 'last_error': False}
        if delivery_id:
            vals['last_delivery_id'] = str(delivery_id)
        self.sudo().write(vals)
