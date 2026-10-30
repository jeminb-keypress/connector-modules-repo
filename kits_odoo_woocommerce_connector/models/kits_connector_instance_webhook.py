# -*- coding: utf-8 -*-
"""WooCommerce customer webhook registration and processing."""

import base64
import hashlib
import hmac
import logging
import secrets

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_CUSTOMER_WEBHOOK_TOPICS = (
    'customer.created',
    'customer.updated',
    'customer.deleted',
)

# Non-customer resources (customer webhooks remain separately registerable).
_RESOURCE_WEBHOOK_TOPICS = {
    'products': (
        'product.created',
        'product.updated',
        'product.deleted',
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

_RESOURCE_JOB_TYPES = {
    'products': 'woo_webhook_product',
    'orders': 'woo_webhook_order',
    'coupons': 'woo_webhook_coupon',
    'customers': 'woo_webhook_customer',
}

_RESOURCE_BINDING_MODELS = {
    'products': 'product.template',
    'orders': 'sale.order',
    'coupons': 'kits.woo.coupon',
    'customers': 'res.partner',
}


class KitsConnectorInstanceWebhook(models.Model):
    _inherit = 'kits.connector.instance'

    woo_webhook_enabled = fields.Boolean(
        string='Customer Webhooks Enabled',
        default=False,
        copy=False,
        help='When enabled, this store registers WooCommerce customer.created / '
             'updated / deleted webhooks that POST into Odoo.',
    )
    woo_webhook_secret = fields.Char(
        string='Webhook Secret',
        copy=False,
        groups='kits_odoo_base_connector.group_kits_connector_manager',
        help='HMAC-SHA256 secret used to verify X-WC-Webhook-Signature. '
             'Generated automatically when webhooks are registered.',
    )
    woo_webhook_public_base_url = fields.Char(
        string='Public Odoo Base URL',
        copy=False,
        help='Public HTTPS origin WooCommerce can reach, e.g. https://odoo.example.com. '
             'Required for remote WooCommerce stores. Local WordPress may use '
             'http://127.0.0.1:2099 during development. Do not include a path.',
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
    woo_webhook_ids = fields.Json(
        string='Registered Webhook IDs',
        copy=False,
        help='Map of topic → WooCommerce webhook id for this instance '
             '(customers, products, orders, coupons).',
    )
    woo_resource_webhooks_enabled = fields.Boolean(
        string='Catalog/Order/Coupon Webhooks Enabled',
        default=False,
        copy=False,
    )

    @api.depends('woo_webhook_public_base_url')
    def _compute_woo_webhook_endpoint_url(self):
        for instance in self:
            base = (instance.woo_webhook_public_base_url or '').rstrip('/')
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

    def _woo_ensure_webhook_secret(self):
        self.ensure_one()
        if not self.woo_webhook_secret:
            self.sudo().woo_webhook_secret = secrets.token_urlsafe(32)
        return self.woo_webhook_secret

    def action_woo_register_customer_webhooks(self):
        """Create or refresh customer.created/updated/deleted webhooks on WooCommerce."""
        self.ensure_one()
        if self.platform_type != 'woocommerce':
            raise UserError(self.env._('Only WooCommerce instances support these webhooks.'))
        self._ensure_woo_ready()
        if not (self.woo_webhook_public_base_url or '').strip():
            raise UserError(self.env._(
                'Set Public Odoo Base URL first. WooCommerce must reach a public HTTPS '
                'endpoint (or a tunnel/local URL for development).'
            ))
        secret = self._woo_ensure_webhook_secret()
        delivery_url = self.woo_webhook_endpoint_url
        api = self._woo_api()
        existing = dict(self.woo_webhook_ids or {})
        errors = []
        for topic in _CUSTOMER_WEBHOOK_TOPICS:
            payload = {
                'name': f'Kits Odoo — {topic} ({self.name})',
                'topic': topic,
                'delivery_url': delivery_url,
                'secret': secret,
                'status': 'active',
            }
            try:
                webhook_id = existing.get(topic)
                if webhook_id:
                    remote = api.update_webhook(int(webhook_id), payload)
                else:
                    remote = api.create_webhook(payload)
                new_id = remote.get('id')
                if not new_id:
                    raise ValueError(self.env._('WooCommerce did not return a webhook id'))
                existing[topic] = int(new_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f'{topic}: {exc}')
                _logger.exception(
                    'Failed to register Woo webhook %s for instance %s', topic, self.id,
                )
        self.sudo().write({
            'woo_webhook_ids': existing,
            'woo_webhook_enabled': bool(existing) and not errors,
        })
        if errors and not existing:
            raise UserError(self.env._(
                'Could not register customer webhooks:\n%(errors)s',
                errors='\n'.join(errors),
            ))
        message = self.env._(
            'Customer webhooks registered for %(store)s (%(count)s topics).',
            store=self.name,
            count=len(existing),
        )
        if errors:
            message = f'{message}\n' + '\n'.join(errors)
        return self._woo_notify(message)

    def action_woo_unregister_customer_webhooks(self):
        self.ensure_one()
        if self.platform_type != 'woocommerce':
            raise UserError(self.env._('Only WooCommerce instances support these webhooks.'))
        existing = dict(self.woo_webhook_ids or {})
        to_remove = {t: existing[t] for t in _CUSTOMER_WEBHOOK_TOPICS if t in existing}
        if not to_remove:
            self.woo_webhook_enabled = False
            return self._woo_notify(self.env._('No registered customer webhooks to remove.'))
        self._ensure_woo_ready()
        api = self._woo_api()
        errors = []
        for topic, webhook_id in list(to_remove.items()):
            try:
                api.delete_webhook(int(webhook_id), force=True)
                existing.pop(topic, None)
            except Exception as exc:  # noqa: BLE001
                errors.append(f'{topic} ({webhook_id}): {exc}')
        self.sudo().write({
            'woo_webhook_ids': existing or False,
            'woo_webhook_enabled': False,
        })
        message = self.env._('Customer webhooks disabled for %(store)s.', store=self.name)
        if errors:
            message = f'{message}\n' + '\n'.join(errors)
        return self._woo_notify(message)

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

    def _woo_enqueue_customer_webhook(self, *, topic, delivery_id, woo_id, payload_body,
                                      body_hash):
        """Enqueue async processing; skip if the same delivery was already queued/done."""
        self.ensure_one()
        Job = self.env['kits.connector.sync.job'].sudo()
        recent = Job.search([
            ('instance_id', '=', self.id),
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

        # Stale protection: newer delivery for same woo_id+topic supersedes older pending
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

    def _woo_process_customer_webhook(self, job=None):
        """Apply a queued customer webhook payload."""
        self.ensure_one()
        payload = (job.payload if job else None) or {}
        topic = payload.get('topic') or ''
        woo_id = payload.get('woo_id')
        customer = payload.get('customer') or {}
        event = topic.split('.')[-1] if topic else ''

        if event == 'deleted':
            if not woo_id:
                return self.env._('Skipped customer.deleted without id.')
            binding = self._woo_binding()._kits_find_binding(
                self, 'res.partner', external_id=str(woo_id),
            )
            if not binding:
                return self.env._(
                    'No binding for deleted Woo customer %(id)s.',
                    id=woo_id,
                )
            partner = binding._kits_get_odoo_record()
            binding._kits_mark_deleted_remote(
                message=self.env._(
                    'Marked unavailable after WooCommerce customer.deleted webhook.'
                ),
            )
            return self.env._(
                'Marked binding for Woo customer %(id)s as deleted remote. '
                'Partner %(partner)s was kept.',
                id=woo_id,
                partner=partner.display_name if partner else woo_id,
            )

        if event not in ('created', 'updated'):
            return self.env._('Ignored unsupported webhook topic %(topic)s.', topic=topic)

        if not customer.get('id') and woo_id:
            customer = dict(customer, id=int(woo_id))
        if not customer.get('id'):
            # Ping / empty payloads
            return self.env._('Skipped webhook without customer payload.')

        binding = self._woo_binding()._kits_find_binding(
            self, 'res.partner', external_id=str(customer.get('id')),
        )
        if self._woo_payload_is_stale(binding, customer):
            return self.env._(
                'Skipped stale customer webhook %(topic)s for %(id)s.',
                topic=topic,
                id=customer.get('id'),
            )

        result = self._woo_import_one_customer(customer, job=job)
        partner = self._woo_find_odoo('res.partner', customer['id'])
        return self.env._(
            'Webhook %(topic)s processed (%(result)s) for partner %(partner)s.',
            topic=topic,
            result=result,
            partner=partner.display_name if partner else customer['id'],
        )

    # -------------------------------------------------------------------------
    # Non-customer resource webhooks (products / orders / coupons)
    # -------------------------------------------------------------------------

    def action_woo_register_resource_webhooks(self):
        """Register product/order/coupon webhooks (does not touch customer topics)."""
        self.ensure_one()
        if self.platform_type != 'woocommerce':
            raise UserError(self.env._('Only WooCommerce instances support these webhooks.'))
        self._ensure_woo_ready()
        if not (self.woo_webhook_public_base_url or '').strip():
            raise UserError(self.env._(
                'Set Public Odoo Base URL first. WooCommerce must reach a public HTTPS '
                'endpoint (or a tunnel/local URL for development).'
            ))
        secret = self._woo_ensure_webhook_secret()
        api = self._woo_api()
        existing = dict(self.woo_webhook_ids or {})
        errors = []
        registered = 0
        for resource, topics in _RESOURCE_WEBHOOK_TOPICS.items():
            delivery_url = f'{self.woo_webhook_resource_base_url}/{resource}'
            for topic in topics:
                payload = {
                    'name': f'Kits Odoo — {topic} ({self.name})',
                    'topic': topic,
                    'delivery_url': delivery_url,
                    'secret': secret,
                    'status': 'active',
                }
                try:
                    webhook_id = existing.get(topic)
                    if webhook_id:
                        remote = api.update_webhook(int(webhook_id), payload)
                    else:
                        remote = api.create_webhook(payload)
                    new_id = remote.get('id')
                    if not new_id:
                        raise ValueError(self.env._('WooCommerce did not return a webhook id'))
                    existing[topic] = int(new_id)
                    registered += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f'{topic}: {exc}')
                    _logger.exception(
                        'Failed to register Woo webhook %s for instance %s', topic, self.id,
                    )
        self.sudo().write({
            'woo_webhook_ids': existing,
            'woo_resource_webhooks_enabled': registered > 0 and not errors,
        })
        if errors and registered == 0:
            raise UserError(self.env._(
                'Could not register resource webhooks:\n%(errors)s',
                errors='\n'.join(errors),
            ))
        message = self.env._(
            'Resource webhooks registered for %(store)s (%(count)s topics).',
            store=self.name,
            count=registered,
        )
        if errors:
            message = f'{message}\n' + '\n'.join(errors)
        return self._woo_notify(message)

    def action_woo_unregister_resource_webhooks(self):
        self.ensure_one()
        existing = dict(self.woo_webhook_ids or {})
        resource_topics = {
            t for topics in _RESOURCE_WEBHOOK_TOPICS.values() for t in topics
        }
        to_remove = {t: existing[t] for t in resource_topics if t in existing}
        if not to_remove:
            self.woo_resource_webhooks_enabled = False
            return self._woo_notify(self.env._('No resource webhooks to remove.'))
        self._ensure_woo_ready()
        api = self._woo_api()
        errors = []
        for topic, webhook_id in list(to_remove.items()):
            try:
                api.delete_webhook(int(webhook_id), force=True)
                existing.pop(topic, None)
            except Exception as exc:  # noqa: BLE001
                errors.append(f'{topic} ({webhook_id}): {exc}')
        self.sudo().write({
            'woo_webhook_ids': existing or False,
            'woo_resource_webhooks_enabled': False,
        })
        message = self.env._('Resource webhooks disabled for %(store)s.', store=self.name)
        if errors:
            message = f'{message}\n' + '\n'.join(errors)
        return self._woo_notify(message)

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
            ('instance_id', '=', self.id),
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

    def _woo_process_product_webhook(self, job=None):
        """Apply product webhook. Never imports Woo stock quantities into Odoo."""
        self.ensure_one()
        payload = (job.payload if job else None) or {}
        topic = payload.get('topic') or ''
        woo_id = payload.get('woo_id')
        product = dict(payload.get('product') or {})
        event = topic.split('.')[-1] if topic else ''

        if event == 'deleted':
            if not woo_id:
                return self.env._('Skipped %(topic)s without id.', topic=topic)
            self._woo_mark_product_deleted_remote(
                woo_id,
                message=self.env._(
                    'Marked unavailable after WooCommerce %(topic)s webhook.',
                    topic=topic,
                ),
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

        binding = self._woo_binding()._kits_find_binding(
            self, 'product.template', external_id=str(product.get('id')),
        )
        if self._woo_payload_is_stale(binding, product):
            return self.env._(
                'Skipped stale product webhook %(topic)s for %(id)s.',
                topic=topic,
                id=product.get('id'),
            )

        # Hard protection: strip stock fields so Woo never overwrites Odoo SoT inventory
        for key in (
            'stock_quantity', 'stock_status', 'manage_stock',
            'low_stock_amount', 'backorders',
        ):
            product.pop(key, None)
        if product.get('type') == 'variable' and product.get('variations'):
            # variations list is ids only usually; ignore
            pass

        result = self._woo_import_one_product(product)
        return self.env._(
            'Webhook %(topic)s processed (%(result)s) for product %(pid)s '
            '(stock fields ignored — Odoo remains inventory source of truth).',
            topic=topic,
            result=result,
            pid=product.get('id'),
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

        if event == 'deleted':
            return self._woo_mark_webhook_deleted('kits.woo.coupon', woo_id, topic)

        if event not in ('created', 'updated'):
            return self.env._('Ignored unsupported webhook topic %(topic)s.', topic=topic)
        if not coupon.get('id') and woo_id:
            coupon['id'] = int(woo_id)
        if not coupon.get('id'):
            return self.env._('Skipped coupon webhook without payload.')

        result = self._woo_import_one_coupon(coupon)
        return self.env._(
            'Webhook %(topic)s processed (%(result)s) for coupon %(cid)s.',
            topic=topic,
            result=result,
            cid=coupon.get('id'),
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
