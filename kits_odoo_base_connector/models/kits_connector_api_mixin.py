# -*- coding: utf-8 -*-
"""API mixin providing reusable HTTP helpers for connector models.

Platform modules typically inherit :class:`kits.connector.instance` (which
already includes this mixin) and override authentication / validation hooks.
Other models may also inherit the mixin when they need direct HTTP access.
"""

from odoo import models

from ..services.exceptions import ConnectorError
from ..services.http_client import DEFAULT_TIMEOUT, ConnectorHttpClient


class KitsConnectorApiMixin(models.AbstractModel):
    _name = 'kits.connector.api.mixin'
    _description = 'Kits Connector API Mixin'

    # -------------------------------------------------------------------------
    # Extension hooks (override in platform modules)
    # -------------------------------------------------------------------------

    def _kits_get_base_url(self):
        """Return the API base URL for this record."""
        self.ensure_one()
        return getattr(self, 'base_url', '') or ''

    def _kits_get_timeout(self):
        """Return the HTTP timeout in seconds."""
        self.ensure_one()
        timeout = getattr(self, 'timeout', None)
        return timeout or DEFAULT_TIMEOUT

    def _kits_get_default_headers(self):
        """Return default HTTP headers (without secrets when possible)."""
        self.ensure_one()
        return {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'User-Agent': 'KitsOdooBaseConnector/20.0',
        }

    def _kits_get_auth_headers(self, method, url, headers, kwargs):
        """Authentication hook.

        Platform modules override this to inject tokens, signatures, or
        Basic/OAuth headers. Must not log secret values.
        """
        self.ensure_one()
        result = dict(headers or {})
        api_key = getattr(self, 'api_key', False)
        if api_key:
            result.setdefault('Authorization', f'Bearer {api_key}')
        return result

    def _kits_validate_response(self, response):
        """Optional response body validator. Raise ``ConnectorError`` subclasses on failure."""
        return None

    def _kits_create_http_client(self):
        """Build a configured :class:`ConnectorHttpClient` for this record."""
        self.ensure_one()
        record = self

        def auth_hook(method, url, headers, kwargs):
            return record._kits_get_auth_headers(method, url, headers, kwargs)

        def validator(response):
            return record._kits_validate_response(response)

        return ConnectorHttpClient(
            base_url=self._kits_get_base_url(),
            timeout=self._kits_get_timeout(),
            headers=self._kits_get_default_headers(),
            auth_hook=auth_hook,
            response_validator=validator,
        )

    # -------------------------------------------------------------------------
    # Public request helpers
    # -------------------------------------------------------------------------

    def _kits_http_request(self, method, endpoint, **kwargs):
        """Send an HTTP request using the connector HTTP client."""
        self.ensure_one()
        client = self._kits_create_http_client()
        return client.request(method, endpoint, **kwargs)

    def _kits_http_request_json(self, method, endpoint, **kwargs):
        """Send an HTTP request and return parsed JSON."""
        self.ensure_one()
        client = self._kits_create_http_client()
        return client.request_json(method, endpoint, **kwargs)

    def _kits_format_connector_error(self, exc):
        """Normalize connector exceptions into a user-facing message + details."""
        if isinstance(exc, ConnectorError):
            message = exc.message or str(exc)
            details = exc.details
            status_code = exc.status_code
        else:
            message = str(exc)
            details = None
            status_code = None
        return {
            'message': message,
            'details': details,
            'status_code': status_code,
        }
