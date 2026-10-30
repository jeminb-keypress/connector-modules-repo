# -*- coding: utf-8 -*-
"""Dedicated WooCommerce customer import (res.partner). Odoo→Woo customer export is not supported.

Matching for registered Woo customers (per instance):
1. Existing binding for this instance + Woo customer ID → update that partner
2. Otherwise create a **new** Odoo partner and bind it

Never merge across instances (or onto an unbound partner) using only email/phone.
Guest checkout (customer_id 0 / missing) must not create customer bindings — handled by orders.
"""

import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class KitsConnectorInstanceCustomer(models.Model):
    _inherit = 'kits.connector.instance'

    def _woo_import_customers(self, job=None):
        self.ensure_one()
        api = self._woo_api()
        payload = (job.payload if job else None) or {}
        woo_ids = self._woo_parse_id_list(payload.get('woo_ids'))
        modified_after = payload.get('modified_after')
        created = updated = skipped = failed = 0
        errors = []

        if woo_ids:
            customers = []
            for cid in woo_ids:
                try:
                    remote = api.get_customer(cid)
                    if remote.get('id'):
                        customers.append(remote)
                    else:
                        skipped += 1
                except Exception as exc:  # noqa: BLE001
                    if self._woo_is_not_found(exc):
                        binding = self._woo_binding()._kits_find_binding(
                            self, 'res.partner', external_id=str(cid),
                        )
                        if binding:
                            binding._kits_mark_deleted_remote(message=self.env._(
                                'WooCommerce customer %(id)s was not found. '
                                'The Odoo contact was kept.',
                                id=cid,
                            ))
                        skipped += 1
                        continue
                    failed += 1
                    errors.append(f'Customer {cid}: {exc}')
            customer_iter = customers
        else:
            params = {'role': 'all'}
            if modified_after:
                params['modified_after'] = str(modified_after).replace(' ', 'T')
            customer_iter = api.get_customers(**params)

        for woo_customer in customer_iter:
            try:
                result = self._woo_import_one_customer(woo_customer, job=job)
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f"Customer {woo_customer.get('id')}: {exc}")
                _logger.exception(
                    'WooCommerce customer import failed for %s on instance %s',
                    woo_customer.get('id'), self.id,
                )
                self._kits_log_operation(
                    operation='woo_import_customers',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f"woo_customer_id={woo_customer.get('id')}",
                    job=job,
                )

        self.woo_last_customer_sync = fields.Datetime.now()
        summary = (
            f'Customer import finished: created={created}, updated={updated}, '
            f'skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_import_one_customer(self, woo_customer, job=None):
        self.ensure_one()
        woo_id = woo_customer.get('id')
        if not woo_id:
            return 'skipped'

        partner, result = self._woo_match_or_create_customer(woo_customer)
        if not partner:
            return 'skipped'

        vals = self._woo_prepare_partner_vals(woo_customer)
        if result == 'created' and woo_customer.get('username'):
            vals['ref'] = woo_customer.get('username')
        partner.with_context(kits_woo_skip_dirty=True).write(vals)
        self._woo_bind(
            'res.partner', partner.id, woo_id,
            external_data=self._woo_customer_external_data(woo_customer),
        )
        return result

    def _woo_match_or_create_customer(self, woo_customer):
        """Return (partner, 'created'|'updated') using binding only, then create.

        Same email/phone on a different instance (or an unbound Odoo contact) must
        not reuse that record — each instance mapping gets its own partner.
        """
        self.ensure_one()
        woo_id = woo_customer.get('id')
        existing = self._woo_find_odoo('res.partner', woo_id)
        if existing:
            return existing, 'updated'

        vals = self._woo_prepare_partner_vals(woo_customer)
        email = (woo_customer.get('email') or '').strip()
        if not vals.get('name'):
            vals['name'] = email or f'WooCommerce Customer {woo_id}'
        partner = self.env['res.partner'].with_company(self.company_id).with_context(
            kits_woo_skip_dirty=True,
        ).create(vals)
        return partner, 'created'

    def _woo_find_partner_by_email(self, email):
        """Guest/order helper only — registered customer sync does not merge by email."""
        self.ensure_one()
        if not email:
            return self.env['res.partner']
        return self.env['res.partner'].with_company(self.company_id).search([
            ('email', '=ilike', email),
            ('parent_id', '=', False),
            ('company_id', 'in', [False, self.company_id.id]),
        ], limit=1)

    def _woo_prepare_partner_vals(self, woo_customer):
        billing = woo_customer.get('billing') or {}
        name = ' '.join(filter(None, [
            woo_customer.get('first_name') or billing.get('first_name'),
            woo_customer.get('last_name') or billing.get('last_name'),
        ])).strip()
        company_name = (billing.get('company') or '').strip()
        if company_name and not name:
            name = company_name
            company_name = ''
        elif company_name == name:
            company_name = ''
        email = (woo_customer.get('email') or billing.get('email') or '').strip()
        mobile = self._woo_meta_value(
            woo_customer, 'mobile', 'billing_mobile', '_billing_mobile',
        )
        vals = {
            'name': name or email or False,
            'email': email or False,
            'phone': billing.get('phone') or False,
            'company_id': self.company_id.id,
            'type': 'contact',
            'woo_company_name': company_name or False,
        }
        # Odoo 20 base res.partner has phone only. Set mobile when the field exists.
        if mobile and 'mobile' in self.env['res.partner']._fields:
            vals['mobile'] = mobile
        self._woo_apply_address_vals(vals, billing)
        keep_empty = ('email', 'phone', 'mobile', 'woo_company_name')
        return {k: v for k, v in vals.items() if v is not False or k in keep_empty}

    def _woo_customer_external_data(self, woo_customer):
        billing = (woo_customer or {}).get('billing') or {}
        email = (woo_customer.get('email') or billing.get('email') or '').strip()
        return {
            'email': email or False,
            'company': (billing.get('company') or '').strip() or False,
            'date_modified': self._woo_normalize_remote_datetime(
                woo_customer.get('date_modified_gmt') or woo_customer.get('date_modified')
            ) or False,
            'meta': self._woo_meta_snapshot(woo_customer),
            'mobile': self._woo_meta_value(
                woo_customer, 'mobile', 'billing_mobile', '_billing_mobile',
            ) or False,
        }

    def _woo_apply_address_vals(self, vals, address):
        if not address:
            return vals
        vals.update({
            'street': address.get('address_1') or vals.get('street') or False,
            'street2': address.get('address_2') or vals.get('street2') or False,
            'city': address.get('city') or vals.get('city') or False,
            'zip': address.get('postcode') or vals.get('zip') or False,
        })
        country_code = (address.get('country') or '').upper()
        if country_code:
            country = self.env['res.country'].search([('code', '=', country_code)], limit=1)
            if country:
                vals['country_id'] = country.id
                state_code = address.get('state')
                if state_code:
                    state = self.env['res.country.state'].search([
                        ('country_id', '=', country.id),
                        ('code', '=', state_code),
                    ], limit=1)
                    if state:
                        vals['state_id'] = state.id
        return vals

    # -------------------------------------------------------------------------
    # Fetch (Woo → Odoo by id)
    # -------------------------------------------------------------------------

    def _woo_fetch_customer_by_id(self, woo_customer_id):
        """Fetch one Woo customer into Odoo (create or update by instance binding).

        Returns the partner recordset.
        """
        self.ensure_one()
        self._ensure_woo_ready()
        woo_customer_id = int(woo_customer_id)
        remote = self._woo_api().get_customer(woo_customer_id)
        if not remote or not remote.get('id'):
            raise ValueError(self.env._(
                'WooCommerce customer %(id)s was not found on %(store)s.',
                id=woo_customer_id,
                store=self.name,
            ))
        result = self._woo_import_one_customer(remote)
        partner = self._woo_find_odoo('res.partner', remote['id'])
        if not partner:
            raise ValueError(self.env._(
                'Customer fetch finished with status %(status)s but no partner was bound.',
                status=result,
            ))
        return partner

    # -------------------------------------------------------------------------
    # Order-time helpers (guest-safe)
    # -------------------------------------------------------------------------

    def _woo_partner_from_order(self, woo_order):
        """Resolve partner for an order. Guests never get customer bindings."""
        self.ensure_one()
        customer_id = woo_order.get('customer_id') or 0
        try:
            customer_id = int(customer_id)
        except (TypeError, ValueError):
            customer_id = 0

        billing = woo_order.get('billing') or {}

        if customer_id > 0:
            try:
                woo_customer = self._woo_api().get_customer(customer_id)
                if woo_customer.get('id'):
                    self._woo_import_one_customer(woo_customer)
                    partner = self._woo_find_odoo('res.partner', woo_customer['id'])
                    if partner:
                        return partner
            except Exception:  # noqa: BLE001
                _logger.info(
                    'Falling back to order billing for Woo customer %s on instance %s',
                    customer_id, self.id,
                )

        # Guest or unreachable customer: create/find by email without binding
        email = (billing.get('email') or '').strip()
        if email:
            partner = self._woo_find_partner_by_email(email)
            if partner:
                return partner

        fallback = email or f"WooCommerce Guest {woo_order.get('id')}"
        vals = self._woo_prepare_partner_vals({
            'email': email,
            'billing': billing,
        })
        if not vals.get('name'):
            vals['name'] = fallback
        return self.env['res.partner'].with_company(self.company_id).with_context(
            kits_woo_skip_dirty=True,
        ).create(vals)
