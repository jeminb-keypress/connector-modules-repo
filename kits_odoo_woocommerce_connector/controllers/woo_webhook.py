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
        # Odoo 20: typed getters (get_param removed)
        raw = ''
        if hasattr(ICP, 'get_str'):
            raw = ICP.get_str(_DB_ALLOWLIST_PARAM, default='') or ''
        elif hasattr(ICP, 'get_param'):
            raw = ICP.get_param(_DB_ALLOWLIST_PARAM, default='') or ''
        return {part.strip() for part in raw.split(',') if part.strip()}

    def _validate_database_routing(self):
        """Reject untrusted multi-DB routing before any business processing.

        Odoo selects the DB before the controller runs. We never trust a header alone:
        if an allowlist is configured, ``request.db`` must be on it. If a client sent
        ``X-Odoo-Database``, it must equal ``request.db`` and also be allowlisted when
        the allowlist is non-empty.
        """
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
            # Header present but no DB resolved — refuse rather than guessing
            _logger.warning(
                'Rejected Woo webhook: database header present but no active database'
            )
            return request.make_json_response(
                {'error': 'database not selected'}, status=403,
            )
        return None

    def _handle_webhook(self, instance_id, resource):
        if request.httprequest.method == 'GET':
            # Probe must also respect allowlist when configured (no secrets exposed)
            denied = self._validate_database_routing()
            if denied:
                return denied
            return request.make_json_response({'status': 'ok', 'resource': resource})

        denied = self._validate_database_routing()
        if denied:
            return denied

        Instance = request.env['kits.connector.instance'].sudo()
        instance = Instance.browse(instance_id).exists()
        if (
            not instance
            or instance.platform_type != 'woocommerce'
            or not instance.active
        ):
            return request.make_json_response({'error': 'unknown instance'}, status=404)

        # Company/database isolation: instance company must be accessible in this DB
        # (always true per-DB; binding lookups are instance-scoped).

        raw_body = request.httprequest.get_data() or b''
        signature = request.httprequest.headers.get('X-WC-Webhook-Signature')
        secret = instance.woo_webhook_secret
        if not instance._woo_verify_webhook_signature(raw_body, signature, secret):
            _logger.warning(
                'Rejected WooCommerce %s webhook for instance %s (bad signature)',
                resource, instance_id,
            )
            return request.make_json_response({'error': 'invalid signature'}, status=401)

        topic = request.httprequest.headers.get('X-WC-Webhook-Topic') or ''
        delivery_id = request.httprequest.headers.get('X-WC-Webhook-Delivery-ID') or False
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
                topic=topic,
                delivery_id=delivery_id,
                woo_id=woo_id,
                payload_body=payload_body,
                body_hash=body_hash,
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                'Failed to enqueue Woo %s webhook for instance %s', resource, instance_id,
            )
            return request.make_json_response({'error': 'enqueue failed'}, status=500)

        return request.make_json_response({'status': 'accepted', 'resource': resource})
