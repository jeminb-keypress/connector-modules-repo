# -*- coding: utf-8 -*-
"""WooCommerce webhook switches, orchestration, and event processing."""

import base64
import hashlib
import hmac
import ipaddress
import logging
import re
from urllib.parse import urlparse

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

_PRIVATE_HOST_RE = re.compile(
    r'^(localhost|.*\.local|.*\.internal|.*\.lan)$',
    re.IGNORECASE,
)

_RESOURCE_WEBHOOK_TOPICS = {
    'products': (
        'product.created',
        'product.updated',
        'product.deleted',
    ),
    'customers': (
        'customer.created',
        'customer.updated',
        'customer.deleted',
    ),
    'orders': (
        'order.created',
        'order.updated',
        'order.deleted',
    ),
    'coupons': (
        'coupon.created',
        'coupon.updated',
        'coupon.deleted',
    ),
}

_RESOURCE_SWITCH_FIELDS = {
    'products': 'woo_webhook_product_enabled',
    'customers': 'woo_webhook_customer_enabled',
    'orders': 'woo_webhook_order_enabled',
    'coupons': 'woo_webhook_coupon_enabled',
}

_RESOURCE_TMP_FIELDS = {
    'products': 'woo_webhook_product_enabled_tmp',
    'customers': 'woo_webhook_customer_enabled_tmp',
    'orders': 'woo_webhook_order_enabled_tmp',
    'coupons': 'woo_webhook_coupon_enabled_tmp',
}

_TMP_TO_STORED = {
    'woo_webhook_product_enabled_tmp': 'woo_webhook_product_enabled',
    'woo_webhook_customer_enabled_tmp': 'woo_webhook_customer_enabled',
    'woo_webhook_order_enabled_tmp': 'woo_webhook_order_enabled',
    'woo_webhook_coupon_enabled_tmp': 'woo_webhook_coupon_enabled',
}

_RESOURCE_JOB_TYPES = {
    'products': 'woo_webhook_product',
    'orders': 'woo_webhook_order',
    'coupons': 'woo_webhook_coupon',
    'customers': 'woo_webhook_customer',
}


