# -*- coding: utf-8 -*-
import re
from urllib.parse import urlparse

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.kits_odoo_base_connector.services.exceptions import (
    ConnectorAuthError,
    ConnectorError,
)

from ..services.shopify_api import SHOP_CONNECTION_QUERY, ShopifyGraphQLClient

# Quarterly Admin API versions still inside the support window of the
# 2026-10 release (latest stable as of 29 Oct 2026, supported until at least
# 1 Oct 2027). 2026-10 was checked against shopify.dev on that date.
SHOPIFY_API_VERSIONS = [
    ('2026-01', '2026-01'),
    ('2026-04', '2026-04'),
    ('2026-07', '2026-07'),
    ('2026-10', '2026-10'),
]
SHOPIFY_API_VERSION_DEFAULT = '2026-10'
_SHOP_HANDLE = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?')


class KitsConnectorInstance(models.Model):
    _inherit = 'kits.connector.instance'

    platform_type = fields.Selection(
        selection_add=[('shopify', 'Shopify')],
        ondelete={'shopify': 'cascade'},
    )
    shopify_shop_domain = fields.Char(
        string='Shop Domain',
        tracking=True,
        help='Permanent shop domain, for example your-store.myshopify.com. '
             'A store name without a domain is completed automatically.',
    )
    shopify_api_version = fields.Selection(
        selection=SHOPIFY_API_VERSIONS,
        string='API Version',
        default=SHOPIFY_API_VERSION_DEFAULT,
        tracking=True,
        help='Shopify Admin API version used in the GraphQL URL. '
             '2026-10 is the current stable version.',
    )
    shopify_access_token = fields.Char(
        string='Admin API Access Token',
        copy=False,
        groups='kits_odoo_base_connector.group_kits_connector_manager',
        help='Token from a custom or merchant-created app. '
             'Sent only as the X-Shopify-Access-Token header. Never logged.',
    )

    # -------------------------------------------------------------------------
    # Values
    # -------------------------------------------------------------------------

    @api.model
    def _shopify_normalize_domain(self, value):
        """Return ``name.myshopify.com`` or raise if the host cannot be used."""
        raw = (value or '').strip().lower()
        if not raw:
            return ''
        if '://' in raw:
            parsed = urlparse(raw)
            raw = (parsed.netloc or parsed.path or '').strip()
        raw = raw.split('/')[0].split('?')[0].strip('.')
        if raw.startswith('www.'):
            raw = raw[4:]
        if '.' not in raw:
            raw = f'{raw}.myshopify.com'
        if not raw.endswith('.myshopify.com'):
            raise ValidationError(self.env._(
                'Use the shop myshopify.com domain (for example your-store.myshopify.com). '
                'The Admin API is not called on a custom storefront domain.'
            ))
        handle = raw[: -len('.myshopify.com')]
        if not _SHOP_HANDLE.fullmatch(handle):
            raise ValidationError(self.env._(
                'Shopify shop domain is not valid. Enter the store name or your-store.myshopify.com.'
            ))
        return raw

    def _shopify_clean_vals(self, vals):
        vals = dict(vals)
        if 'shopify_access_token' in vals:
            token = (vals.get('shopify_access_token') or '').strip()
            if not token:
                # Password widgets submit an empty string when the saved token is unchanged.
                vals.pop('shopify_access_token')
            else:
                vals['shopify_access_token'] = token
        if 'shopify_shop_domain' in vals:
            vals['shopify_shop_domain'] = self._shopify_normalize_domain(
                vals.get('shopify_shop_domain'),
            ) or False
        return vals

    def _shopify_with_base_url(self, vals, record=None):
        """Store the Admin API root on ``base_url`` for Shopify instances."""
        platform = vals.get('platform_type') or (record.platform_type if record else False)
        if platform != 'shopify':
            return vals
        domain = vals.get('shopify_shop_domain')
        if domain is None and record is not None:
            domain = record.shopify_shop_domain
        version = vals.get('shopify_api_version')
        if not version and record is not None:
            version = record.shopify_api_version
        version = version or SHOPIFY_API_VERSION_DEFAULT
        if domain and version:
            vals['base_url'] = f'https://{domain}/admin/api/{version}'
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for vals in vals_list:
            vals = self._shopify_clean_vals(vals)
            vals = self._shopify_with_base_url(vals)
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        vals = self._shopify_clean_vals(vals)
        if len(self) == 1:
            vals = self._shopify_with_base_url(vals, self)
            return super().write(vals)
        if any(key in vals for key in (
            'shopify_shop_domain', 'shopify_api_version', 'platform_type', 'base_url',
        )):
            for record in self:
                rec_vals = record._shopify_with_base_url(dict(vals), record)
                super(KitsConnectorInstance, record).write(rec_vals)
            return True
        return super().write(vals)

    # -------------------------------------------------------------------------
    # HTTP hooks
    # -------------------------------------------------------------------------

    def _is_shopify(self):
        self.ensure_one()
        return self.platform_type == 'shopify'

    def _shopify_api_root(self):
        self.ensure_one()
        domain = (self.shopify_shop_domain or '').strip()
        version = self.shopify_api_version or SHOPIFY_API_VERSION_DEFAULT
        if not domain:
            return ''
        return f'https://{domain}/admin/api/{version}'

    def _kits_get_base_url(self):
        self.ensure_one()
        if self._is_shopify():
            return self._shopify_api_root() or super()._kits_get_base_url()
        return super()._kits_get_base_url()

    def _kits_get_default_headers(self):
        headers = super()._kits_get_default_headers()
        if self.platform_type != 'shopify':
            return headers
        headers = dict(headers)
        headers['User-Agent'] = 'KitsOdooShopifyConnector/20.0'
        return headers

    def _shopify_get_token(self):
        """Return the access token. Reads with sudo so group-restricted fields stay usable."""
        self.ensure_one()
        token = (self.sudo().shopify_access_token or '').strip()
        if not token:
            raise ConnectorAuthError(self.env._(
                'Shopify Admin API access token is required. '
                'Paste the token on the store form and save before testing.'
            ))
        return token

    def _shopify_redact(self, value):
        """Remove the saved access token from a message or details payload."""
        self.ensure_one()
        token = (self.sudo().shopify_access_token or '').strip()
        if value is None or not token or len(token) < 8:
            return value
        if not isinstance(value, str):
            value = str(value)
        return value.replace(token, '[redacted]')

    def _shopify_require_config(self):
        self.ensure_one()
        if not (self.shopify_shop_domain or '').strip():
            raise ConnectorError(self.env._(
                'Shopify shop domain is required. Enter your-store.myshopify.com and save.'
            ))
        if not self.shopify_api_version:
            raise ConnectorError(self.env._('Shopify API version is required.'))
        self._shopify_get_token()

    def _kits_get_auth_headers(self, method, url, headers, kwargs):
        self.ensure_one()
        if not self._is_shopify():
            return super()._kits_get_auth_headers(method, url, headers, kwargs)
        result = dict(headers or {})
        # Do not send the generic Bearer api_key on Shopify calls.
        result.pop('Authorization', None)
        result['X-Shopify-Access-Token'] = self._shopify_get_token()
        return result

    def _shopify_api(self):
        self.ensure_one()
        if not self._is_shopify():
            raise ConnectorError(self.env._(
                'This action is only available for Shopify instances.'
            ))
        return ShopifyGraphQLClient(self)

    def _kits_test_connection(self):
        self.ensure_one()
        if not self._is_shopify():
            return super()._kits_test_connection()
        self._shopify_require_config()
        try:
            payload = self._shopify_api().execute(SHOP_CONNECTION_QUERY)
        except ConnectorAuthError as exc:
            hint = self._shopify_redact(exc.details or '')
            message = self.env._(
                'Shopify authentication failed (HTTP %(code)s). '
                'Check the shop domain and Admin API access token.',
                code=exc.status_code or 401,
            )
            if hint:
                message = f'{message}\n{hint}'
            raise ConnectorAuthError(
                self._shopify_redact(message),
                details=hint or False,
                status_code=exc.status_code or 401,
            ) from exc
        shop = (payload.get('data') or {}).get('shop') or {}
        if not shop.get('name'):
            raise ConnectorError(self.env._(
                'Shopify did not return the shop name. Confirm the API version and token scopes.'
            ))
        return True
