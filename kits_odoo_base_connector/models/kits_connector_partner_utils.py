# -*- coding: utf-8 -*-
"""Reusable contact / address helpers for platform connectors."""

from odoo import models


class KitsConnectorPartnerUtils(models.AbstractModel):
    _name = 'kits.connector.partner.utils'
    _description = 'Kits Connector Partner Utilities'

    @staticmethod
    def _kits_normalize_email(email):
        return (email or '').strip().lower()

    @staticmethod
    def _kits_address_text(value):
        return (value or '').strip().casefold()

    def _kits_find_partners_by_email(self, email, *, company=None):
        """Top-level contacts with this normalized email (company-scoped)."""
        email = self._kits_normalize_email(email)
        if not email:
            return self.env['res.partner']
        company = company or self.env.company
        candidates = self.env['res.partner'].with_company(company).search([
            ('email', '=ilike', email),
            ('parent_id', '=', False),
            ('company_id', 'in', [False, company.id]),
        ], order='id')
        return candidates.filtered(
            lambda partner: self._kits_normalize_email(partner.email) == email
        )

    def _kits_find_partner_by_email(self, email, *, company=None):
        """Unambiguous email match, or empty if none / multiple."""
        partners = self._kits_find_partners_by_email(email, company=company)
        if len(partners) == 1:
            return partners
        return self.env['res.partner']

    def _kits_resolve_country_state(self, country_code, state_code=False):
        """Return ``(country, state)`` recordsets from ISO-like codes."""
        country = self.env['res.country']
        state = self.env['res.country.state']
        code = (country_code or '').upper().strip()
        if not code:
            return country, state
        country = self.env['res.country'].search([('code', '=', code)], limit=1)
        if country and state_code:
            state = self.env['res.country.state'].search([
                ('country_id', '=', country.id),
                ('code', '=', state_code),
            ], limit=1)
        return country, state

    def _kits_apply_address_vals(self, vals, address):
        """Merge street/city/zip/country/state from a platform address dict into vals."""
        address = address or {}
        vals.update({
            'street': address.get('address_1') or address.get('street') or vals.get('street') or False,
            'street2': address.get('address_2') or address.get('street2') or vals.get('street2') or False,
            'city': address.get('city') or vals.get('city') or False,
            'zip': address.get('postcode') or address.get('zip') or vals.get('zip') or False,
        })
        country, state = self._kits_resolve_country_state(
            address.get('country'), address.get('state'),
        )
        if country:
            vals['country_id'] = country.id
            if state:
                vals['state_id'] = state.id
        return vals

    def _kits_address_matches(self, child, vals, *, compare_phone=False):
        """Exact child-address match used when reusing invoice/delivery contacts."""
        if self._kits_address_text(child.name) != self._kits_address_text(vals.get('name')):
            return False
        for key in ('street', 'street2', 'city', 'zip'):
            if self._kits_address_text(child[key]) != self._kits_address_text(vals.get(key)):
                return False
        if (child.state_id.id or False) != (vals.get('state_id') or False):
            return False
        if (child.country_id.id or False) != (vals.get('country_id') or False):
            return False
        if compare_phone and self._kits_address_text(child.phone) != self._kits_address_text(
            vals.get('phone')
        ):
            return False
        return True