class KitsConnectorInstanceWebhook(models.Model):
    _inherit = 'kits.woo.instance'

    # --- Desired configuration (stored switches) ---
    woo_webhook_product_enabled = fields.Boolean(
        string='Product Webhooks Enabled',
        default=False,
        copy=False,
    )
    woo_webhook_customer_enabled = fields.Boolean(
        string='Customer Webhooks Enabled',
        default=False,
        copy=False,
    )
    woo_webhook_order_enabled = fields.Boolean(
        string='Order Webhooks Enabled',
        default=False,
        copy=False,
    )
    woo_webhook_coupon_enabled = fields.Boolean(
        string='Coupon Webhooks Enabled',
        default=False,
        copy=False,
    )

    # Temporary form fields (not source of truth; mirrored from stored on compute)
    woo_webhook_product_enabled_tmp = fields.Boolean(
        string='Product Webhooks',
        compute='_compute_woo_webhook_enabled_tmp',
        inverse='_inverse_woo_webhook_enabled_tmp',
        readonly=False,
    )
    woo_webhook_customer_enabled_tmp = fields.Boolean(
        string='Customer Webhooks',
        compute='_compute_woo_webhook_enabled_tmp',
        inverse='_inverse_woo_webhook_enabled_tmp',
        readonly=False,
    )
    woo_webhook_order_enabled_tmp = fields.Boolean(
        string='Order Webhooks',
        compute='_compute_woo_webhook_enabled_tmp',
        inverse='_inverse_woo_webhook_enabled_tmp',
        readonly=False,
    )
    woo_webhook_coupon_enabled_tmp = fields.Boolean(
        string='Coupon Webhooks',
        compute='_compute_woo_webhook_enabled_tmp',
        inverse='_inverse_woo_webhook_enabled_tmp',
        readonly=False,
    )

    # Legacy fields retained for migration compatibility only (not UI source of truth)
    woo_webhook_enabled = fields.Boolean(
        string='(Legacy) Customer Webhooks Enabled',
        default=False,
        copy=False,
        help='Deprecated. Migrated to woo_webhook_customer_enabled.',
    )
    woo_resource_webhooks_enabled = fields.Boolean(
        string='(Legacy) Catalog/Order/Coupon Webhooks Enabled',
        default=False,
        copy=False,
        help='Deprecated. Split into product/order/coupon switches.',
    )
    woo_webhook_secret = fields.Char(
        string='(Legacy) Shared Webhook Secret',
        copy=False,
        groups='kits_odoo_base_connector.group_kits_connector_manager',
        help='Deprecated shared secret. Migrated onto individual webhook records.',
    )
    woo_webhook_ids = fields.Json(
        string='(Legacy) Registered Webhook IDs',
        copy=False,
        help='Deprecated topic→id map. Migrated to kits.woo.webhook records.',
    )

    public_webhook_url = fields.Char(
        string='Public Webhook URL',
        copy=False,
        help='Optional externally reachable Odoo origin used for WooCommerce '
             'webhook deliveries (scheme + host[+port], no path/query/fragment). '
             'Leave empty to use Odoo get_base_url() / web.base.url. '
             'Example: https://odoo.example.com or https://abc.ngrok-free.app',
    )
    # Backward-compatible alias (non-stored) for older call sites/tests.
    woo_webhook_public_base_url = fields.Char(
        related='public_webhook_url',
        readonly=False,
        string='Public Webhook URL',
    )
    effective_webhook_base_url = fields.Char(
        string='Effective Webhook Base URL',
        compute='_compute_woo_webhook_endpoint_url',
        help='Base URL currently used for new webhook registrations '
             '(Public Webhook URL when set, otherwise get_base_url()).',
    )
    public_webhook_url_warning = fields.Char(
        string='Public Webhook URL Warning',
        compute='_compute_woo_webhook_endpoint_url',
    )
    woo_webhook_endpoint_url = fields.Char(
        string='Customer Webhook Endpoint',
        compute='_compute_woo_webhook_endpoint_url',
        help='Delivery URL registered on WooCommerce for customer events.',
    )
    woo_webhook_resource_base_url = fields.Char(
        string='Webhook Endpoint Base',
        compute='_compute_woo_webhook_endpoint_url',
        help='Base path for resource webhooks: …/kits_woo/webhook/<instance_id>/<resource>.',
    )
    woo_webhook_ids_rel = fields.One2many(
        'kits.woo.webhook',
        'instance_id',
        string='Registered Webhooks',
    )

    @api.depends(
        'woo_webhook_product_enabled',
        'woo_webhook_customer_enabled',
        'woo_webhook_order_enabled',
        'woo_webhook_coupon_enabled',
    )
    def _compute_woo_webhook_enabled_tmp(self):
        for instance in self:
            instance.woo_webhook_product_enabled_tmp = instance.woo_webhook_product_enabled
            instance.woo_webhook_customer_enabled_tmp = instance.woo_webhook_customer_enabled
            instance.woo_webhook_order_enabled_tmp = instance.woo_webhook_order_enabled
            instance.woo_webhook_coupon_enabled_tmp = instance.woo_webhook_coupon_enabled

    def _inverse_woo_webhook_enabled_tmp(self):
        """Stash intended switch values for write() to apply after API success."""
        pending = self.env.context.get('_woo_webhook_switch_pending')
        if pending is None:
            # Context is immutable; use env cache attribute for this transaction.
            pending = getattr(self.env, '_woo_webhook_switch_pending', None)
            if pending is None:
                pending = {}
                self.env._woo_webhook_switch_pending = pending
        for instance in self:
            intent = pending.setdefault(instance.id, {})
            intent['woo_webhook_product_enabled'] = bool(
                instance.woo_webhook_product_enabled_tmp
            )
            intent['woo_webhook_customer_enabled'] = bool(
                instance.woo_webhook_customer_enabled_tmp
            )
            intent['woo_webhook_order_enabled'] = bool(
                instance.woo_webhook_order_enabled_tmp
            )
            intent['woo_webhook_coupon_enabled'] = bool(
                instance.woo_webhook_coupon_enabled_tmp
            )

    @api.model
    def _woo_normalize_public_webhook_url(self, url, *, raise_error=True):
        """Normalize and validate an optional Public Webhook URL origin."""
        raw = (url or '').strip()
        if not raw:
            return ''
        parsed = urlparse(raw)
        if parsed.scheme not in ('http', 'https'):
            msg = self.env._(
                'Public Webhook URL must start with http:// or https://.'
            )
            if raise_error:
                raise ValidationError(msg)
            return ''
        if parsed.query or parsed.fragment:
            msg = self.env._(
                'Public Webhook URL must not include query strings or fragments.'
            )
            if raise_error:
                raise ValidationError(msg)
            return ''
        if parsed.path and parsed.path not in ('', '/'):
            msg = self.env._(
                'Public Webhook URL must be an origin only (no path). '
                'Example: https://odoo.example.com'
            )
            if raise_error:
                raise ValidationError(msg)
            return ''
        if not parsed.netloc:
            msg = self.env._('Public Webhook URL is missing a host name.')
            if raise_error:
                raise ValidationError(msg)
            return ''
        return f'{parsed.scheme}://{parsed.netloc}'.rstrip('/')

    @api.model
    def _woo_public_webhook_url_warning(self, base_url):
        """Human-readable warning for likely-unreachable or insecure origins."""
        if not base_url:
            return ''
        parsed = urlparse(base_url)
        host = (parsed.hostname or '').lower()
        warnings = []
        if parsed.scheme == 'http':
            warnings.append(self.env._(
                'URL uses HTTP instead of HTTPS. Prefer HTTPS in production.'
            ))
        if host in ('localhost', '127.0.0.1', '::1') or host.endswith('.localhost'):
            warnings.append(self.env._(
                'URL points at localhost. A remote WooCommerce server cannot '
                'reach this address unless you use a public tunnel.'
            ))
        elif _PRIVATE_HOST_RE.match(host or ''):
            warnings.append(self.env._(
                'URL uses an internal hostname that is unlikely to be reachable '
                'from WooCommerce over the internet.'
            ))
        else:
            try:
                ip = ipaddress.ip_address(host)
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                ):
                    warnings.append(self.env._(
                        'URL uses a private/local IP address. WooCommerce must '
                        'be able to reach Odoo over the internet.'
                    ))
            except ValueError:
                pass
        return ' '.join(warnings)

    def _woo_get_public_base_url(self):
        """Public origin WooCommerce must call for this store.

        Uses ``public_webhook_url`` when set; otherwise ``get_base_url()``.
        """
        self.ensure_one()
        override = self._woo_normalize_public_webhook_url(
            self.public_webhook_url, raise_error=False,
        )
        if override:
            return override
        return (self.get_base_url() or '').strip().rstrip('/')

    def _get_webhook_delivery_url(self, topic):
        """Build the delivery URL for one WooCommerce topic on this store."""
        self.ensure_one()
        resource = self.env['kits.woo.webhook']._woo_resource_for_topic(topic)
        if not resource:
            raise UserError(self.env._(
                'Unsupported webhook topic: %(topic)s',
                topic=topic,
            ))
        return self._woo_delivery_url_for_resource(resource)

    @api.depends('public_webhook_url')
    def _compute_woo_webhook_endpoint_url(self):
        for instance in self:
            base = instance._woo_get_public_base_url() if instance.id else (
                instance._woo_normalize_public_webhook_url(
                    instance.public_webhook_url, raise_error=False,
                ) or (instance.get_base_url() or '').strip().rstrip('/')
            )
            instance.effective_webhook_base_url = base or False
            instance.public_webhook_url_warning = (
                instance._woo_public_webhook_url_warning(base) if base else False
            )
            if base and instance.id:
                instance.woo_webhook_resource_base_url = (
                    f'{base}/kits_woo/webhook/{instance.id}'
                )
                instance.woo_webhook_endpoint_url = (
                    f'{base}/kits_woo/webhook/{instance.id}/customers'
                )
            else:
                instance.woo_webhook_resource_base_url = False
                instance.woo_webhook_endpoint_url = False

    @api.constrains('public_webhook_url')
    def _check_public_webhook_url(self):
        for instance in self:
            if not instance.public_webhook_url:
                continue
            instance._woo_normalize_public_webhook_url(
                instance.public_webhook_url, raise_error=True,
            )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('public_webhook_url'):
                vals['public_webhook_url'] = self._woo_normalize_public_webhook_url(
                    vals['public_webhook_url'], raise_error=True,
                )
            elif vals.get('woo_webhook_public_base_url'):
                vals['public_webhook_url'] = self._woo_normalize_public_webhook_url(
                    vals.pop('woo_webhook_public_base_url'), raise_error=True,
                )
        return super().create(vals_list)

    def write(self, vals):
        vals = dict(vals)
        if 'public_webhook_url' in vals and vals.get('public_webhook_url'):
            vals['public_webhook_url'] = self._woo_normalize_public_webhook_url(
                vals['public_webhook_url'], raise_error=True,
            )
        elif 'woo_webhook_public_base_url' in vals and vals.get('woo_webhook_public_base_url'):
            # Alias write path — related field forwards onto public_webhook_url.
            vals['woo_webhook_public_base_url'] = self._woo_normalize_public_webhook_url(
                vals['woo_webhook_public_base_url'], raise_error=True,
            )
        deactivating_ids = set()
        if 'active' in vals and not vals.get('active'):
            for rec in self:
                if rec.active:
                    deactivating_ids.add(rec.id)

        # Never treat legacy switches as the modern source of truth on save.
        # Allow migration/context writes via explicit flag.
        if not self.env.context.get('kits_woo_webhook_legacy_write'):
            vals.pop('woo_webhook_enabled', None)
            vals.pop('woo_resource_webhooks_enabled', None)

        # Direct writes to stored switches (tests/API) still go through orchestration
        # unless the caller already finished API work.
        direct_changes = {}
        if not self.env.context.get('kits_woo_webhook_skip_orchestrate'):
            for stored in _RESOURCE_SWITCH_FIELDS.values():
                if stored in vals:
                    new_val = bool(vals.pop(stored))
                    for rec in self:
                        if bool(rec[stored]) != new_val:
                            direct_changes.setdefault(rec.id, {})[stored] = new_val

        res = super().write(vals)

        pending = getattr(self.env, '_woo_webhook_switch_pending', {}) or {}
        for rec in self:
            intent = pending.pop(rec.id, None) or {}
            changes = dict(direct_changes.get(rec.id) or {})
            for stored, desired in intent.items():
                if bool(rec[stored]) != bool(desired):
                    changes[stored] = bool(desired)
            if changes and not self.env.context.get('kits_woo_webhook_skip_orchestrate'):
                rec._woo_apply_webhook_switch_changes(changes)
            if rec.id in deactivating_ids:
                rec._woo_cleanup_webhooks_on_deactivate()
        return res

    # -------------------------------------------------------------------------
    # Resource helpers
    # -------------------------------------------------------------------------

    @api.model
    def _woo_resource_topics(self, resource):
        return _RESOURCE_WEBHOOK_TOPICS.get(resource) or ()

    @api.model
    def _woo_resource_for_switch(self, stored_field):
        for resource, field in _RESOURCE_SWITCH_FIELDS.items():
            if field == stored_field:
                return resource
        return False

    def _woo_delivery_url_for_resource(self, resource):
        self.ensure_one()
        public_base = self._woo_get_public_base_url()
        if not public_base:
            raise UserError(self.env._(
                'Odoo has no public base URL. Set Public Webhook URL on this '
                'store, or ensure get_base_url() / web.base.url is configured '
                'to an origin WooCommerce can reach.'
            ))
        if not self.id:
            raise UserError(self.env._(
                'Save the store before enabling webhooks.'
            ))
        return f'{public_base}/kits_woo/webhook/{self.id}/{resource}'

    def _woo_webhook_default_name(self, topic):
        self.ensure_one()
        return f'Kits Odoo — {topic} ({self.name})'

    def _woo_find_webhook(self, topic):
        self.ensure_one()
        return self.env['kits.woo.webhook'].sudo().search([
            ('instance_id', '=', self.id),
            ('topic', '=', topic),
        ], limit=1)

    def _woo_normalize_host(self, url):
        if not url:
            return ''
        parsed = urlparse(url if '://' in url else f'https://{url}')
        host = (parsed.netloc or parsed.path or '').lower().strip()
        if host.startswith('www.'):
            host = host[4:]
        return host.split(':')[0]

    def _woo_source_matches_store(self, source_header):
        self.ensure_one()
        store_host = self._woo_normalize_host(self.base_url)
        source_host = self._woo_normalize_host(source_header)
        return bool(store_host and source_host and store_host == source_host)

    # -------------------------------------------------------------------------
    # Orchestration
    # -------------------------------------------------------------------------

    def _woo_apply_webhook_switch_changes(self, changes):
        """Apply enable/disable for changed stored switches only."""
        self.ensure_one()
        errors = []
        for stored, desired in changes.items():
            resource = self._woo_resource_for_switch(stored)
            if not resource:
                continue
            try:
                self._woo_refresh_webhooks(resources=[resource])
                if desired:
                    ok = self._woo_enable_webhook_resource(resource)
                else:
                    ok = self._woo_disable_webhook_resource(resource)
                # Only flip the stored switch when every topic for the resource succeeded.
                if ok:
                    self.with_context(kits_woo_webhook_skip_orchestrate=True).sudo().write({
                        stored: desired,
                    })
            except UserError as exc:
                errors.append(str(exc))
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    'Webhook switch change failed for %s on instance %s',
                    resource, self.id,
                )
                errors.append(f'{resource}: {exc}')
        if errors:
            raise UserError(self.env._(
                'Webhook configuration failed:\n%(errors)s',
                errors='\n'.join(errors),
            ))

    def _woo_notify_partial_webhook_failure(self, message):
        """Show a sticky danger notification without rolling back sibling successes."""
        self.ensure_one()
        try:
            self.env['bus.bus']._sendone(
                self.env.user.partner_id,
                'simple_notification',
                {
                    'title': self.env._('WooCommerce Webhooks'),
                    'message': message,
                    'type': 'danger',
                    'sticky': True,
                },
            )
        except Exception:  # noqa: BLE001
            _logger.warning('Could not send webhook partial-failure notification')

    def _woo_enable_webhook_resource(self, resource):
        """Enable a resource. Returns True on full success.

        Partial success keeps registered sibling topics, leaves the stored switch
        unchanged (caller), logs failures, and notifies without raising so the
        transaction is not rolled back.
        """
        self.ensure_one()
        self._ensure_woo_ready()
        if not self._woo_get_public_base_url():
            raise UserError(self.env._(
                'Odoo has no public base URL. Set Public Webhook URL on this '
                'store, or configure web.base.url / get_base_url() first.'
            ))
        topics = self._woo_resource_topics(resource)
        if not topics:
            raise UserError(self.env._(
                'Unknown webhook resource %(resource)s.',
                resource=resource,
            ))
        Webhook = self.env['kits.woo.webhook'].sudo()
        errors = []
        successes = []
        for topic in topics:
            try:
                with self.env.cr.savepoint():
                    record = self._woo_find_webhook(topic)
                    desired_url = self._get_webhook_delivery_url(topic)
                    if not record:
                        record = Webhook.create({
                            'name': self._woo_webhook_default_name(topic),
                            'instance_id': self.id,
                            'topic': topic,
                            'delivery_url': desired_url,
                            'secret': Webhook._woo_generate_secret(),
                            'status': 'paused',
                        })

                    if record.webhook_id:
                        remote_url = (record.delivery_url or '').rstrip('/')
                        needs_url_push = remote_url != desired_url.rstrip('/')
                        if needs_url_push:
                            # PUT full payload (delivery_url + active). Do not
                            # refresh first — refresh would pull the old remote URL.
                            record._woo_register_remote()
                        elif record.status == 'active':
                            record._woo_refresh_from_remote()
                        else:
                            try:
                                record._woo_set_remote_status('active')
                            except UserError as exc:
                                if record.status == 'disabled':
                                    raise UserError(self.env._(
                                        'WooCommerce webhook %(topic)s is disabled '
                                        'remotely and could not be reactivated '
                                        '(%(err)s). Use Activate after fixing delivery '
                                        'issues, or Delete and re-enable.',
                                        topic=topic,
                                        err=exc,
                                    )) from exc
                                raise
                    else:
                        # Always generate delivery URL from the effective public
                        # base at registration time (never reuse a stale value).
                        record._woo_register_remote()
                    successes.append(topic)
            except Exception as exc:  # noqa: BLE001
                errors.append(f'{topic}: {exc}')
                leftover = self._woo_find_webhook(topic)
                if leftover and leftover.webhook_id:
                    leftover.sudo().write({'last_error': str(exc)})
        if errors:
            detail = '\n'.join(errors)
            if successes:
                message = self.env._(
                    'Enabled %(ok)s topic(s) for %(resource)s, but failed:\n%(errors)s. '
                    'The resource switch was left disabled.',
                    ok=', '.join(successes),
                    resource=resource,
                    errors=detail,
                )
                self._kits_log_operation(
                    operation='woo_webhook_enable',
                    status='failure',
                    level='error',
                    message=message,
                )
                self._woo_notify_partial_webhook_failure(message)
                return False
            raise UserError(self.env._(
                'Could not enable %(resource)s webhooks:\n%(errors)s',
                resource=resource,
                errors=detail,
            ))

        self._kits_log_operation(
            operation='woo_webhook_enable',
            status='success',
            message=self.env._(
                'Enabled %(resource)s webhooks on %(store)s (%(topics)s).',
                resource=resource,
                store=self.display_name,
                topics=', '.join(topics),
            ),
        )
        return True

    def _woo_disable_webhook_resource(self, resource):
        """Pause a resource. Returns True on full success; False on partial."""
        self.ensure_one()
        topics = self._woo_resource_topics(resource)
        if not topics:
            raise UserError(self.env._(
                'Unknown webhook resource %(resource)s.',
                resource=resource,
            ))
        errors = []
        successes = []
        for topic in topics:
            record = self._woo_find_webhook(topic)
            if not record:
                continue
            try:
                with self.env.cr.savepoint():
                    if not record.webhook_id:
                        record.sudo().write({'status': 'paused', 'last_error': False})
                    else:
                        self._ensure_woo_ready()
                        record._woo_set_remote_status('paused')
                    successes.append(topic)
            except Exception as exc:  # noqa: BLE001
                errors.append(f'{topic}: {exc}')

        if errors:
            detail = '\n'.join(errors)
            if successes:
                message = self.env._(
                    'Paused %(ok)s topic(s) for %(resource)s, but failed:\n%(errors)s. '
                    'The resource switch was left enabled.',
                    ok=', '.join(successes),
                    resource=resource,
                    errors=detail,
                )
                self._kits_log_operation(
                    operation='woo_webhook_disable',
                    status='failure',
                    level='error',
                    message=message,
                )
                self._woo_notify_partial_webhook_failure(message)
                return False
            raise UserError(self.env._(
                'Could not pause %(resource)s webhooks:\n%(errors)s',
                resource=resource,
                errors=detail,
            ))

        self._kits_log_operation(
            operation='woo_webhook_disable',
            status='success',
            message=self.env._(
                'Paused %(resource)s webhooks on %(store)s (records retained).',
                resource=resource,
                store=self.display_name,
            ),
        )
        return True

    def action_woo_refresh_webhooks(self):
        self.ensure_one()
        self._ensure_woo_ready()
        result = self._woo_refresh_webhooks()
        return self._woo_notify(result)

    def action_woo_rebuild_webhook_delivery_urls(self):
        """PUT new delivery URLs onto existing remote webhooks (no recreate)."""
        self.ensure_one()
        self._ensure_woo_ready()
        if not self._woo_get_public_base_url():
            raise UserError(self.env._(
                'Odoo has no public base URL. Set Public Webhook URL or '
                'web.base.url before rebuilding delivery URLs.'
            ))
        webhooks = self.env['kits.woo.webhook'].sudo().search([
            ('instance_id', '=', self.id),
        ])
        if not webhooks:
            return self._woo_notify(self.env._(
                'No local webhook records to rebuild for %(store)s.',
                store=self.display_name,
            ))

        updated = skipped = 0
        errors = []
        for webhook in webhooks:
            try:
                with self.env.cr.savepoint():
                    outcome = webhook._woo_rebuild_delivery_url()
                if outcome == 'updated':
                    updated += 1
                elif outcome == 'unchanged':
                    skipped += 1
                else:
                    errors.append(f'{webhook.topic}: unexpected result {outcome}')
            except Exception as exc:  # noqa: BLE001
                # Savepoint rolls back in-method last_error writes; persist here.
                webhook.sudo().write({'last_error': str(exc)})
                errors.append(f'{webhook.topic}: {exc}')

        message = self.env._(
            'Rebuild delivery URLs for %(store)s: %(updated)s updated, '
            '%(skipped)s unchanged, %(errors)s failed.',
            store=self.display_name,
            updated=updated,
            skipped=skipped,
            errors=len(errors),
        )
        if errors:
            detail = '; '.join(errors[:8])
            if len(errors) > 8:
                detail = f'{detail}; …'
            message = f'{message}\n{detail}'
            self._kits_log_operation(
                operation='woo_webhook_rebuild_delivery_url',
                status='failure',
                level='error',
                message=message,
            )
            # Do not raise: raising would roll back successful sibling PUT updates
            # and last_error writes. Surface failure via sticky notification.
            return self._woo_notify(message, sticky=True, notify_type='danger')

        self._kits_log_operation(
            operation='woo_webhook_rebuild_delivery_url',
            status='success',
            message=message,
        )
        return self._woo_notify(message)

    # Compatibility wrappers for upgrade-time view validation (old header buttons).
    def action_woo_register_customer_webhooks(self):
        self.ensure_one()
        self.write({'woo_webhook_customer_enabled': True})
        return self._woo_notify(self.env._(
            'Customer webhooks enabled for %(store)s.',
            store=self.name,
        ))

    def action_woo_unregister_customer_webhooks(self):
        self.ensure_one()
        self.write({'woo_webhook_customer_enabled': False})
        return self._woo_notify(self.env._(
            'Customer webhooks paused for %(store)s.',
            store=self.name,
        ))

    def action_woo_register_resource_webhooks(self):
        self.ensure_one()
        self.write({
            'woo_webhook_product_enabled': True,
            'woo_webhook_order_enabled': True,
            'woo_webhook_coupon_enabled': True,
        })
        return self._woo_notify(self.env._(
            'Product, order, and coupon webhooks enabled for %(store)s.',
            store=self.name,
        ))

    def action_woo_unregister_resource_webhooks(self):
        self.ensure_one()
        self.write({
            'woo_webhook_product_enabled': False,
            'woo_webhook_order_enabled': False,
            'woo_webhook_coupon_enabled': False,
        })
        return self._woo_notify(self.env._(
            'Product, order, and coupon webhooks paused for %(store)s.',
            store=self.name,
        ))

    def _woo_refresh_webhooks(self, resources=None):
        self.ensure_one()
        domain = [('instance_id', '=', self.id)]
        if resources:
            topics = []
            for resource in resources:
                topics.extend(self._woo_resource_topics(resource))
            domain.append(('topic', 'in', topics))
        webhooks = self.env['kits.woo.webhook'].sudo().search(domain)
        ok = removed = errors = 0
        for webhook in webhooks:
            outcome = webhook._woo_refresh_from_remote()
            if outcome == 'ok':
                ok += 1
            elif outcome == 'removed':
                removed += 1
            else:
                errors += 1
        message = self.env._(
            'Webhook refresh for %(store)s: %(ok)s synced, %(removed)s removed, '
            '%(errors)s errors.',
            store=self.display_name,
            ok=ok,
            removed=removed,
            errors=errors,
        )
        self._kits_log_operation(
            operation='woo_webhook_refresh',
            status='failure' if errors else 'success',
            level='error' if errors else 'info',
            message=message,
        )
        if errors:
            raise UserError(message)
        return message

    def _woo_cleanup_webhooks_on_deactivate(self):
        """Delete remote webhooks and local records; clear switches; stop auto jobs."""
        self.ensure_one()
        Webhook = self.env['kits.woo.webhook'].sudo()
        webhooks = Webhook.search([('instance_id', '=', self.id)])
        failures = []
        for webhook in webhooks:
            try:
                # Store is inactive; call API delete without _ensure_woo_ready active check
                remote_id = webhook.webhook_id
                if remote_id:
                    try:
                        # Temporarily allow API via sudo write path — use API directly
                        api = self._woo_api()
                        api.delete_webhook(int(remote_id), force=True)
                    except Exception as exc:  # noqa: BLE001
                        from odoo.addons.kits_odoo_base_connector.services.exceptions import (
                            ConnectorError,
                        )
                        if isinstance(exc, ConnectorError) and getattr(exc, 'status_code', None) == 404:
                            webhook.unlink()
                            continue
                        webhook.last_error = str(exc)
                        failures.append(f'{webhook.topic}: {exc}')
                        self._kits_log_operation(
                            operation='woo_webhook_deactivate',
                            status='failure',
                            level='error',
                            message=self.env._(
                                'Deactivate cleanup failed for %(topic)s (%(rid)s): %(err)s',
                                topic=webhook.topic,
                                rid=remote_id,
                                err=exc,
                            ),
                        )
                        continue
                webhook.unlink()
            except Exception as exc:  # noqa: BLE001
                failures.append(str(exc))

        self.with_context(kits_woo_webhook_skip_orchestrate=True).sudo().write({
            'woo_webhook_product_enabled': False,
            'woo_webhook_customer_enabled': False,
            'woo_webhook_order_enabled': False,
            'woo_webhook_coupon_enabled': False,
            'woo_auto_import_orders': False,
            'woo_auto_export_products': False,
            'woo_auto_import_products': False,
            'woo_auto_export_stock': False,
        })
        self._kits_log_operation(
            operation='woo_webhook_deactivate',
            status='failure' if failures else 'success',
            level='error' if failures else 'info',
            message=self.env._(
                'Store %(store)s deactivated: webhook cleanup done'
                '%(fail)s.',
                store=self.display_name,
                fail=('; failures: ' + '; '.join(failures)) if failures else '',
            ),
        )

    # -------------------------------------------------------------------------
    # Signature + enqueue (security)
    # -------------------------------------------------------------------------

    @api.model
    def _woo_verify_webhook_signature(self, raw_body, signature_header, secret):
        """Return True if X-WC-Webhook-Signature matches HMAC-SHA256(raw_body, secret)."""
        if not secret or not signature_header:
            return False
        if isinstance(raw_body, str):
            raw_body = raw_body.encode('utf-8')
        digest = hmac.new(
            secret.encode('utf-8'),
            raw_body,
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(digest).decode('ascii')
        return hmac.compare_digest(expected, signature_header.strip())

    def _woo_find_webhook_by_remote_id(self, remote_webhook_id):
        self.ensure_one()
        if not remote_webhook_id:
            return self.env['kits.woo.webhook']
        return self.env['kits.woo.webhook'].sudo().search([
            ('instance_id', '=', self.id),
            ('webhook_id', '=', str(remote_webhook_id)),
        ], limit=1)

    def _woo_enqueue_customer_webhook(self, *, topic, delivery_id, woo_id, payload_body,
                                      body_hash):
        """Enqueue async processing; skip if the same delivery was already queued/done."""
        self.ensure_one()
        Job = self.env['kits.connector.sync.job'].sudo()
        recent = Job.search([
            ('instance_id', '=', self._kits_instance_ref()),
            ('job_type', '=', 'woo_webhook_customer'),
            ('state', 'in', ('pending', 'running', 'done', 'cancelled')),
        ], order='id desc', limit=80)
        if delivery_id:
            for job in recent:
                if str((job.payload or {}).get('delivery_id')) == str(delivery_id):
                    return job
        if body_hash:
            for job in recent.filtered(lambda j: j.state in ('pending', 'running', 'done')):
                if (job.payload or {}).get('body_hash') == body_hash:
                    return job

        if woo_id and topic:
            for job in recent.filtered(lambda j: j.state == 'pending'):
                p = job.payload or {}
                if str(p.get('woo_id')) == str(woo_id) and p.get('topic') == topic:
                    job.write({
                        'state': 'cancelled',
                        'result_summary': self.env._(
                            'Superseded by newer webhook delivery %(delivery)s.',
                            delivery=delivery_id or body_hash,
                        ),
                    })

        return self.action_create_sync_job(
            'woo_webhook_customer',
            name=self.env._(
                'WooCommerce webhook %(topic)s #%(woo_id)s (%(store)s)',
                topic=topic,
                woo_id=woo_id or '?',
                store=self.name,
            ),
            payload={
                'topic': topic,
                'delivery_id': delivery_id,
                'woo_id': woo_id,
                'body_hash': body_hash,
                'customer': payload_body if isinstance(payload_body, dict) else {},
            },
            priority=5,
        )

    def _woo_customer_webhook_role_allowed(self, customer):
        """Webhooks accept only WooCommerce role ``customer``."""
        return self._woo_customer_role_allowed(customer, allow_missing=False)

    def _woo_process_customer_webhook(self, job=None):
        """Validate webhook payload and enqueue the customer waiting list."""
        self.ensure_one()
        payload = (job.payload if job else None) or {}
        topic = payload.get('topic') or ''
        woo_id = payload.get('woo_id')
        customer = payload.get('customer') or {}
        delivery_id = payload.get('delivery_id')
        body_hash = payload.get('body_hash')
        event = topic.split('.')[-1] if topic else ''
        Waiting = self.env['kits.woo.customer.waiting']

        if event == 'deleted':
            if not woo_id and customer.get('id'):
                woo_id = customer.get('id')
            if not woo_id:
                return self.env._('Skipped customer.deleted without id.')
            binding = self.env['kits.woo.customer.binding']._find(
                self, external_id=str(woo_id),
            )
            if not binding:
                self._kits_log_operation(
                    operation='woo_webhook_customer',
                    status='success',
                    level='info',
                    message=self.env._(
                        'Ignored customer.deleted for WooCommerce user %(id)s on %(store)s: '
                        'no customer binding exists (non-customer roles are never bound).',
                        id=woo_id,
                        store=self.display_name,
                    ),
                    job=job,
                )
                return self.env._(
                    'Ignored customer.deleted for %(id)s with no customer binding.',
                    id=woo_id,
                )
            Waiting._enqueue(
                self,
                external_id=woo_id,
                payload=customer if isinstance(customer, dict) else {},
                source='webhook',
                event_type='delete',
                delivery_id=delivery_id,
                body_hash=body_hash,
                process_delete_now=True,
                trigger_batch=False,
            )
            return self.env._(
                'Queued and processed delete for WooCommerce customer %(id)s.',
                id=woo_id,
            )

        if event not in ('created', 'updated'):
            return self.env._('Ignored unsupported webhook topic %(topic)s.', topic=topic)

        if not customer.get('id') and woo_id:
            customer = dict(customer, id=int(woo_id))
        if not customer.get('id'):
            return self.env._('Skipped webhook without customer payload.')

        if not self._woo_customer_webhook_role_allowed(customer):
            self._kits_log_operation(
                operation='woo_webhook_customer',
                status='success',
                level='info',
                message=self.env._(
                    'Ignored %(topic)s for WooCommerce user %(id)s with role %(role)s. '
                    'Only role "customer" is accepted for customer webhooks.',
                    topic=topic,
                    id=customer.get('id'),
                    role=customer.get('role') or '(missing)',
                ),
                job=job,
            )
            return self.env._(
                'Ignored webhook %(topic)s for non-customer role %(role)s.',
                topic=topic,
                role=customer.get('role') or '(missing)',
            )

        binding = self._woo_record_binding(
            'res.partner', external_id=str(customer.get('id')),
        )
        if self._woo_payload_is_stale(binding, customer):
            return self.env._(
                'Skipped stale customer webhook %(topic)s for %(id)s.',
                topic=topic,
                id=customer.get('id'),
            )

        row = Waiting._enqueue(
            self,
            external_id=customer.get('id'),
            payload=customer,
            source='webhook',
            event_type='create' if event == 'created' else 'update',
            delivery_id=delivery_id,
            body_hash=body_hash,
            trigger_batch=True,
        )
        if row.state == 'pending':
            row._process_entries(job=job)
        partner = row.partner_id
        return self.env._(
            'Webhook %(topic)s processed (%(state)s) for WooCommerce customer %(id)s'
            '%(partner)s.',
            topic=topic,
            state=row.state,
            id=customer.get('id'),
            partner=f' / {partner.display_name}' if partner else '',
        )

    def _woo_enqueue_resource_webhook(self, *, resource, topic, delivery_id, woo_id,
                                      payload_body, body_hash):
        """Shared enqueue for product/order/coupon (and reusable by customers)."""
        self.ensure_one()
        job_type = _RESOURCE_JOB_TYPES.get(resource)
        if not job_type:
            raise UserError(self.env._('Unsupported webhook resource %(r)s.', r=resource))
        if resource == 'customers':
            return self._woo_enqueue_customer_webhook(
                topic=topic,
                delivery_id=delivery_id,
                woo_id=woo_id,
                payload_body=payload_body,
                body_hash=body_hash,
            )

        Job = self.env['kits.connector.sync.job'].sudo()
        recent = Job.search([
            ('instance_id', '=', self._kits_instance_ref()),
            ('job_type', '=', job_type),
            ('state', 'in', ('pending', 'running', 'done', 'cancelled')),
        ], order='id desc', limit=80)
        if delivery_id:
            for job in recent:
                if str((job.payload or {}).get('delivery_id')) == str(delivery_id):
                    return job
        if body_hash:
            for job in recent.filtered(lambda j: j.state in ('pending', 'running', 'done')):
                if (job.payload or {}).get('body_hash') == body_hash:
                    return job
        if woo_id and topic:
            for job in recent.filtered(lambda j: j.state == 'pending'):
                p = job.payload or {}
                if str(p.get('woo_id')) == str(woo_id) and p.get('topic') == topic:
                    job.write({
                        'state': 'cancelled',
                        'result_summary': self.env._(
                            'Superseded by newer webhook delivery %(delivery)s.',
                            delivery=delivery_id or body_hash,
                        ),
                    })

        payload_key = {
            'products': 'product',
            'orders': 'order',
            'coupons': 'coupon',
        }.get(resource, 'data')
        return self.action_create_sync_job(
            job_type,
            name=self.env._(
                'WooCommerce webhook %(topic)s #%(woo_id)s (%(store)s)',
                topic=topic,
                woo_id=woo_id or '?',
                store=self.name,
            ),
            payload={
                'topic': topic,
                'resource': resource,
                'delivery_id': delivery_id,
                'woo_id': woo_id,
                'body_hash': body_hash,
                payload_key: payload_body if isinstance(payload_body, dict) else {},
            },
            priority=5,
        )

    def _woo_product_webhook_is_eligible(self, product, binding):
        """Accept known products (incl. archived) or published non-variation products."""
        self.ensure_one()
        if binding:
            return True
        ptype = (product.get('type') or 'simple').lower()
        if ptype == 'variation':
            return False
        status = (product.get('status') or '').lower()
        return status == 'publish'

    def _woo_process_product_webhook(self, job=None):
        """Enqueue product webhook onto the product waiting list.

        Never imports Woo stock quantities into Odoo. Process runs via the
        waiting-list cron/job, or immediately when 50 draft rows accumulate.
        """
        self.ensure_one()
        payload = (job.payload if job else None) or {}
        topic = payload.get('topic') or ''
        woo_id = payload.get('woo_id')
        product = dict(payload.get('product') or {})
        event = topic.split('.')[-1] if topic else ''
        Waiting = self.env['kits.woo.product.waiting']

        if event == 'deleted':
            if not woo_id:
                return self.env._('Skipped %(topic)s without id.', topic=topic)
            Waiting._enqueue(
                self,
                external_id=woo_id,
                payload=product or {'id': int(woo_id) if str(woo_id).isdigit() else woo_id},
                source='webhook',
                event_type='delete',
                delivery_id=payload.get('delivery_id') or False,
                body_hash=payload.get('body_hash') or False,
                process_delete_now=True,
                trigger_batch=False,
            )
            self._kits_log_operation(
                operation='woo_webhook_delete',
                status='success',
                message=self.env._(
                    'Remote delete for product.template external_id=%(id)s — binding marked '
                    'deleted; Odoo product kept; other stores unchanged.',
                    id=woo_id,
                ),
            )
            return self.env._(
                'Marked binding for product.template %(id)s as deleted remote.',
                id=woo_id,
            )

        if event not in ('created', 'updated'):
            return self.env._('Ignored unsupported webhook topic %(topic)s.', topic=topic)
        if not product.get('id') and woo_id:
            product['id'] = int(woo_id)
        if not product.get('id'):
            return self.env._('Skipped product webhook without payload.')

        binding = self.env['kits.woo.product.binding'].with_context(active_test=False)._find(
            self, external_id=str(product.get('id')),
        )
        if not self._woo_product_webhook_is_eligible(product, binding):
            return self.env._(
                'Ignored product webhook %(topic)s for %(id)s '
                '(unknown unpublished/draft or standalone variation).',
                topic=topic,
                id=product.get('id'),
            )

        if self._woo_payload_is_stale(binding, product):
            return self.env._(
                'Skipped stale product webhook %(topic)s for %(id)s.',
                topic=topic,
                id=product.get('id'),
            )

        for key in (
            'stock_quantity', 'stock_status', 'manage_stock',
            'low_stock_amount', 'backorders',
        ):
            product.pop(key, None)

        if binding and not binding.active:
            binding.write({'active': True, 'exists_in_store': True})

        try:
            product = self._woo_prepare_product_payload_for_queue(product, self._woo_api())
        except Exception as exc:  # noqa: BLE001
            self._kits_log_operation(
                operation='woo_webhook_product',
                status='failure',
                level='error',
                message=str(exc),
                details=f'woo_product_id={product.get("id")}',
                job=job,
            )
            raise

        row = Waiting._enqueue(
            self,
            external_id=product.get('id'),
            payload=product,
            source='webhook',
            event_type='create' if event == 'created' else 'update',
            delivery_id=payload.get('delivery_id') or False,
            body_hash=payload.get('body_hash') or False,
            trigger_batch=True,
        )
        return self.env._(
            'Webhook %(topic)s queued for product %(pid)s on waiting list '
            '(row %(row)s, stock fields ignored — Odoo remains inventory source of truth).',
            topic=topic,
            pid=product.get('id'),
            row=row.id,
        )

    def _woo_process_order_webhook(self, job=None):
        self.ensure_one()
        payload = (job.payload if job else None) or {}
        topic = payload.get('topic') or ''
        woo_id = payload.get('woo_id')
        order = dict(payload.get('order') or {})
        event = topic.split('.')[-1] if topic else ''

        if event == 'deleted':
            return self._woo_mark_webhook_deleted('sale.order', woo_id, topic)

        if event not in ('created', 'updated'):
            return self.env._('Ignored unsupported webhook topic %(topic)s.', topic=topic)
        if not order.get('id') and woo_id:
            order['id'] = int(woo_id)
        if not order.get('id'):
            return self.env._('Skipped order webhook without payload.')

        result, _date = self._woo_import_one_order(order, job=job)
        return self.env._(
            'Webhook %(topic)s processed (%(result)s) for order %(oid)s.',
            topic=topic,
            result=result,
            oid=order.get('id'),
        )

    def _woo_process_coupon_webhook(self, job=None):
        self.ensure_one()
        payload = (job.payload if job else None) or {}
        topic = payload.get('topic') or ''
        woo_id = payload.get('woo_id')
        coupon = dict(payload.get('coupon') or {})
        event = topic.split('.')[-1] if topic else ''

        Waiting = self.env['kits.woo.coupon.waiting']
        if event == 'deleted':
            if not woo_id and coupon.get('id'):
                woo_id = coupon.get('id')
            if not woo_id:
                return self.env._('Skipped %(topic)s without id.', topic=topic)
            row = Waiting._enqueue(
                self,
                external_id=woo_id,
                payload=coupon or {'id': woo_id},
                source='webhook',
                event_type='delete',
                delivery_id=payload.get('delivery_id') or False,
                body_hash=payload.get('body_hash') or False,
            )
            row.write({'state': 'processing'})
            row._process_entries(job=job)
            return self.env._(
                'Webhook %(topic)s processed delete for coupon %(cid)s.',
                topic=topic,
                cid=woo_id,
            )

        if event not in ('created', 'updated'):
            return self.env._('Ignored unsupported webhook topic %(topic)s.', topic=topic)
        if not coupon.get('id') and woo_id:
            coupon['id'] = int(woo_id)
        if not coupon.get('id'):
            return self.env._('Skipped coupon webhook without payload.')

        binding = self._woo_binding()._kits_find_binding(
            self, 'kits.woo.coupon', external_id=str(coupon.get('id')),
        )
        if self._woo_payload_is_stale(binding, coupon):
            return self.env._(
                'Skipped stale coupon webhook %(topic)s for %(id)s.',
                topic=topic,
                id=coupon.get('id'),
            )

        row = Waiting._enqueue(
            self,
            external_id=coupon.get('id'),
            payload=coupon,
            source='webhook',
            event_type='create' if event == 'created' else 'update',
            delivery_id=payload.get('delivery_id') or False,
            body_hash=payload.get('body_hash') or False,
            trigger_batch=True,
        )
        # Near-real-time: process this row in the same webhook job.
        if row.state == 'pending':
            row.write({'state': 'processing'})
            row._process_entries(job=job)
        return self.env._(
            'Webhook %(topic)s queued/processed for coupon %(cid)s (waiting row %(row)s).',
            topic=topic,
            cid=coupon.get('id'),
            row=row.id,
        )

    def _woo_mark_webhook_deleted(self, odoo_model, woo_id, topic):
        self.ensure_one()
        if not woo_id:
            return self.env._('Skipped %(topic)s without id.', topic=topic)
        binding = self._woo_binding()._kits_find_binding(
            self, odoo_model, external_id=str(woo_id),
        )
        if not binding:
            return self.env._(
                'No binding for deleted remote %(model)s %(id)s.',
                model=odoo_model,
                id=woo_id,
            )
        binding._kits_mark_deleted_remote(
            message=self.env._(
                'Marked unavailable after WooCommerce %(topic)s webhook.',
                topic=topic,
            ),
        )
        self._kits_log_operation(
            operation='woo_webhook_delete',
            status='success',
            message=self.env._(
                'Remote delete for %(model)s external_id=%(id)s — binding marked deleted; '
                'Odoo record kept.',
                model=odoo_model,
                id=woo_id,
            ),
        )
        return self.env._(
            'Marked binding for %(model)s %(id)s as deleted remote.',
            model=odoo_model,
            id=woo_id,
        )
