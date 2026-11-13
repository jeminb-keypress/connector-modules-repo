# -*- coding: utf-8 -*-
import hashlib
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_SUPPORTED_RESOURCES = frozenset({'customers', 'products', 'orders', 'coupons'})

# System parameter: comma-separated database names allowed to accept Woo webhooks.
# When set, the active request DB (and any X-Odoo-Database header) must be listed.
_DB_ALLOWLIST_PARAM = 'kits_woo.webhook_db_allowlist'

_TOPIC_PREFIX_RESOURCE = {
    'product': 'products',
    'customer': 'customers',
    'order': 'orders',
    'coupon': 'coupons',
}


class KitsWooWebhookController(http.Controller):
    """Public HTTPS endpoints for WooCommerce webhooks.

    Routes:
    - ``/kits_woo/webhook/<instance_id>/customers`` (legacy + customer events)
    - ``/kits_woo/webhook/<instance_id>/<resource>`` for products|orders|coupons|customers

    Production requires a public HTTPS Odoo URL (or tunnel). Prefer ``dbfilter`` /
    single-database deployments. If ``X-Odoo-Database`` is used on multi-DB hosts,
    configure ``kits_woo.webhook_db_allowlist`` so arbitrary DB names are rejected.
    Secrets, signatures, and full payloads are never logged.
    """

    @http.route(
        '/kits_woo/webhook/<int:instance_id>/customers',
        type='http',
        auth='public',
        methods=['POST', 'GET'],
        csrf=False,
        save_session=False,
    )
    def customer_webhook(self, instance_id, **kwargs):
        return self._handle_webhook(instance_id, 'customers')

    @http.route(
        '/kits_woo/webhook/<int:instance_id>/<string:resource>',
        type='http',
        auth='public',
        methods=['POST', 'GET'],
        csrf=False,
        save_session=False,
    )
    def resource_webhook(self, instance_id, resource, **kwargs):
        resource = (resource or '').lower().strip()
        if resource not in _SUPPORTED_RESOURCES:
            return request.make_json_response({'error': 'unsupported resource'}, status=404)
        return self._handle_webhook(instance_id, resource)

    def _allowed_databases(self):
        ICP = request.env['ir.config_parameter'].sudo()
        raw = ''
        if hasattr(ICP, 'get_str'):
            raw = ICP.get_str(_DB_ALLOWLIST_PARAM, default='') or ''
        elif hasattr(ICP, 'get_param'):
            raw = ICP.get_param(_DB_ALLOWLIST_PARAM, default='') or ''
        return {part.strip() for part in raw.split(',') if part.strip()}

    def _validate_database_routing(self):
        """Reject untrusted multi-DB routing before any business processing."""
        active_db = getattr(request, 'db', None) or ''
        header_db = (
            request.httprequest.headers.get('X-Odoo-Database')
            or request.httprequest.headers.get('X-Odoo-Database'.lower())
            or ''
        ).strip()
        allowlist = self._allowed_databases()

        if header_db and active_db and header_db != active_db:
            _logger.warning(
                'Rejected Woo webhook: X-Odoo-Database does not match active database'
            )
            return request.make_json_response(
                {'error': 'database mismatch'}, status=403,
            )

        if allowlist:
            if not active_db or active_db not in allowlist:
                _logger.warning(
                    'Rejected Woo webhook: active database is not on webhook allowlist'
                )
                return request.make_json_response(
                    {'error': 'database not allowed'}, status=403,
                )
            if header_db and header_db not in allowlist:
                _logger.warning(
                    'Rejected Woo webhook: X-Odoo-Database is not on webhook allowlist'
                )
                return request.make_json_response(
                    {'error': 'database not allowed'}, status=403,
                )
        elif header_db and not active_db:
            _logger.warning(
                'Rejected Woo webhook: database header present but no active database'
            )
            return request.make_json_response(
                {'error': 'database not selected'}, status=403,
            )
        return None

    def _handle_webhook(self, instance_id, resource):
        if request.httprequest.method == 'GET':
            denied = self._validate_database_routing()
            if denied:
                return denied
            return request.make_json_response({'status': 'ok', 'resource': resource})

        denied = self._validate_database_routing()
        if denied:
            return denied

        Instance = request.env['kits.woo.instance'].sudo()
        instance = Instance.browse(instance_id).exists()
        if (
            not instance
            or instance.platform_type != 'woocommerce'
            or not instance.active
        ):
            return request.make_json_response({'error': 'unknown instance'}, status=404)

        headers = request.httprequest.headers
        topic = headers.get('X-WC-Webhook-Topic') or ''
        delivery_id = headers.get('X-WC-Webhook-Delivery-ID') or False
        remote_webhook_id = (
            headers.get('X-WC-Webhook-ID')
            or headers.get('X-WC-Webhook-Id')
            or False
        )
        source = headers.get('X-WC-Webhook-Source') or ''

        topic_resource = _TOPIC_PREFIX_RESOURCE.get((topic or '').split('.', 1)[0])
        if topic and topic_resource and topic_resource != resource:
            _logger.warning(
                'Rejected Woo webhook: topic %s does not match route resource %s',
                topic, resource,
            )
            return request.make_json_response({'error': 'topic mismatch'}, status=400)

        webhook = instance._woo_find_webhook_by_remote_id(remote_webhook_id)
        if not webhook and remote_webhook_id:
            # ID was supplied but unknown — do not fall back to topic-only match
            # (would accept forged/unknown remote ids).
            _logger.warning(
                'Rejected Woo webhook: unknown webhook id for instance %s',
                instance_id,
            )
            return request.make_json_response({'error': 'unknown webhook'}, status=401)
        if not webhook:
            # Legacy fallback when Woo omitted X-WC-Webhook-ID (migration window)
            webhook = self._legacy_resolve_webhook(instance, topic, remote_webhook_id)
        if not webhook:
            _logger.warning(
                'Rejected Woo webhook: unknown webhook id for instance %s',
                instance_id,
            )
            return request.make_json_response({'error': 'unknown webhook'}, status=401)

        if webhook.status != 'active':
            _logger.warning(
                'Rejected Woo webhook: local webhook %s status is %s',
                webhook.id, webhook.status,
            )
            return request.make_json_response({'error': 'webhook inactive'}, status=403)

        if topic and webhook.topic and topic != webhook.topic:
            _logger.warning(
                'Rejected Woo webhook: topic %s does not match registered %s',
                topic, webhook.topic,
            )
            return request.make_json_response({'error': 'topic mismatch'}, status=400)

        if source and not instance._woo_source_matches_store(source):
            _logger.warning(
                'Rejected Woo webhook: source host does not match store URL '
                'for instance %s',
                instance_id,
            )
            return request.make_json_response({'error': 'invalid source'}, status=401)

        raw_body = request.httprequest.get_data() or b''
        signature = headers.get('X-WC-Webhook-Signature')
        secret = webhook.secret or instance.woo_webhook_secret
        if not instance._woo_verify_webhook_signature(raw_body, signature, secret):
            _logger.warning(
                'Rejected WooCommerce %s webhook for instance %s (bad signature)',
                resource, instance_id,
            )
            return request.make_json_response({'error': 'invalid signature'}, status=401)

        if delivery_id and webhook.last_delivery_id and str(delivery_id) == str(
            webhook.last_delivery_id
        ):
            _logger.info(
                'Duplicate Woo webhook delivery %s for instance %s ignored',
                delivery_id, instance_id,
            )
            return request.make_json_response({
                'status': 'accepted',
                'resource': resource,
                'duplicate': True,
            })

        body_hash = hashlib.sha256(raw_body).hexdigest()
        payload_body = {}
        if raw_body:
            try:
                parsed = json.loads(raw_body.decode('utf-8'))
                if isinstance(parsed, dict):
                    payload_body = parsed
            except (UnicodeDecodeError, json.JSONDecodeError):
                if b'webhook_id=' in raw_body:
                    return request.make_json_response({'status': 'pong'})
                return request.make_json_response({'error': 'invalid json'}, status=400)

        woo_id = payload_body.get('id')
        try:
            instance._woo_enqueue_resource_webhook(
                resource=resource,
                topic=topic or webhook.topic,
                delivery_id=delivery_id,
                woo_id=woo_id,
                payload_body=payload_body,
                body_hash=body_hash,
            )
            webhook._woo_note_delivery(delivery_id)
            instance._kits_log_operation(
                operation='woo_webhook_receive',
                status='success',
                message=request.env._(
                    'Accepted %(resource)s webhook %(topic)s delivery %(delivery)s '
                    'for %(store)s.',
                    resource=resource,
                    topic=topic or webhook.topic,
                    delivery=delivery_id or '—',
                    store=instance.display_name,
                ),
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                'Failed to enqueue Woo %s webhook for instance %s', resource, instance_id,
            )
            return request.make_json_response({'error': 'enqueue failed'}, status=500)

        return request.make_json_response({'status': 'accepted', 'resource': resource})

    def _legacy_resolve_webhook(self, instance, topic, remote_webhook_id):
        """Build a temporary resolver from legacy JSON map during migration window."""
        Webhook = request.env['kits.woo.webhook'].sudo()
        if topic:
            existing = Webhook.search([
                ('instance_id', '=', instance.id),
                ('topic', '=', topic),
            ], limit=1)
            if existing:
                return existing

        legacy_ids = instance.woo_webhook_ids or {}
        if not isinstance(legacy_ids, dict):
            return Webhook

        matched_topic = topic
        if remote_webhook_id:
            for leg_topic, leg_id in legacy_ids.items():
                if str(leg_id) == str(remote_webhook_id):
                    matched_topic = leg_topic
                    break
        if not matched_topic or matched_topic not in legacy_ids:
            return Webhook

        # Create a lightweight in-memory-like record by ensuring a DB row exists
        # so subsequent deliveries use the proper path. Preserve shared secret.
        delivery_resource = _TOPIC_PREFIX_RESOURCE.get(matched_topic.split('.', 1)[0])
        if not delivery_resource:
            return Webhook
        base = instance.woo_webhook_resource_base_url
        if not base:
            return Webhook
        record = Webhook.search([
            ('instance_id', '=', instance.id),
            ('topic', '=', matched_topic),
        ], limit=1)
        if record:
            return record
        return Webhook.create({
            'name': instance._woo_webhook_default_name(matched_topic),
            'instance_id': instance.id,
            'topic': matched_topic,
            'webhook_id': str(int(legacy_ids[matched_topic])),
            'delivery_url': f'{base}/{delivery_resource}',
            'secret': instance.woo_webhook_secret or Webhook._woo_generate_secret(),
            'status': 'active',
        })
