# -*- coding: utf-8 -*-
"""Reusable HTTP request utilities for connector modules.

Platform modules should not call ``requests`` directly for connector traffic.
Use :class:`ConnectorHttpClient` (or instance helpers that wrap it) so timeouts,
auth headers, logging hooks, and error mapping stay consistent.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Mapping, MutableMapping, Optional

import requests

from .exceptions import (
    ConnectorAPIError,
    ConnectorAuthError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorValidationError,
)

_logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RESPONSE_LOG_CHARS = 4000


class ConnectorHttpClient:
    """Thin HTTP client with connector-friendly defaults and hooks.

    Parameters
    ----------
    base_url:
        Optional base URL prepended to relative paths.
    timeout:
        Default request timeout in seconds.
    headers:
        Default headers merged into every request.
    session:
        Optional ``requests.Session`` for connection reuse / cookies.
    auth_hook:
        Callable ``(method, url, headers, kwargs) -> headers`` that may mutate
        or replace headers before the request is sent.
    response_validator:
        Callable ``(response) -> None`` that raises on invalid responses.
    """

    def __init__(
        self,
        *,
        base_url: str = '',
        timeout: int | float = DEFAULT_TIMEOUT,
        headers: Optional[Mapping[str, str]] = None,
        session: Optional[requests.Session] = None,
        auth_hook: Optional[Callable[..., Mapping[str, str]]] = None,
        response_validator: Optional[Callable[[requests.Response], None]] = None,
    ):
        self.base_url = (base_url or '').rstrip('/')
        self.timeout = timeout
        self.default_headers = dict(headers or {})
        self.session = session or requests.Session()
        self.auth_hook = auth_hook
        self.response_validator = response_validator or self._default_validate_response

    def build_url(self, endpoint: str) -> str:
        """Build an absolute URL from ``base_url`` and ``endpoint``."""
        if not endpoint:
            return self.base_url
        if endpoint.startswith(('http://', 'https://')):
            return endpoint
        endpoint = endpoint if endpoint.startswith('/') else f'/{endpoint}'
        if not self.base_url:
            return endpoint
        return f'{self.base_url}{endpoint}'

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        data: Any = None,
        json_body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[int | float] = None,
        allow_redirects: bool = True,
        **kwargs,
    ) -> requests.Response:
        """Send an HTTP request and validate the response.

        Raises
        ------
        ConnectorTimeoutError
            On request timeout.
        ConnectorAuthError
            On HTTP 401/403.
        ConnectorAPIError
            On other HTTP error statuses.
        ConnectorValidationError
            When the configured response validator rejects the payload.
        ConnectorError
            On unexpected transport failures.
        """
        url = self.build_url(endpoint)
        request_headers: MutableMapping[str, str] = dict(self.default_headers)
        if headers:
            request_headers.update(headers)

        request_kwargs: dict[str, Any] = {
            'params': params,
            'data': data,
            'json': json_body,
            'headers': request_headers,
            'timeout': self.timeout if timeout is None else timeout,
            'allow_redirects': allow_redirects,
        }
        request_kwargs.update(kwargs)

        if self.auth_hook:
            updated_headers = self.auth_hook(
                method=method.upper(),
                url=url,
                headers=request_headers,
                kwargs=request_kwargs,
            )
            if updated_headers is not None:
                request_kwargs['headers'] = dict(updated_headers)

        try:
            response = self.session.request(method.upper(), url, **request_kwargs)
        except requests.Timeout as exc:
            raise ConnectorTimeoutError(
                f'Request timed out calling {url}',
                details=str(exc),
            ) from exc
        except requests.RequestException as exc:
            raise ConnectorError(
                f'HTTP transport error calling {url}',
                details=str(exc),
            ) from exc

        self._raise_for_status(response)
        try:
            self.response_validator(response)
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface validator failures cleanly
            raise ConnectorValidationError(
                f'Response validation failed for {url}',
                details=str(exc),
                status_code=response.status_code,
            ) from exc
        return response

    def get(self, endpoint: str, **kwargs) -> requests.Response:
        return self.request('GET', endpoint, **kwargs)

    def post(self, endpoint: str, **kwargs) -> requests.Response:
        return self.request('POST', endpoint, **kwargs)

    def put(self, endpoint: str, **kwargs) -> requests.Response:
        return self.request('PUT', endpoint, **kwargs)

    def patch(self, endpoint: str, **kwargs) -> requests.Response:
        return self.request('PATCH', endpoint, **kwargs)

    def delete(self, endpoint: str, **kwargs) -> requests.Response:
        return self.request('DELETE', endpoint, **kwargs)

    def request_json(self, method: str, endpoint: str, **kwargs) -> Any:
        """Send a request and return parsed JSON (or ``None`` for empty bodies)."""
        response = self.request(method, endpoint, **kwargs)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ConnectorValidationError(
                'Response body is not valid JSON',
                details=self.truncate_for_log(response.text),
                status_code=response.status_code,
            ) from exc

    @staticmethod
    def truncate_for_log(value: Any, max_chars: int = DEFAULT_MAX_RESPONSE_LOG_CHARS) -> str:
        """Convert a value to a log-safe truncated string."""
        if value is None:
            return ''
        if isinstance(value, (dict, list)):
            try:
                text = json.dumps(value, default=str)
            except (TypeError, ValueError):
                text = str(value)
        else:
            text = value if isinstance(value, str) else str(value)
        if len(text) <= max_chars:
            return text
        return f'{text[:max_chars]}... [truncated]'

    @staticmethod
    def _default_validate_response(response: requests.Response) -> None:
        """Default validator: accept any successful HTTP response."""
        return None

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        if response.ok:
            return
        body = ConnectorHttpClient.truncate_for_log(response.text)
        message = f'HTTP {response.status_code} calling {response.url}'
        if response.status_code in (401, 403):
            raise ConnectorAuthError(
                message,
                details=body,
                status_code=response.status_code,
            )
        raise ConnectorAPIError(
            message,
            details=body,
            status_code=response.status_code,
        )
