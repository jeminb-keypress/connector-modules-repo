# -*- coding: utf-8 -*-
import base64
import hashlib
import json
import logging
from urllib.parse import urlparse

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from odoo.addons.kits_odoo_base_connector.services.exceptions import (
    ConnectorAuthError,
    ConnectorError,
)
from odoo.addons.kits_odoo_base_connector.services.http_client import ConnectorHttpClient

from ..services.woo_api import WooCommerceAPI
from ..services.woo_oauth import apply_http_oauth

_logger = logging.getLogger(__name__)


class KitsConnectorInstance(models.Model):
    _inherit = 'kits.connector.instance'

    platform_type = fields.Selection(
        selection_add=[('woocommerce', 'WooCommerce')],
        ondelete={'woocommerce': 'cascade'},
    )

    # WooCommerce credentials (manager-only; never log values)
    woo_consumer_key = fields.Char(
        string='Consumer Key',
        copy=False,
        groups='kits_odoo_base_connector.group_kits_connector_manager',
    )
    woo_consumer_secret = fields.Char(
        string='Consumer Secret',
        copy=False,
        groups='kits_odoo_base_connector.group_kits_connector_manager',
    )
    woo_api_version = fields.Selection(
        selection=[('wc/v3', 'WooCommerce REST API v3')],
        default='wc/v3',
        required=True,
        string='API Version',
    )

    # Store operational settings
    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Warehouse',
        check_company=True,
        help='Stock quantities for inventory export are taken from this warehouse.',
    )
    pricelist_id = fields.Many2one(
        'product.pricelist',
        string='Pricelist',
        check_company=True,
        help='Used when exporting product prices to WooCommerce.',
    )
    sales_team_id = fields.Many2one(
        'crm.team',
        string='Sales Team',
        check_company=True,
    )
    woo_import_images = fields.Boolean(
        string='Import Product Images',
        default=True,
    )
    woo_match_by_sku = fields.Boolean(
        string='Match Products by SKU',
        default=True,
        help='When no store binding exists, match an unbound Odoo product by SKU '
             'within the same company. Exactly one SKU match is linked. Several matches '
             'are logged and skipped — the connector does not guess, and it never matches '
             'by product name alone.',
    )
    woo_allow_unmapped_order_lines = fields.Boolean(
        string='Allow Unmapped Order Lines',
        default=False,
        help='If disabled (default), orders with unmapped products fail and can be retried. '
             'If enabled, unmapped lines use a placeholder product.',
    )
    woo_order_statuses = fields.Char(
        string='Order Statuses to Import',
        default='processing,completed',
        help='Comma-separated WooCommerce order statuses (e.g. processing,completed).',
    )
    woo_auto_import_orders = fields.Boolean(
        string='Auto Import Orders',
        default=False,
        help='When enabled, the scheduled cron enqueues order import jobs for this store.',
    )
    woo_auto_import_customers = fields.Boolean(
        string='Auto Import Customers',
        default=False,
        help='When enabled, the scheduled cron enqueues customer import jobs for this store.',
    )
    woo_auto_export_stock = fields.Boolean(
        string='Auto Export Inventory',
        default=False,
        help='When enabled, the scheduled cron enqueues inventory export jobs for this store.',
    )
    woo_auto_export_products = fields.Boolean(
        string='Auto Export Products',
        default=False,
        help='When enabled, the scheduled cron exports products that list this store '
             'under WooCommerce Stores and need sync (new / not synced / error). '
             'Products whose remote binding is Deleted Remote are not recreated.',
    )
    woo_last_product_sync = fields.Datetime(readonly=True, copy=False)
    woo_last_order_sync = fields.Datetime(readonly=True, copy=False)
    woo_last_customer_sync = fields.Datetime(readonly=True, copy=False)
    woo_last_stock_sync = fields.Datetime(readonly=True, copy=False)
    woo_order_import_failures = fields.Json(
        string='Pending Failed Order IDs',
        copy=False,
        help='WooCommerce order IDs that failed import and must be retried by ID '
             'so the date checkpoint does not skip them.',
    )
    woo_imported_order_count = fields.Integer(compute='_compute_woo_counts')
    woo_bound_product_count = fields.Integer(compute='_compute_woo_counts')
    woo_bound_customer_count = fields.Integer(compute='_compute_woo_counts')

    @api.depends('binding_ids')
    def _compute_woo_counts(self):
        Binding = self.env['kits.connector.binding']
        for instance in self:
            if instance.platform_type != 'woocommerce':
                instance.woo_imported_order_count = 0
                instance.woo_bound_product_count = 0
                instance.woo_bound_customer_count = 0
                continue
            instance.woo_imported_order_count = Binding.search_count([
                ('instance_id', '=', instance.id),
                ('odoo_model', '=', 'sale.order'),
            ])
            instance.woo_bound_product_count = Binding.search_count([
                ('instance_id', '=', instance.id),
                ('odoo_model', '=', 'product.product'),
            ])
            instance.woo_bound_customer_count = Binding.search_count([
                ('instance_id', '=', instance.id),
                ('odoo_model', '=', 'res.partner'),
            ])

    @api.constrains('platform_type', 'base_url', 'woo_consumer_key', 'woo_consumer_secret')
    def _check_woo_configuration(self):
        for instance in self:
            if instance.platform_type != 'woocommerce':
                continue
            if instance.base_url:
                parsed = urlparse(instance.base_url)
                if parsed.scheme not in ('http', 'https') or not parsed.netloc:
                    raise ValidationError(self.env._(
                        'WooCommerce store URL must be a valid http(s) URL.'
                    ))

    def _is_woocommerce(self):
        self.ensure_one()
        return self.platform_type == 'woocommerce'

    @staticmethod
    def _woo_parse_id_list(raw):
        """Parse comma-separated or list-like IDs into unique ints."""
        if not raw:
            return []
        if isinstance(raw, str):
            parts = [p.strip() for p in raw.replace(';', ',').split(',') if p.strip()]
        elif isinstance(raw, (list, tuple, set)):
            parts = list(raw)
        else:
            parts = [raw]
        result = []
        seen = set()
        for part in parts:
            try:
                value = int(part)
            except (TypeError, ValueError):
                continue
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result

    def _woo_api(self):
        self.ensure_one()
        if not self._is_woocommerce():
            raise UserError(self.env._('This action is only available for WooCommerce instances.'))
        return WooCommerceAPI(self)

    # -------------------------------------------------------------------------
    # HTTP hooks (override base mixin behavior for WooCommerce)
    # -------------------------------------------------------------------------

    def _kits_get_base_url(self):
        self.ensure_one()
        if not self._is_woocommerce():
            return super()._kits_get_base_url()
        store = (self.base_url or '').rstrip('/')
        if not store:
            return ''
        version = self.woo_api_version or 'wc/v3'
        return f'{store}/wp-json/{version}'

    def _woo_is_http_store(self):
        """True when the store URL is plain HTTP (typical local WordPress)."""
        self.ensure_one()
        return (self.base_url or '').lower().startswith('http://')

    def write(self, vals):
        # Password widgets often submit empty strings when unchanged; do not wipe secrets.
        vals = dict(vals)
        for fname in ('woo_consumer_key', 'woo_consumer_secret', 'api_key', 'api_secret'):
            if fname in vals and not vals[fname]:
                vals.pop(fname)
        return super().write(vals)

    def _woo_get_credentials(self):
        """Return (consumer_key, consumer_secret) without logging values.

        Read with sudo so ``groups=`` on credential fields cannot hide values from
        API calls after the user already saved them as a manager.
        """
        self.ensure_one()
        record = self.sudo()
        key = (record.woo_consumer_key or record.api_key or '').strip()
        secret = (record.woo_consumer_secret or record.api_secret or '').strip()
        if not key or not secret:
            raise ConnectorError(self.env._(
                'WooCommerce consumer key and secret are required. '
                'Open the store form, paste both keys again, and Save before testing.'
            ))
        return key, secret

    def _kits_get_auth_headers(self, method, url, headers, kwargs):
        """Authenticate WooCommerce REST calls (used by the pretty /wp-json/ path).

        HTTP stores normally go through ``_woo_http_request`` (rest_route) instead.
        """
        self.ensure_one()
        if not self._is_woocommerce():
            return super()._kits_get_auth_headers(method, url, headers, kwargs)

        result = dict(headers or {})
        result.pop('Authorization', None)
        key, secret = self._woo_get_credentials()

        params = dict(kwargs.get('params') or {})
        params['consumer_key'] = key
        params['consumer_secret'] = secret
        kwargs['params'] = params

        if (url or self._kits_get_base_url() or '').lower().startswith('https://'):
            kwargs['auth'] = (key, secret)
        else:
            kwargs.pop('auth', None)
        return result

    def _kits_get_default_headers(self):
        self.ensure_one()
        headers = super()._kits_get_default_headers()
        if self._is_woocommerce():
            headers['User-Agent'] = 'KitsOdooWooCommerceConnector/20.0'
        return headers

    def _kits_http_request(self, method, endpoint, **kwargs):
        """Route WooCommerce HTTP traffic through the local-safe REST helper."""
        self.ensure_one()
        if self._is_woocommerce():
            return self._woo_http_request(method, endpoint, **kwargs)
        return super()._kits_http_request(method, endpoint, **kwargs)

    def _kits_http_request_json(self, method, endpoint, **kwargs):
        self.ensure_one()
        if self._is_woocommerce():
            response = self._woo_http_request(method, endpoint, **kwargs)
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise ConnectorError(
                    self.env._('WooCommerce response is not valid JSON'),
                    details=ConnectorHttpClient.truncate_for_log(response.text),
                    status_code=response.status_code,
                ) from exc
        return super()._kits_http_request_json(method, endpoint, **kwargs)

    def _woo_get_request_urls(self, endpoint):
        """Build candidate absolute REST URLs for this store.

        WooCommerce only runs API-key authentication when REQUEST_URI contains
        ``wp-json/wc/``. The ``?rest_route=`` form often skips WC auth and then
        fails with ``woocommerce_rest_cannot_view`` (anonymous user).

        For local HTTP / missing permalinks, ``index.php/wp-json/wc/...`` keeps
        ``wp-json/wc`` in the path so consumer key auth is applied.
        """
        self.ensure_one()
        store = (self.base_url or '').rstrip('/')
        version = self.woo_api_version or 'wc/v3'
        endpoint = endpoint if str(endpoint).startswith('/') else f'/{endpoint}'
        pretty = f'{store}/wp-json/{version}{endpoint}'
        index_php = f'{store}/index.php/wp-json/{version}{endpoint}'
        if self._woo_is_http_store():
            # Prefer index.php path first on HTTP (no rewrite rules required)
            preferred = (self.sudo().config_json or {}).get('woo_url_mode')
            if preferred == 'pretty':
                return [pretty, index_php]
            if preferred == 'index_php':
                return [index_php, pretty]
            return [index_php, pretty]
        return [pretty, index_php]

    def _woo_remember_url_mode(self, url):
        """Persist which URL style worked so later sync jobs stay fast."""
        self.ensure_one()
        mode = 'index_php' if '/index.php/wp-json/' in url else 'pretty'
        config = dict(self.sudo().config_json or {})
        if config.get('woo_url_mode') == mode:
            return
        config['woo_url_mode'] = mode
        self.sudo().write({'config_json': config})

    def _woo_http_request(self, method, endpoint, **kwargs):
        """Send a WooCommerce REST request with correct auth for HTTP vs HTTPS.

        Official WooCommerce rules:
        * HTTPS → HTTP Basic Auth (and/or consumer_key query params)
        * HTTP  → OAuth 1.0a one-legged signatures (Basic Auth is rejected)

        Always use a URL containing ``wp-json/wc/`` so WC auth hooks run.
        Secrets are never written to logs.
        """
        self.ensure_one()
        store = (self.base_url or '').rstrip('/')
        if not store:
            raise ConnectorError(self.env._('Store URL is required.'))
        key, secret = self._woo_get_credentials()

        base_params = dict(kwargs.pop('params', None) or {})
        request_kwargs_base = dict(kwargs)

        client = ConnectorHttpClient(
            base_url='',
            timeout=self._kits_get_timeout(),
            headers=self._kits_get_default_headers(),
        )

        urls = self._woo_get_request_urls(endpoint)
        last_exc = None
        for index, url in enumerate(urls):
            request_kwargs = dict(request_kwargs_base)
            params = dict(base_params)
            if self._woo_is_http_store():
                # OAuth 1.0a is mandatory on plain HTTP
                request_kwargs['params'] = params
                apply_http_oauth(method, url, key, secret, request_kwargs)
            else:
                params['consumer_key'] = key
                params['consumer_secret'] = secret
                request_kwargs['params'] = params
                request_kwargs['auth'] = (key, secret)
            try:
                response = client.request(method, url, **request_kwargs)
                self._woo_remember_url_mode(url)
                return response
            except ConnectorError as exc:
                last_exc = exc
                if index < len(urls) - 1 and getattr(exc, 'status_code', None) in (
                    401, 403, 404,
                ):
                    continue
                raise
        raise last_exc or ConnectorError(self.env._('WooCommerce request failed'))

    def _woo_extract_api_message(self, details):
        """Best-effort parse of WooCommerce/WP JSON error payload (no secrets)."""
        if not details:
            return ''
        text = details.strip()
        try:
            payload = json.loads(text) if text.startswith('{') else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return ConnectorHttpClient.truncate_for_log(text, 500)
        message = payload.get('message') or payload.get('code') or ''
        code = payload.get('code') or ''
        if message and code and code not in message:
            return f'{code}: {message}'
        return message or ConnectorHttpClient.truncate_for_log(text, 500)

    def _kits_test_connection(self):
        self.ensure_one()
        if not self._is_woocommerce():
            return super()._kits_test_connection()
        store_url = (self.base_url or '').rstrip('/')
        if not store_url:
            raise ConnectorError(self.env._('Store URL is required.'))
        self._woo_get_credentials()
        try:
            self._woo_api().request_json(
                'GET', '/products', params={'per_page': 1, 'page': 1},
            )
        except ConnectorError as exc:
            if isinstance(exc, ConnectorAuthError) or (
                getattr(exc, 'status_code', None) in (401, 403)
            ):
                api_message = self._woo_extract_api_message(exc.details)
                http_hint = ''
                if self._woo_is_http_store():
                    http_hint = self.env._(
                        '\nThis store uses plain HTTP. WooCommerce requires OAuth 1.0a '
                        'on HTTP (Basic Auth only works on HTTPS). '
                        'Restart Odoo so OAuth signing is loaded, then test again.\n'
                    )
                raise ConnectorError(self.env._(
                    'WooCommerce authentication/permission failed (HTTP %(code)s).\n'
                    'Store: %(store)s\n'
                    'API message: %(api_message)s\n'
                    '%(http_hint)s\n'
                    'Do not share your Consumer Secret.\n'
                    '- Keys: WooCommerce → Settings → Advanced → REST API '
                    '(Enabled, Read/Write, Admin/Shop manager user)\n'
                    '- Permalinks: Settings → Permalinks → Post name → Save\n'
                    '- Or switch the site to HTTPS to use simpler Basic Auth',
                    code=exc.status_code or 401,
                    store=store_url,
                    api_message=api_message or self.env._('(none returned)'),
                    http_hint=http_hint,
                ), details=exc.details, status_code=exc.status_code) from exc
            raise
        return True

    # -------------------------------------------------------------------------
    # Manual sync actions → enqueue base sync jobs
    # -------------------------------------------------------------------------

    def action_woo_import_products(self):
        self._ensure_woo_ready()
        for instance in self:
            instance.action_create_sync_job(
                'woo_import_products',
                name=self.env._('WooCommerce: Import Products (%s)', instance.name),
                payload={},
            )
        return self._woo_notify(self.env._('Product import job(s) queued.'))

    def action_woo_export_products(self):
        self._ensure_woo_ready()
        for instance in self:
            instance.action_create_sync_job(
                'woo_export_products',
                name=self.env._('WooCommerce: Export Products (%s)', instance.name),
                payload={},
            )
        return self._woo_notify(self.env._('Product export job(s) queued.'))

    def action_woo_import_orders(self):
        # sale_stock requires a warehouse to confirm orders with storable lines
        self._ensure_woo_ready(require_warehouse=True)
        for instance in self:
            instance.action_create_sync_job(
                'woo_import_orders',
                name=self.env._('WooCommerce: Import Orders (%s)', instance.name),
                payload={},
            )
        return self._woo_notify(self.env._('Order import job(s) queued.'))

    def action_woo_export_inventory(self):
        self._ensure_woo_ready(require_warehouse=True)
        for instance in self:
            instance.action_create_sync_job(
                'woo_export_inventory',
                name=self.env._('WooCommerce: Export Inventory (%s)', instance.name),
                payload={},
            )
        return self._woo_notify(self.env._('Inventory export job(s) queued.'))

    def action_woo_import_customers(self):
        self._ensure_woo_ready()
        for instance in self:
            instance.action_create_sync_job(
                'woo_import_customers',
                name=self.env._('WooCommerce: Import Customers (%s)', instance.name),
                payload={},
            )
        return self._woo_notify(self.env._('Customer import job(s) queued.'))

    def _ensure_woo_ready(self, require_warehouse=False):
        for instance in self:
            if not instance._is_woocommerce():
                raise UserError(self.env._(
                    'Instance "%(name)s" is not a WooCommerce connector.',
                    name=instance.name,
                ))
            if not instance.active:
                raise UserError(self.env._(
                    'Instance "%(name)s" is archived.',
                    name=instance.name,
                ))
            if require_warehouse and not instance._woo_get_warehouse():
                raise UserError(self.env._(
                    'Configure a warehouse on "%(name)s" (required for order confirmation '
                    'and inventory export with sale_stock).',
                    name=instance.name,
                ))

    def _woo_get_warehouse(self):
        """Return the store warehouse, or the company's first warehouse as fallback."""
        self.ensure_one()
        if self.warehouse_id:
            return self.warehouse_id
        return self.env['stock.warehouse'].search(
            [('company_id', '=', self.company_id.id)],
            limit=1,
        )

    def _woo_notify(self, message):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._('WooCommerce Connector'),
                'message': message,
                'type': 'success',
                'sticky': False,
            },
        }

    # -------------------------------------------------------------------------
    # Shared helpers
    # -------------------------------------------------------------------------

    def _woo_binding(self):
        return self.env['kits.connector.binding']

    def _woo_find_odoo(self, odoo_model, external_id):
        self.ensure_one()
        binding = self._woo_binding()._kits_find_binding(
            self, odoo_model, external_id=str(external_id),
        )
        return binding._kits_get_odoo_record() if binding else self.env[odoo_model]

    def _woo_bind(self, odoo_model, odoo_record_id, external_id, external_data=None):
        self.ensure_one()
        return self._woo_binding()._kits_bind_record(
            self,
            odoo_model,
            odoo_record_id,
            str(external_id),
            external_data=external_data,
        )

    @staticmethod
    def _woo_normalize_remote_datetime(value):
        if not value:
            return ''
        text = str(value).strip().replace(' ', 'T').replace('Z', '')
        return text.split('+')[0]

    def _woo_payload_is_stale(self, binding, payload):
        """True when payload date_modified is older than the binding's last applied remote time."""
        self.ensure_one()
        if not binding or not payload:
            return False
        incoming = self._woo_normalize_remote_datetime(
            payload.get('date_modified_gmt') or payload.get('date_modified')
        )
        if not incoming:
            return False
        previous = self._woo_normalize_remote_datetime(
            (binding.external_data or {}).get('date_modified')
        )
        if not previous:
            return False
        return incoming < previous

    def _woo_allows_recreate(self, job=None):
        """Explicit user actions may recreate; cron and webhooks may not."""
        if self.env.context.get('woo_allow_recreate'):
            return True
        payload = (job.payload if job else None) or {}
        if payload.get('automatic'):
            return False
        return bool(payload.get('allow_recreate'))

    @staticmethod
    def _woo_is_not_found(exc):
        return getattr(exc, 'status_code', None) == 404

    @staticmethod
    def _woo_meta_value(woo_record, *keys):
        wanted = set(keys)
        for item in (woo_record or {}).get('meta_data') or []:
            if not isinstance(item, dict):
                continue
            if item.get('key') in wanted and item.get('value') not in (None, '', False):
                return item.get('value')
        return False

    @staticmethod
    def _woo_meta_snapshot(woo_record, limit=30):
        items = []
        secret_keys = {'consumer_key', 'consumer_secret', 'password', 'webhook_secret'}
        for item in (woo_record or {}).get('meta_data') or []:
            if not isinstance(item, dict) or not item.get('key'):
                continue
            key = str(item.get('key'))
            if key.lower() in secret_keys:
                continue
            value = item.get('value')
            if isinstance(value, str) and len(value) > 500:
                value = value[:500]
            items.append({'key': key, 'value': value})
            if len(items) >= limit:
                break
        return items

    def _woo_extract_cost(self, woo_record):
        raw = (woo_record or {}).get('cost')
        if raw in (None, '', False):
            raw = self._woo_meta_value(
                woo_record, '_wc_cog_cost', '_alg_wc_cog_cost', 'cost', 'standard_price',
            )
        if raw in (None, '', False):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _woo_download_image(self, image_url, *, max_bytes=5 * 1024 * 1024):
        """Download a remote image as (base64, sha256) or (False, False) on failure."""
        if not image_url:
            return False, False
        try:
            response = requests.get(
                image_url,
                timeout=min(self.timeout or 30, 60),
                stream=True,
            )
            response.raise_for_status()
            content_type = (response.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            if content_type and not content_type.startswith('image/'):
                _logger.warning(
                    'Skipping non-image URL for WooCommerce store %s: %s (%s)',
                    self.id, image_url, content_type,
                )
                return False, False
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    _logger.warning(
                        'Skipping oversized WooCommerce image (%s bytes): %s',
                        total, image_url,
                    )
                    return False, False
                chunks.append(chunk)
            raw = b''.join(chunks)
            if not raw:
                return False, False
            digest = hashlib.sha256(raw).hexdigest()
            # Odoo 20 Binary/Image fields expect base64 *str* (or BinaryValue), not raw bytes.
            return base64.b64encode(raw).decode('ascii'), digest
        except Exception as exc:  # noqa: BLE001
            _logger.warning('Failed to download WooCommerce image %s: %s', image_url, exc)
            return False, False

    def _woo_order_failure_ids(self):
        self.ensure_one()
        raw = self.woo_order_import_failures or []
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result

    def _woo_remember_order_failure(self, woo_order_id):
        self.ensure_one()
        try:
            woo_id = int(woo_order_id)
        except (TypeError, ValueError):
            return
        failures = self._woo_order_failure_ids()
        if woo_id not in failures:
            failures.append(woo_id)
            self.woo_order_import_failures = failures

    def _woo_clear_order_failure(self, woo_order_id):
        self.ensure_one()
        try:
            woo_id = int(woo_order_id)
        except (TypeError, ValueError):
            return
        failures = [fid for fid in self._woo_order_failure_ids() if fid != woo_id]
        self.woo_order_import_failures = failures or False

    @api.model
    def _cron_woo_schedule_jobs(self):
        """Enqueue automatic sync jobs for active WooCommerce stores."""
        order_instances = self.search([
            ('platform_type', '=', 'woocommerce'),
            ('active', '=', True),
            ('woo_auto_import_orders', '=', True),
        ])
        for instance in order_instances:
            if not instance._woo_get_warehouse():
                continue
            pending = self.env['kits.connector.sync.job'].search_count([
                ('instance_id', '=', instance.id),
                ('job_type', '=', 'woo_import_orders'),
                ('state', 'in', ('pending', 'running')),
            ])
            if not pending:
                instance.action_create_sync_job(
                    'woo_import_orders',
                    name=self.env._('WooCommerce: Scheduled Order Import (%s)', instance.name),
                )

        customer_instances = self.search([
            ('platform_type', '=', 'woocommerce'),
            ('active', '=', True),
            ('woo_auto_import_customers', '=', True),
        ])
        for instance in customer_instances:
            pending = self.env['kits.connector.sync.job'].search_count([
                ('instance_id', '=', instance.id),
                ('job_type', '=', 'woo_import_customers'),
                ('state', 'in', ('pending', 'running')),
            ])
            if not pending:
                instance.action_create_sync_job(
                    'woo_import_customers',
                    name=self.env._('WooCommerce: Scheduled Customer Import (%s)', instance.name),
                )

        product_instances = self.search([
            ('platform_type', '=', 'woocommerce'),
            ('active', '=', True),
            ('woo_auto_export_products', '=', True),
        ])
        for instance in product_instances:
            pending = self.env['kits.connector.sync.job'].search_count([
                ('instance_id', '=', instance.id),
                ('job_type', '=', 'woo_export_products'),
                ('state', 'in', ('pending', 'running')),
            ])
            if pending:
                continue
            # Products that select this store and are new or dirty for this store
            templates = self.env['product.template'].search([
                ('woo_instance_ids', 'in', instance.id),
                ('sale_ok', '=', True),
                ('company_id', 'in', [False, instance.company_id.id]),
            ])
            if not templates:
                continue
            Binding = self.env['kits.connector.binding']
            need_export_ids = []
            for template in templates:
                binding = Binding._kits_find_binding(
                    instance, 'product.template', odoo_record_id=template.id,
                )
                if binding and binding.sync_state in ('deleted', 'deleted_local'):
                    continue
                if not binding or binding.sync_state in ('not_synced', 'error'):
                    need_export_ids.append(template.id)
            if need_export_ids:
                instance.action_create_sync_job(
                    'woo_export_products',
                    name=self.env._(
                        'WooCommerce: Scheduled Product Export (%s)', instance.name,
                    ),
                    payload={'template_ids': need_export_ids, 'automatic': True},
                )

        stock_instances = self.search([
            ('platform_type', '=', 'woocommerce'),
            ('active', '=', True),
            ('woo_auto_export_stock', '=', True),
        ])
        for instance in stock_instances:
            if not instance._woo_get_warehouse():
                continue
            # Recovery: only re-export bindings that are not_synced/error.
            # Event-driven jobs already cover fresh changes; this catches leftovers.
            pending_targeted = self.env['kits.connector.sync.job'].search_count([
                ('instance_id', '=', instance.id),
                ('job_type', '=', 'woo_export_inventory'),
                ('state', 'in', ('pending', 'running')),
            ])
            if pending_targeted:
                continue
            dirty = self.env['kits.connector.binding'].search_count([
                ('instance_id', '=', instance.id),
                ('odoo_model', '=', 'product.product'),
                ('active', '=', True),
                ('sync_state', 'in', ('not_synced', 'error')),
            ])
            if dirty:
                instance.action_create_sync_job(
                    'woo_export_inventory',
                    name=self.env._(
                        'WooCommerce: Inventory Recovery (%s)', instance.name,
                    ),
                    payload={'recovery_only': True, 'full': True, 'automatic': True},
                    priority=15,
                )
        return True

    def action_woo_open_sync_wizard(self):
        self.ensure_one()
        self._ensure_woo_ready()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('WooCommerce Sync Operation'),
            'res_model': 'kits.woo.sync.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_instance_id': self.id,
            },
        }
