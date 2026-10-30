# -*- coding: utf-8 -*-
"""Shopify Admin GraphQL client.

Uses the base connector HTTP client. Interprets GraphQL ``errors`` on HTTP 200
and retries throttled calls. Never logs the access token.
"""
import json
import time

from odoo.addons.kits_odoo_base_connector.services.exceptions import (
    ConnectorAPIError,
    ConnectorAuthError,
    ConnectorError,
    ConnectorValidationError,
)

# Official connection check: POST graphql.json, header X-Shopify-Access-Token.
# https://shopify.dev/docs/api/admin-graphql/2026-10
SHOP_CONNECTION_QUERY = 'query { shop { name } }'

DEFAULT_MAX_ATTEMPTS = 3
MAX_THROTTLE_SLEEP = 2.0


class ShopifyGraphQLClient:
    """Execute Admin GraphQL operations for one connector instance."""

    def __init__(self, instance, *, sleep=None, max_attempts=DEFAULT_MAX_ATTEMPTS):
        self.instance = instance
        self._sleep = sleep or time.sleep
        self.max_attempts = max_attempts

    def execute(self, query, variables=None):
        """POST a GraphQL document and return the JSON payload.

        Raises
        ------
        ConnectorAuthError
            HTTP 401/403, or a missing token reported by the instance.
        ConnectorAPIError
            HTTP errors, GraphQL ``errors``, or a throttle that persists.
        ConnectorError
            Transport failures and other connector errors.
        """
        if not query or not str(query).strip():
            raise ConnectorValidationError('Shopify GraphQL query is empty.')

        body = {'query': query}
        if variables:
            body['variables'] = variables

        last_error = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                payload = self.instance._kits_http_request_json(
                    'POST',
                    '/graphql.json',
                    json_body=body,
                )
            except ConnectorError as exc:
                if self._should_retry_http(exc, attempt):
                    self._sleep(MAX_THROTTLE_SLEEP)
                    last_error = exc
                    continue
                raise self._redacted(exc) from exc

            if not isinstance(payload, dict):
                raise ConnectorValidationError(
                    'Shopify returned a response that is not a JSON object.',
                )

            errors = _normalize_errors(payload.get('errors'))
            if errors and _is_throttled(errors) and attempt < self.max_attempts:
                self._sleep(_throttle_delay(payload))
                last_error = ConnectorAPIError('Shopify throttled the request.')
                continue
            if errors:
                message = self.instance._shopify_redact(_format_errors(errors))
                details = self.instance._shopify_redact(
                    json.dumps(errors, default=str),
                )
                raise ConnectorAPIError(message, details=details)

            if payload.get('data') is None:
                raise ConnectorValidationError(
                    'Shopify response did not include data.',
                )
            return payload

        raise self._redacted(last_error or ConnectorAPIError(
            'Shopify rate limit persisted after retries.',
        ))

    def _should_retry_http(self, exc, attempt):
        return (
            attempt < self.max_attempts
            and isinstance(exc, ConnectorAPIError)
            and exc.status_code == 429
        )

    def _redacted(self, exc):
        message = self.instance._shopify_redact(
            getattr(exc, 'message', None) or str(exc),
        )
        details = self.instance._shopify_redact(getattr(exc, 'details', None))
        status_code = getattr(exc, 'status_code', None)
        if isinstance(exc, ConnectorError):
            return type(exc)(message, details=details, status_code=status_code)
        return ConnectorAPIError(message, details=details, status_code=status_code)


def _normalize_errors(errors):
    if not errors:
        return []
    if isinstance(errors, str):
        return [{'message': errors}]
    if isinstance(errors, dict):
        return [errors]
    if isinstance(errors, list):
        return errors
    return [{'message': str(errors)}]


def _is_throttled(errors):
    for err in errors:
        if isinstance(err, str) and err.strip().lower() == 'throttled':
            return True
        if not isinstance(err, dict):
            continue
        code = str((err.get('extensions') or {}).get('code') or '').upper()
        message = str(err.get('message') or '').strip().lower()
        if code == 'THROTTLED' or message == 'throttled':
            return True
    return False


def _format_errors(errors):
    parts = []
    for err in errors:
        if isinstance(err, str):
            parts.append(err)
            continue
        if not isinstance(err, dict):
            parts.append(str(err))
            continue
        message = err.get('message') or 'Unknown Shopify error'
        code = (err.get('extensions') or {}).get('code')
        if code and str(code) not in str(message):
            parts.append(f'{code}: {message}')
        else:
            parts.append(str(message))
    return '; '.join(parts) or 'Shopify GraphQL request failed.'


def _throttle_delay(payload):
    """Seconds to wait based on GraphQL cost throttle status. Capped."""
    try:
        status = payload['extensions']['cost']['throttleStatus']
        restore = float(status.get('restoreRate') or 0)
        available = float(status.get('currentlyAvailable') or 0)
        if restore > 0:
            needed = max(0.0, 100.0 - available)
            return min(MAX_THROTTLE_SLEEP, max(0.05, needed / restore))
    except (TypeError, KeyError, ValueError):
        pass
    return 1.0
