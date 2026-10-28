# -*- coding: utf-8 -*-
"""WooCommerce OAuth 1.0a one-legged signing for HTTP (non-SSL) REST calls.

Official WooCommerce rule:
* HTTPS → HTTP Basic Auth (consumer key/secret)
* HTTP  → OAuth 1.0a one-legged signatures (Basic Auth is rejected)

Secrets must never be logged by callers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import Any, Mapping, MutableMapping
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit


def percent_encode(value: Any) -> str:
    """RFC 3986 percent-encoding used by OAuth 1.0a / WooCommerce."""
    return quote(str(value), safe='~')


def normalize_base_url(url: str) -> str:
    """Return the URL WooCommerce uses when verifying the OAuth signature.

    Strip query/fragment and remove a trailing slash from the path except for
    root ``/``.
    """
    parts = urlsplit(url)
    path = parts.path or '/'
    if path != '/' and path.endswith('/'):
        path = path.rstrip('/')
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def build_oauth_params(
    consumer_key: str,
    *,
    extra_params: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Build unsigned OAuth + request parameters (no oauth_signature yet)."""
    params: dict[str, str] = {
        'oauth_consumer_key': consumer_key,
        'oauth_nonce': uuid.uuid4().hex,
        'oauth_signature_method': 'HMAC-SHA1',
        'oauth_timestamp': str(int(time.time())),
    }
    if extra_params:
        for key, value in extra_params.items():
            if value is None or key == 'oauth_signature':
                continue
            # WooCommerce / requests may pass list values for multi params; flatten
            if isinstance(value, (list, tuple)):
                if not value:
                    continue
                value = value[0]
            params[str(key)] = str(value)
    return params


def sign_oauth_request(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    *,
    params: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Return query params including a valid ``oauth_signature`` for WooCommerce HTTP.

    The signature base string is::

        METHOD&urlencoded(base_url)&urlencoded(sorted_param_string)

    Signing key is ``consumer_secret&`` (empty token secret).
    """
    base_url = normalize_base_url(url)
    # Merge any params already present on the URL (should normally be empty)
    url_params = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    oauth_params = build_oauth_params(consumer_key, extra_params={**url_params, **(params or {})})

    # Sort and encode parameter string
    sorted_items = sorted(
        ((percent_encode(k), percent_encode(v)) for k, v in oauth_params.items()),
        key=lambda item: (item[0], item[1]),
    )
    param_string = '&'.join(f'{k}={v}' for k, v in sorted_items)
    base_string = '&'.join([
        method.upper(),
        percent_encode(base_url),
        percent_encode(param_string),
    ])
    signing_key = f'{consumer_secret}&'
    digest = hmac.new(
        signing_key.encode('utf-8'),
        base_string.encode('utf-8'),
        hashlib.sha1,
    ).digest()
    # WooCommerce expects the raw HMAC digest base64-encoded
    oauth_params['oauth_signature'] = base64.b64encode(digest).decode('ascii')
    return oauth_params


def apply_http_oauth(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    request_kwargs: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Mutate ``request_kwargs`` to use OAuth query params and drop Basic Auth."""
    existing_params = dict(request_kwargs.get('params') or {})
    # Remove Basic-auth style query secrets if present; OAuth replaces them
    existing_params.pop('consumer_key', None)
    existing_params.pop('consumer_secret', None)
    signed = sign_oauth_request(
        method,
        url,
        consumer_key,
        consumer_secret,
        params=existing_params,
    )
    request_kwargs['params'] = signed
    request_kwargs.pop('auth', None)
    return request_kwargs
