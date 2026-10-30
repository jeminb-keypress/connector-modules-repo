# -*- coding: utf-8 -*-
"""WooCommerce REST API helpers built on the base connector HTTP client.

All methods expect an ``kits.connector.instance`` record already configured for
WooCommerce. Secrets must never be written to logs by callers.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator, Mapping, Optional

from odoo.addons.kits_odoo_base_connector.services.exceptions import (
    ConnectorAPIError,
    ConnectorError,
)
from odoo.addons.kits_odoo_base_connector.services.http_client import ConnectorHttpClient

_logger = logging.getLogger(__name__)

DEFAULT_PER_PAGE = 50
MAX_RATE_LIMIT_RETRIES = 3


class WooCommerceAPI:
    """Thin facade over instance HTTP helpers for WooCommerce REST endpoints."""

    def __init__(self, instance):
        self.instance = instance

    # ------------------------------------------------------------------
    # Low-level request with 429 retry
    # ------------------------------------------------------------------

    def request(self, method: str, endpoint: str, **kwargs):
        """Perform an HTTP request with limited 429 backoff retries."""
        last_exc = None
        for attempt in range(MAX_RATE_LIMIT_RETRIES):
            try:
                return self.instance._kits_http_request(method, endpoint, **kwargs)
            except ConnectorAPIError as exc:
                last_exc = exc
                if exc.status_code != 429 or attempt >= MAX_RATE_LIMIT_RETRIES - 1:
                    raise
                sleep_for = min(2 ** attempt, 8)
                _logger.warning(
                    'WooCommerce rate limit on instance %s; retrying in %ss',
                    self.instance.id,
                    sleep_for,
                )
                time.sleep(sleep_for)
        raise last_exc or ConnectorError('WooCommerce request failed')

    def request_json(self, method: str, endpoint: str, **kwargs) -> Any:
        response = self.request(method, endpoint, **kwargs)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ConnectorError(
                'WooCommerce response is not valid JSON',
                details=ConnectorHttpClient.truncate_for_log(response.text),
                status_code=response.status_code,
            ) from exc

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def iter_paginated(
        self,
        endpoint: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> Iterator[list]:
        """Yield successive pages (lists) from a WooCommerce collection endpoint."""
        page = 1
        base_params = dict(params or {})
        while True:
            page_params = dict(base_params, page=page, per_page=per_page)
            response = self.request('GET', endpoint, params=page_params)
            try:
                payload = response.json() if response.content else []
            except ValueError as exc:
                raise ConnectorError(
                    'Invalid JSON page from WooCommerce',
                    details=ConnectorHttpClient.truncate_for_log(response.text),
                ) from exc
            if not isinstance(payload, list):
                raise ConnectorError(
                    'Expected a list response from WooCommerce collection endpoint',
                    details=ConnectorHttpClient.truncate_for_log(payload),
                )
            if not payload:
                break
            yield payload
            total_pages = int(response.headers.get('X-WP-TotalPages') or 1)
            if page >= total_pages:
                break
            page += 1
            time.sleep(0.15)

    def iter_records(self, endpoint: str, **kwargs) -> Iterator[dict]:
        for page in self.iter_paginated(endpoint, **kwargs):
            for record in page:
                yield record

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    def get_products(self, **params) -> Iterator[dict]:
        return self.iter_records('/products', params=params)

    def get_product(self, product_id: int) -> dict:
        return self.request_json('GET', f'/products/{int(product_id)}') or {}

    def create_product(self, payload: dict) -> dict:
        return self.request_json('POST', '/products', json_body=payload) or {}

    def update_product(self, product_id: int, payload: dict) -> dict:
        return self.request_json('PUT', f'/products/{int(product_id)}', json_body=payload) or {}

    def get_variations(self, product_id: int, **params) -> Iterator[dict]:
        return self.iter_records(f'/products/{int(product_id)}/variations', params=params)

    def create_variation(self, product_id: int, payload: dict) -> dict:
        return self.request_json(
            'POST', f'/products/{int(product_id)}/variations', json_body=payload,
        ) or {}

    def update_variation(self, product_id: int, variation_id: int, payload: dict) -> dict:
        return self.request_json(
            'PUT',
            f'/products/{int(product_id)}/variations/{int(variation_id)}',
            json_body=payload,
        ) or {}

    # ------------------------------------------------------------------
    # Categories / attributes
    # ------------------------------------------------------------------

    def get_categories(self, **params) -> Iterator[dict]:
        return self.iter_records('/products/categories', params=params)

    def get_category(self, category_id: int) -> dict:
        return self.request_json('GET', f'/products/categories/{int(category_id)}') or {}

    def create_category(self, payload: dict) -> dict:
        return self.request_json('POST', '/products/categories', json_body=payload) or {}

    def update_category(self, category_id: int, payload: dict) -> dict:
        return self.request_json(
            'PUT', f'/products/categories/{int(category_id)}', json_body=payload,
        ) or {}

    def get_tags(self, **params) -> Iterator[dict]:
        return self.iter_records('/products/tags', params=params)

    def get_tag(self, tag_id: int) -> dict:
        return self.request_json('GET', f'/products/tags/{int(tag_id)}') or {}

    def create_tag(self, payload: dict) -> dict:
        return self.request_json('POST', '/products/tags', json_body=payload) or {}

    def update_tag(self, tag_id: int, payload: dict) -> dict:
        return self.request_json(
            'PUT', f'/products/tags/{int(tag_id)}', json_body=payload,
        ) or {}

    def get_attributes(self, **params) -> Iterator[dict]:
        return self.iter_records('/products/attributes', params=params)

    def get_attribute(self, attribute_id: int) -> dict:
        return self.request_json('GET', f'/products/attributes/{int(attribute_id)}') or {}

    def create_attribute(self, payload: dict) -> dict:
        return self.request_json('POST', '/products/attributes', json_body=payload) or {}

    def update_attribute(self, attribute_id: int, payload: dict) -> dict:
        return self.request_json(
            'PUT', f'/products/attributes/{int(attribute_id)}', json_body=payload,
        ) or {}

    def get_attribute_terms(self, attribute_id: int, **params) -> Iterator[dict]:
        return self.iter_records(
            f'/products/attributes/{int(attribute_id)}/terms', params=params,
        )

    def create_attribute_term(self, attribute_id: int, payload: dict) -> dict:
        return self.request_json(
            'POST',
            f'/products/attributes/{int(attribute_id)}/terms',
            json_body=payload,
        ) or {}

    def update_attribute_term(self, attribute_id: int, term_id: int, payload: dict) -> dict:
        return self.request_json(
            'PUT',
            f'/products/attributes/{int(attribute_id)}/terms/{int(term_id)}',
            json_body=payload,
        ) or {}

    # ------------------------------------------------------------------
    # Customers
    # ------------------------------------------------------------------

    def get_customers(self, **params) -> Iterator[dict]:
        return self.iter_records('/customers', params=params)

    def get_customer(self, customer_id: int) -> dict:
        return self.request_json('GET', f'/customers/{int(customer_id)}') or {}

    def create_customer(self, payload: dict) -> dict:
        return self.request_json('POST', '/customers', json_body=payload) or {}

    def update_customer(self, customer_id: int, payload: dict) -> dict:
        return self.request_json(
            'PUT', f'/customers/{int(customer_id)}', json_body=payload,
        ) or {}

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def get_orders(self, **params) -> Iterator[dict]:
        return self.iter_records('/orders', params=params)

    def get_order(self, order_id: int) -> dict:
        return self.request_json('GET', f'/orders/{int(order_id)}') or {}

    def update_order(self, order_id: int, payload: dict) -> dict:
        return self.request_json(
            'PUT', f'/orders/{int(order_id)}', json_body=payload,
        ) or {}

    def get_order_notes(self, order_id: int, **params) -> Iterator[dict]:
        return self.iter_records(f'/orders/{int(order_id)}/notes', params=params)

    def create_order_note(self, order_id: int, payload: dict) -> dict:
        return self.request_json(
            'POST', f'/orders/{int(order_id)}/notes', json_body=payload,
        ) or {}

    def get_order_refunds(self, order_id: int, **params) -> Iterator[dict]:
        return self.iter_records(f'/orders/{int(order_id)}/refunds', params=params)

    def get_order_refund(self, order_id: int, refund_id: int) -> dict:
        return self.request_json(
            'GET', f'/orders/{int(order_id)}/refunds/{int(refund_id)}',
        ) or {}

    # ------------------------------------------------------------------
    # Coupons
    # ------------------------------------------------------------------

    def get_coupons(self, **params) -> Iterator[dict]:
        return self.iter_records('/coupons', params=params)

    def get_coupon(self, coupon_id: int) -> dict:
        return self.request_json('GET', f'/coupons/{int(coupon_id)}') or {}

    def create_coupon(self, payload: dict) -> dict:
        return self.request_json('POST', '/coupons', json_body=payload) or {}

    def update_coupon(self, coupon_id: int, payload: dict) -> dict:
        return self.request_json(
            'PUT', f'/coupons/{int(coupon_id)}', json_body=payload,
        ) or {}

    # ------------------------------------------------------------------
    # Payment / shipping masters
    # ------------------------------------------------------------------

    def get_taxes(self, **params) -> Iterator[dict]:
        return self.iter_records('/taxes', params=params)

    def get_tax(self, tax_id: int) -> dict:
        return self.request_json('GET', f'/taxes/{int(tax_id)}') or {}

    def get_tax_classes(self, **params) -> Iterator[dict]:
        payload = self.request_json('GET', '/taxes/classes', params=params) or []
        if isinstance(payload, list):
            for row in payload:
                yield row
        elif isinstance(payload, dict):
            yield payload

    def get_payment_gateways(self, **params) -> Iterator[dict]:
        # Woo returns a list without classic pagination headers for gateways
        payload = self.request_json('GET', '/payment_gateways', params=params) or []
        if isinstance(payload, list):
            for row in payload:
                yield row
        elif isinstance(payload, dict):
            yield payload

    def get_shipping_methods(self, **params) -> Iterator[dict]:
        payload = self.request_json('GET', '/shipping_methods', params=params) or []
        if isinstance(payload, list):
            for row in payload:
                yield row
        elif isinstance(payload, dict):
            yield payload

    def get_shipping_zones(self, **params) -> Iterator[dict]:
        return self.iter_records('/shipping/zones', params=params)

    def get_shipping_zone_methods(self, zone_id: int, **params) -> Iterator[dict]:
        return self.iter_records(
            f'/shipping/zones/{int(zone_id)}/methods', params=params,
        )

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    def get_webhooks(self, **params) -> Iterator[dict]:
        return self.iter_records('/webhooks', params=params)

    def get_webhook(self, webhook_id: int) -> dict:
        return self.request_json('GET', f'/webhooks/{int(webhook_id)}') or {}

    def create_webhook(self, payload: dict) -> dict:
        return self.request_json('POST', '/webhooks', json_body=payload) or {}

    def update_webhook(self, webhook_id: int, payload: dict) -> dict:
        return self.request_json(
            'PUT', f'/webhooks/{int(webhook_id)}', json_body=payload,
        ) or {}

    def delete_webhook(self, webhook_id: int, *, force: bool = True) -> dict:
        params = {'force': 'true'} if force else None
        return self.request_json(
            'DELETE', f'/webhooks/{int(webhook_id)}', params=params,
        ) or {}

    # ------------------------------------------------------------------
    # System
    # ------------------------------------------------------------------

    def get_system_status(self) -> dict:
        return self.request_json('GET', '/system_status') or {}
