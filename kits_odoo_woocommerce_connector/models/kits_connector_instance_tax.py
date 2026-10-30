# -*- coding: utf-8 -*-
"""Fetch Woo tax rates and resolve mappings during order import."""

import logging

from odoo import fields, models
from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tools.float_utils import float_compare, float_is_zero

_logger = logging.getLogger(__name__)


class KitsConnectorInstanceTax(models.Model):
    _inherit = 'kits.connector.instance'

    woo_tax_map_ids = fields.One2many(
        'kits.woo.tax.map',
        'instance_id',
        string='Tax Mappings',
    )
    woo_tax_mapping_mode = fields.Selection(
        selection=[
            ('mapped', 'Require Mapped Taxes'),
            ('fallback_clear', 'Fallback: Clear Line Taxes'),
            ('fallback_product', 'Fallback: Keep Product Taxes'),
        ],
        string='Tax Mapping Mode',
        default='fallback_clear',
        required=True,
        help='mapped: fail order import when Woo taxes cannot be mapped (recommended '
             'once Tax Mappings are configured). '
             'fallback_clear: clear Odoo taxes when unmapped (legacy safe anti-double-tax). '
             'fallback_product: keep product default taxes when unmapped (may double-tax).',
    )
    woo_tax_total_tolerance = fields.Float(
        string='Tax Total Tolerance',
        default=0.05,
        help='Allowed absolute difference between Woo and Odoo order tax totals '
             'before a discrepancy is logged on the sales order.',
    )

    def action_woo_fetch_tax_rates(self):
        self._ensure_woo_ready()
        for instance in self:
            instance._woo_fetch_tax_rates()
        return self._woo_notify(self.env._('Tax rates fetched from WooCommerce.'))

    def _woo_fetch_tax_rates(self):
        self.ensure_one()
        api = self._woo_api()
        Map = self.env['kits.woo.tax.map']
        created = updated = 0
        for rate in api.get_taxes():
            rate_id = rate.get('id')
            if not rate_id:
                continue
            existing = Map.search([
                ('instance_id', '=', self.id),
                ('woo_rate_id', '=', str(rate_id)),
            ], limit=1)
            tax_class = rate.get('class') or ''
            vals = {
                'woo_tax_class': tax_class,
                'woo_rate': float(rate.get('rate') or 0.0),
                'woo_name': rate.get('name') or False,
                'name': rate.get('name') or f'Tax {rate_id}',
            }
            if existing:
                # Never overwrite administrator tax_id / zero-rated choice
                existing.write(vals)
                updated += 1
            else:
                Map.create({
                    'instance_id': self.id,
                    'woo_rate_id': str(rate_id),
                    'is_zero_rated': float_is_zero(
                        float(rate.get('rate') or 0.0), precision_digits=4,
                    ),
                    **vals,
                })
                created += 1
        self._kits_log_operation(
            operation='woo_fetch_tax_rates',
            status='success',
            message=self.env._(
                'Fetched tax rates: created=%(c)s updated=%(u)s',
                c=created, u=updated,
            ),
        )
        return created, updated

    def _woo_find_tax_maps(self, *, rate_ids=None, tax_class=None, prices_include_tax=False):
        """Return active tax maps matching Woo rate ids and/or tax class."""
        self.ensure_one()
        Map = self.env['kits.woo.tax.map']
        domain = [('instance_id', '=', self.id), ('active', '=', True)]
        include_key = 'included' if prices_include_tax else 'excluded'
        maps = Map.search(domain)
        maps = maps.filtered(
            lambda m: m.match_price_include in ('any', include_key)
        )
        rate_ids = [str(r) for r in (rate_ids or []) if r not in (None, False, '')]
        matched = self.env['kits.woo.tax.map']
        if rate_ids:
            # Exact rate-id match only — never fall back to class when ids were given
            matched = maps.filtered(lambda m: m.woo_rate_id and str(m.woo_rate_id) in rate_ids)
            return matched
        if tax_class is not None:
            cls = tax_class or ''
            matched = maps.filtered(
                lambda m, cls=cls: (m.woo_tax_class or '') == cls and not (
                    m.woo_rate_id and str(m.woo_rate_id).isdigit()
                )
            ) or maps.filtered(lambda m, cls=cls: (m.woo_tax_class or '') == cls)
        return matched

    def _woo_resolve_line_taxes(self, item, woo_order):
        """Resolve Odoo taxes for one Woo line_item. Returns (tax_recordset, error_or_False)."""
        self.ensure_one()
        prices_include_tax = bool(woo_order.get('prices_include_tax'))
        tax_class = item.get('tax_class')
        if tax_class is None:
            tax_class = ''
        woo_taxes = item.get('taxes') or []
        rate_ids = [t.get('id') for t in woo_taxes if t.get('id')]
        # Explicit no-tax line
        line_tax_total = sum(float(t.get('total') or 0.0) for t in woo_taxes)
        if not rate_ids and float_is_zero(line_tax_total, precision_digits=4):
            # Prefer explicit zero/exempt map for class; otherwise no tax
            zero_maps = self._woo_find_tax_maps(
                tax_class=tax_class, prices_include_tax=prices_include_tax,
            ).filtered('is_zero_rated')
            if zero_maps:
                return self.env['account.tax'], False
            if not woo_taxes:
                return self.env['account.tax'], False

        maps = self.env['kits.woo.tax.map']
        if rate_ids:
            for rid in rate_ids:
                maps |= self._woo_find_tax_maps(
                    rate_ids=[rid],
                    prices_include_tax=prices_include_tax,
                )
        else:
            maps = self._woo_find_tax_maps(
                tax_class=tax_class,
                prices_include_tax=prices_include_tax,
            )

        if not maps:
            return self.env['account.tax'], self.env._(
                'No tax mapping for Woo rate ids=%(ids)s class=%(cls)s on store "%(store)s". '
                'Configure Tax Mappings or change Tax Mapping Mode.',
                ids=rate_ids or [],
                cls=tax_class or 'standard',
                store=self.name,
            )

        unconfigured = maps.filtered(lambda m: not m.is_configured())
        if unconfigured:
            return self.env['account.tax'], self.env._(
                'Tax mapping row(s) for rates %(ids)s exist but are incomplete '
                '(set an Odoo sales tax or Zero/Exempt).',
                ids=rate_ids or unconfigured.mapped('woo_rate_id'),
            )

        if any(m.is_zero_rated for m in maps) and any(not m.is_zero_rated for m in maps):
            return self.env['account.tax'], self.env._(
                'Ambiguous tax mappings (zero-rated mixed with taxed) for rates %(ids)s.',
                ids=rate_ids,
            )

        if all(m.is_zero_rated for m in maps):
            return self.env['account.tax'], False

        taxes = maps.mapped('tax_id').filtered(lambda t: t and t.active)
        if not taxes:
            return self.env['account.tax'], self.env._(
                'Tax mapping rows exist for rates %(ids)s but no Odoo tax is set '
                '(and Zero/Exempt is not enabled).',
                ids=rate_ids,
            )
        # Deduplicate while preserving order
        seen = set()
        ordered = self.env['account.tax']
        for tax in taxes:
            if tax.id not in seen:
                seen.add(tax.id)
                ordered |= tax
        return ordered, False

    def _woo_apply_order_taxes(self, order, woo_order, line_items_meta):
        """Apply mapped taxes to order lines and report total discrepancies.

        ``line_items_meta`` is a list of dicts ``{line, item}`` for product lines only.
        """
        self.ensure_one()
        mode = self.woo_tax_mapping_mode or 'mapped'
        prices_include_tax = bool(woo_order.get('prices_include_tax'))
        errors = []

        for meta in line_items_meta:
            line = meta['line']
            item = meta['item']
            taxes, err = self._woo_resolve_line_taxes(item, woo_order)
            if err:
                if mode == 'mapped':
                    errors.append(err)
                    continue
                if mode == 'fallback_clear':
                    line.tax_ids = [Command.clear()]
                    continue
                # fallback_product: leave product taxes as computed by Odoo
                continue
            line.tax_ids = [Command.set(taxes.ids)] if taxes else [Command.clear()]

        if errors:
            raise UserError(self.env._(
                'WooCommerce order %(woo_id)s tax mapping failed:\n%(errors)s',
                woo_id=woo_order.get('id'),
                errors='\n'.join(errors[:15]),
            ))

        if prices_include_tax and mode == 'fallback_clear' and not self.woo_tax_map_ids:
            # Legacy behavior preserved when no maps configured and fallback_clear
            for line in order.order_line:
                if line.tax_ids:
                    line.tax_ids = [Command.clear()]

        self._woo_report_tax_total_discrepancy(order, woo_order)

    def _woo_report_tax_total_discrepancy(self, order, woo_order):
        self.ensure_one()
        woo_tax = float(woo_order.get('total_tax') or 0.0)
        odoo_tax = order.amount_tax
        tolerance = abs(self.woo_tax_total_tolerance or 0.05)
        currency = order.currency_id
        prec = currency.decimal_places if currency else 2
        if float_compare(abs(woo_tax - odoo_tax), tolerance, precision_digits=prec) > 0:
            msg = self.env._(
                'Tax total discrepancy: Woo=%(woo).2f Odoo=%(odoo).2f '
                '(tolerance=%(tol).2f). Totals were not silently adjusted.',
                woo=woo_tax,
                odoo=odoo_tax,
                tol=tolerance,
            )
            order.woo_sync_conflict = (
                f'{order.woo_sync_conflict}\n{msg}' if order.woo_sync_conflict else msg
            )
            self._kits_log_operation(
                operation='woo_import_orders',
                status='failure',
                level='warning',
                message=msg,
                details=f"woo_order_id={woo_order.get('id')}",
            )
