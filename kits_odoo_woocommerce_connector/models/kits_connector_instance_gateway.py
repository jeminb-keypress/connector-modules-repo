# -*- coding: utf-8 -*-
"""Fetch Woo payment gateways / shipping methods and apply mappings on orders."""

import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class KitsConnectorInstanceGateway(models.Model):
    _inherit = 'kits.connector.instance'

    woo_payment_gateway_map_ids = fields.One2many(
        'kits.woo.payment.gateway.map',
        'instance_id',
        string='Payment Gateway Mappings',
    )
    woo_shipping_method_map_ids = fields.One2many(
        'kits.woo.shipping.method.map',
        'instance_id',
        string='Shipping Method Mappings',
    )

    def action_woo_fetch_payment_gateways(self):
        self._ensure_woo_ready()
        for instance in self:
            instance._woo_fetch_payment_gateways()
        return self._woo_notify(self.env._('Payment gateways fetched from WooCommerce.'))

    def action_woo_fetch_shipping_methods(self):
        self._ensure_woo_ready()
        for instance in self:
            instance._woo_fetch_shipping_methods()
        return self._woo_notify(self.env._('Shipping methods fetched from WooCommerce.'))

    def _woo_fetch_payment_gateways(self):
        self.ensure_one()
        api = self._woo_api()
        Map = self.env['kits.woo.payment.gateway.map']
        created = updated = 0
        for gateway in api.get_payment_gateways():
            gateway_id = gateway.get('id')
            if not gateway_id:
                continue
            existing = Map.search([
                ('instance_id', '=', self.id),
                ('woo_gateway_id', '=', str(gateway_id)),
            ], limit=1)
            vals = {
                'woo_title': gateway.get('title') or gateway.get('method_title') or False,
                'woo_enabled': bool(gateway.get('enabled')),
            }
            if existing:
                existing.write(vals)
                updated += 1
            else:
                Map.create({
                    'instance_id': self.id,
                    'woo_gateway_id': str(gateway_id),
                    **vals,
                })
                created += 1
        self._kits_log_operation(
            operation='woo_fetch_payment_gateways',
            status='success',
            message=self.env._(
                'Fetched payment gateways: created=%(c)s updated=%(u)s',
                c=created, u=updated,
            ),
        )
        return created, updated

    def _woo_fetch_shipping_methods(self):
        self.ensure_one()
        api = self._woo_api()
        Map = self.env['kits.woo.shipping.method.map']
        created = updated = 0
        seen = set()

        # Global method definitions
        for method in api.get_shipping_methods():
            method_id = method.get('id')
            if not method_id:
                continue
            seen.add(str(method_id))
            existing = Map.search([
                ('instance_id', '=', self.id),
                ('woo_method_id', '=', str(method_id)),
            ], limit=1)
            vals = {'woo_method_title': method.get('title') or method.get('method_title') or False}
            if existing:
                existing.write(vals)
                updated += 1
            else:
                Map.create({
                    'instance_id': self.id,
                    'woo_method_id': str(method_id),
                    **vals,
                })
                created += 1

        # Zone-specific instances often expose method_id like flat_rate:3 — also capture base ids
        try:
            for zone in api.get_shipping_zones():
                zone_id = zone.get('id')
                if zone_id is None:
                    continue
                for method in api.get_shipping_zone_methods(zone_id):
                    method_id = method.get('method_id') or method.get('id')
                    if not method_id:
                        continue
                    method_id = str(method_id)
                    if method_id in seen:
                        # Refresh title if richer
                        existing = Map.search([
                            ('instance_id', '=', self.id),
                            ('woo_method_id', '=', method_id),
                        ], limit=1)
                        title = method.get('title') or method.get('method_title')
                        if existing and title and existing.woo_method_title != title:
                            existing.woo_method_title = title
                        continue
                    seen.add(method_id)
                    existing = Map.search([
                        ('instance_id', '=', self.id),
                        ('woo_method_id', '=', method_id),
                    ], limit=1)
                    vals = {
                        'woo_method_title': method.get('title') or method.get('method_title') or False,
                    }
                    if existing:
                        existing.write(vals)
                        updated += 1
                    else:
                        Map.create({
                            'instance_id': self.id,
                            'woo_method_id': method_id,
                            **vals,
                        })
                        created += 1
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                'Shipping zone method fetch partially failed for instance %s: %s',
                self.id, exc,
            )

        self._kits_log_operation(
            operation='woo_fetch_shipping_methods',
            status='success',
            message=self.env._(
                'Fetched shipping methods: created=%(c)s updated=%(u)s',
                c=created, u=updated,
            ),
        )
        return created, updated

    def _woo_find_payment_gateway_map(self, woo_payment_method):
        self.ensure_one()
        if not woo_payment_method:
            return self.env['kits.woo.payment.gateway.map']
        return self.env['kits.woo.payment.gateway.map'].search([
            ('instance_id', '=', self.id),
            ('woo_gateway_id', '=', str(woo_payment_method)),
            ('active', '=', True),
        ], limit=1)

    def _woo_find_shipping_method_map(self, woo_method_id):
        self.ensure_one()
        if not woo_method_id:
            return self.env['kits.woo.shipping.method.map']
        method_id = str(woo_method_id)
        Map = self.env['kits.woo.shipping.method.map']
        found = Map.search([
            ('instance_id', '=', self.id),
            ('woo_method_id', '=', method_id),
            ('active', '=', True),
        ], limit=1)
        if found:
            return found
        # flat_rate:12 → flat_rate
        if ':' in method_id:
            base = method_id.split(':', 1)[0]
            return Map.search([
                ('instance_id', '=', self.id),
                ('woo_method_id', '=', base),
                ('active', '=', True),
            ], limit=1)
        return Map
