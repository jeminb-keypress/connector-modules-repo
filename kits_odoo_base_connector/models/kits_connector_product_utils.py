# -*- coding: utf-8 -*-
"""Reusable product / image / attribute helpers for platform connectors."""

import base64
import hashlib
import logging

from odoo import models

_logger = logging.getLogger(__name__)


class KitsConnectorProductUtils(models.AbstractModel):
    _name = 'kits.connector.product.utils'
    _description = 'Kits Connector Product Utilities'

    @staticmethod
    def _kits_image_sha1(raw_bytes):
        """Return hex SHA1 of raw image bytes, or False."""
        if not raw_bytes:
            return False
        if isinstance(raw_bytes, str):
            try:
                raw_bytes = base64.b64decode(raw_bytes)
            except Exception:  # noqa: BLE001
                return False
        return hashlib.sha1(raw_bytes).hexdigest()

    @staticmethod
    def _kits_b64_str(raw_bytes):
        """Normalize bytes / BinaryValue / b64 str to a base64 ASCII string."""
        if raw_bytes is None or raw_bytes is False:
            return False
        if isinstance(raw_bytes, memoryview):
            raw_bytes = raw_bytes.tobytes()
        if isinstance(raw_bytes, bytes):
            return base64.b64encode(raw_bytes).decode('ascii')
        if isinstance(raw_bytes, str):
            return raw_bytes
        try:
            return base64.b64encode(bytes(raw_bytes)).decode('ascii')
        except Exception:  # noqa: BLE001
            return False

    def _kits_should_skip_image(self, existing_hash, new_bytes):
        """True when the new image matches the stored hash (no rewrite needed)."""
        new_hash = self._kits_image_sha1(new_bytes)
        return bool(existing_hash and new_hash and existing_hash == new_hash)

    def _kits_ensure_attribute(self, name, *, create_variant='always'):
        """Find or create a product.attribute by name."""
        Attribute = self.env['product.attribute']
        name = (name or '').strip()
        if not name:
            return Attribute
        attribute = Attribute.search([('name', '=', name)], limit=1)
        if attribute:
            return attribute
        return Attribute.create({
            'name': name,
            'create_variant': create_variant,
        })

    def _kits_ensure_attribute_value(self, attribute, value_name):
        """Find or create a product.attribute.value under ``attribute``."""
        Value = self.env['product.attribute.value']
        value_name = (value_name or '').strip()
        if not attribute or not value_name:
            return Value
        value = Value.search([
            ('attribute_id', '=', attribute.id),
            ('name', '=', value_name),
        ], limit=1)
        if value:
            return value
        return Value.create({
            'attribute_id': attribute.id,
            'name': value_name,
        })

    def _kits_pricelist_price(self, pricelist, product, quantity=1.0):
        """Return the pricelist price for ``product``, or list_price fallback."""
        if not product:
            return 0.0
        if pricelist:
            try:
                return pricelist._get_product_price(product, quantity)
            except Exception:  # noqa: BLE001
                _logger.debug('Pricelist price lookup failed', exc_info=True)
        return float(getattr(product, 'list_price', 0.0) or 0.0)
