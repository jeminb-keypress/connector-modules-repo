# -*- coding: utf-8 -*-
"""Dedicated WooCommerce customer import/export (res.partner).

Matching order:
1. Existing store binding for res.partner
2. Validated email match (same company, top-level, not bound to another Woo customer on this store)
3. Create new partner

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
        created = updated = skipped = failed = 0
        errors = []

        for woo_customer in api.get_customers(role='all'):
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
        partner.write(vals)
        self._woo_sync_customer_addresses(partner, woo_customer)
        self._woo_bind('res.partner', partner.id, woo_id, external_data={
            'email': woo_customer.get('email'),
            'username': woo_customer.get('username'),
        })
        return result

    def _woo_match_or_create_customer(self, woo_customer):
        """Return (partner, 'created'|'updated') using binding → email → create."""
        self.ensure_one()
        woo_id = woo_customer.get('id')
        existing = self._woo_find_odoo('res.partner', woo_id)
        if existing:
            return existing, 'updated'

        email = (woo_customer.get('email') or '').strip()
        partner = self._woo_find_partner_by_email(email) if email else self.env['res.partner']
        if partner:
            # Do not steal a partner already bound to a different Woo customer on this store
            other = self._woo_binding()._kits_find_binding(
                self, 'res.partner', odoo_record_id=partner.id,
            )
            if other and str(other.external_id) != str(woo_id):
                partner = self.env['res.partner']
            else:
                return partner, 'updated'

        vals = self._woo_prepare_partner_vals(woo_customer)
        if not vals.get('name'):
            vals['name'] = email or f'WooCommerce Customer {woo_id}'
        partner = self.env['res.partner'].with_company(self.company_id).create(vals)
        return partner, 'created'

    def _woo_find_partner_by_email(self, email):
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
        elif company_name and name and company_name.lower() not in name.lower():
            # Odoo 20 dropped res.partner.company_name; keep company in display name
            name = f'{name} ({company_name})'
        email = (woo_customer.get('email') or billing.get('email') or '').strip()
        vals = {
            'name': name or email or False,
            'email': email or False,
            'phone': billing.get('phone') or False,
            'company_id': self.company_id.id,
            'type': 'contact',
        }
        # Prefer billing address on the main contact
        self._woo_apply_address_vals(vals, billing)
        return {k: v for k, v in vals.items() if v is not False or k in ('email', 'phone')}

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

    def _woo_sync_customer_addresses(self, partner, woo_customer):
        """Ensure invoice/delivery child contacts mirror Woo billing/shipping when distinct."""
        self.ensure_one()
        billing = woo_customer.get('billing') or {}
        shipping = woo_customer.get('shipping') or {}
        if self._woo_address_has_content(billing):
            self._woo_upsert_address_child(partner, billing, 'invoice')
        if self._woo_address_has_content(shipping):
            self._woo_upsert_address_child(partner, shipping, 'delivery')

    @staticmethod
    def _woo_address_has_content(address):
        return bool(address) and any(
            address.get(k) for k in ('address_1', 'city', 'postcode', 'first_name', 'company')
        )

    def _woo_upsert_address_child(self, parent, address, address_type):
        self.ensure_one()
        children = parent.child_ids.filtered(lambda c: c.type == address_type)
        name = ' '.join(filter(None, [
            address.get('first_name'),
            address.get('last_name'),
        ])).strip() or parent.name
        vals = {
            'name': name,
            'parent_id': parent.id,
            'type': address_type,
            'phone': address.get('phone') or False,
            'company_id': self.company_id.id,
        }
        self._woo_apply_address_vals(vals, address)
        if children:
            children[0].write(vals)
            return children[0]
        return self.env['res.partner'].create(vals)

    # -------------------------------------------------------------------------
    # Export
    # -------------------------------------------------------------------------

    def _woo_export_customers(self, job=None):
        self.ensure_one()
        api = self._woo_api()
        # Export partners with email that are either bound or flagged company contacts
        Partner = self.env['res.partner']
        domain = [
            ('parent_id', '=', False),
            ('email', '!=', False),
            ('company_id', 'in', [False, self.company_id.id]),
            ('active', '=', True),
        ]
        # Prefer already-bound partners for this store; also include recent commercial partners
        bound_ids = self._woo_binding().search([
            ('instance_id', '=', self.id),
            ('odoo_model', '=', 'res.partner'),
        ]).mapped('odoo_record_id')
        partners = Partner.browse(bound_ids).exists()
        # Cap unbound export candidates to avoid accidental mass create
        extra = Partner.search(domain + [('id', 'not in', partners.ids)], limit=100)
        partners |= extra

        created = updated = skipped = failed = 0
        errors = []
        for partner in partners:
            try:
                result = self._woo_export_one_customer(partner, api)
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f'Partner {partner.id}: {exc}')
                _logger.exception(
                    'WooCommerce customer export failed for partner %s', partner.id,
                )
                self._kits_log_operation(
                    operation='woo_export_customers',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f'res.partner,{partner.id}',
                    job=job,
                )

        self.woo_last_customer_sync = fields.Datetime.now()
        summary = (
            f'Customer export finished: created={created}, updated={updated}, '
            f'skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_export_one_customer(self, partner, api):
        self.ensure_one()
        email = (partner.email or '').strip()
        if not email:
            raise ValueError(self.env._(
                'Partner %(name)s has no email; WooCommerce requires an email.',
                name=partner.display_name,
            ))
        Binding = self._woo_binding()
        binding = Binding._kits_find_binding(self, 'res.partner', odoo_record_id=partner.id)
        first, last = self._woo_split_partner_name(partner.name)
        payload = {
            'email': email,
            'first_name': first,
            'last_name': last,
            'billing': self._woo_partner_address_payload(partner, 'invoice'),
            'shipping': self._woo_partner_address_payload(partner, 'delivery'),
        }
        if partner.phone:
            payload['billing']['phone'] = partner.phone

        if binding:
            remote = api.update_customer(int(binding.external_id), payload)
            result = 'updated'
            woo_id = binding.external_id
        else:
            remote = api.create_customer(payload)
            woo_id = remote.get('id')
            if not woo_id:
                raise ValueError(self.env._('WooCommerce did not return a customer id'))
            result = 'created'

        self._woo_bind('res.partner', partner.id, woo_id, external_data={'email': email})
        return result

    def _woo_partner_address_payload(self, partner, address_type):
        child = partner.child_ids.filtered(lambda c: c.type == address_type)[:1]
        source = child or partner
        first, last = self._woo_split_partner_name(source.name or partner.name)
        return {
            'first_name': first,
            'last_name': last,
            'company': '',
            'address_1': source.street or '',
            'address_2': source.street2 or '',
            'city': source.city or '',
            'postcode': source.zip or '',
            'country': source.country_id.code or '',
            'state': source.state_id.code or '',
            'email': partner.email or '',
            'phone': source.phone or partner.phone or '',
        }

    @staticmethod
    def _woo_split_partner_name(name):
        parts = (name or '').strip().split(None, 1)
        if not parts:
            return '', ''
        if len(parts) == 1:
            return parts[0], ''
        return parts[0], parts[1]

    # -------------------------------------------------------------------------
    # Order-time helpers (guest-safe)
    # -------------------------------------------------------------------------

    def _woo_partner_from_order(self, woo_order):
        """Resolve partner for an order. Guests are never bound as Woo customers."""
        self.ensure_one()
        customer_id = woo_order.get('customer_id') or 0
        try:
            customer_id = int(customer_id)
        except (TypeError, ValueError):
            customer_id = 0

        billing = woo_order.get('billing') or {}

        if customer_id > 0:
            existing = self._woo_find_odoo('res.partner', customer_id)
            if existing:
                return existing
            # Prefer pulling registered customer payload when possible
            try:
                woo_customer = self._woo_api().get_customer(customer_id)
                if woo_customer.get('id'):
                    partner, _result = self._woo_match_or_create_customer(woo_customer)
                    self._woo_sync_customer_addresses(partner, woo_customer)
                    self._woo_bind('res.partner', partner.id, customer_id, external_data={
                        'email': woo_customer.get('email'),
                        'from_order': woo_order.get('id'),
                    })
                    return partner
            except Exception:  # noqa: BLE001
                _logger.info(
                    'Falling back to order billing for Woo customer %s on instance %s',
                    customer_id, self.id,
                )

        # Guest or unreachable customer: create/find by email without customer binding
        email = (billing.get('email') or '').strip()
        if email:
            partner = self._woo_find_partner_by_email(email)
            if partner:
                return partner

        name = ' '.join(filter(None, [
            billing.get('first_name'),
            billing.get('last_name'),
        ])).strip() or email or f"WooCommerce Guest {woo_order.get('id')}"
        vals = {
            'name': name,
            'email': email or False,
            'phone': billing.get('phone') or False,
            'company_id': self.company_id.id,
            'type': 'contact',
        }
        if billing.get('company') and billing.get('company') not in (name or ''):
            vals['name'] = f"{name} ({billing.get('company')})"
        self._woo_apply_address_vals(vals, billing)
        partner = self.env['res.partner'].with_company(self.company_id).create(vals)
        # Bind only registered customers
        if customer_id > 0:
            self._woo_bind('res.partner', partner.id, customer_id, external_data={
                'email': email,
                'from_order': woo_order.get('id'),
            })
        return partner
