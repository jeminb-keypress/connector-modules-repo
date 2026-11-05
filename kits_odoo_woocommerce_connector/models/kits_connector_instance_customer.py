# -*- coding: utf-8 -*-
"""Dedicated WooCommerce customer import (res.partner). Odoo→Woo customer export is not supported.

Matching for registered Woo customers:
1. Customer binding for this store + external customer id → reuse the contact, do not change it
2. Else one Odoo contact with the same email, any store → reuse it and add a binding for this store
3. Else create the contact and its binding
4. Multiple email matches → ambiguous (manual review; never create another contact)
5. No person name → skip and log
6. Only WooCommerce role ``customer`` is imported (bulk, Fetch, order, webhooks)

Billing and shipping addresses are child contacts. An exact match is reused.
Existing address and contact fields are never overwritten.
Guest checkout (customer_id 0) does not create a customer binding.
"""

import logging
from datetime import datetime

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Keep in sync with kits.woo.customer.waiting.WAITING_PROCESS_BATCH
CUSTOMER_WAITING_PROCESS_BATCH = 50


class KitsConnectorInstanceCustomer(models.Model):
    _inherit = 'kits.connector.instance'

    def _woo_customer_role_allowed(self, customer, *, allow_missing=False):
        """Only WooCommerce users with role ``customer`` become store customers."""
        role = (customer or {}).get('role')
        if role is None or role == '':
            return bool(allow_missing)
        return str(role).strip().lower() == 'customer'

    def _woo_customer_remote_modified(self, woo_customer):
        """Parse Woo date_modified into an Odoo datetime, or False."""
        raw = (woo_customer or {}).get('date_modified_gmt') or (woo_customer or {}).get(
            'date_modified'
        )
        if not raw:
            return False
        text = str(raw).replace('T', ' ').replace('Z', '')
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
            try:
                return fields.Datetime.to_datetime(datetime.strptime(text[:26], fmt))
            except (TypeError, ValueError):
                continue
        try:
            return fields.Datetime.to_datetime(text)
        except Exception:  # noqa: BLE001
            return False

    def _woo_log_skipped_role(self, woo_customer, *, path='import'):
        woo_id = (woo_customer or {}).get('id')
        role = (woo_customer or {}).get('role') or '(missing)'
        self._woo_log_customer(
            self.env._(
                'Skipped WooCommerce user %(id)s on %(store)s (%(path)s): role '
                '"%(role)s" is not "customer".',
                id=woo_id,
                store=self.display_name,
                path=path,
                role=role,
            ),
            'info',
        )

    def _woo_import_customers(self, job=None):
        """Paginate Woo customers into the waiting list, then process one batch.

        Partners are not created during API pagination. Matching runs in the
        waiting-list processor. At most 50 pending rows are processed here; the
        rest stay pending for cron / follow-up process jobs.
        """
        self.ensure_one()
        api = self._woo_api()
        payload = (job.payload if job else None) or {}
        woo_ids = self._woo_parse_id_list(payload.get('woo_ids'))
        modified_after = payload.get('modified_after')
        queued = skipped = failed = 0
        errors = []
        Waiting = self.env['kits.woo.customer.waiting']

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
                        self._woo_delete_customer_binding(cid, reason=self.env._(
                            'WooCommerce customer %(id)s was not found. The Odoo contact was kept.',
                            id=cid,
                        ))
                        skipped += 1
                        continue
                    failed += 1
                    errors.append(f'Customer {cid}: {exc}')
            customer_iter = customers
        else:
            params = {'role': 'customer'}
            if modified_after:
                params['modified_after'] = str(modified_after).replace(' ', 'T')
            customer_iter = api.get_customers(**params)

        for woo_customer in customer_iter:
            woo_id = woo_customer.get('id')
            if not woo_id:
                skipped += 1
                continue
            if not self._woo_customer_role_allowed(woo_customer):
                skipped += 1
                self._woo_log_skipped_role(woo_customer, path='bulk import')
                continue
            if not self._woo_customer_person_name(woo_customer):
                skipped += 1
                self._woo_log_customer(
                    self.env._(
                        'Skipped WooCommerce customer %(id)s on %(store)s because it has no first or last name.',
                        id=woo_id,
                        store=self.display_name,
                    ),
                    'warning',
                )
                continue
            try:
                Waiting._enqueue(
                    self,
                    external_id=woo_id,
                    payload=woo_customer,
                    source='bulk_import',
                    event_type='update',
                    trigger_batch=False,
                )
                queued += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f'Customer {woo_id}: {exc}')
                _logger.exception(
                    'WooCommerce customer enqueue failed for %s on instance %s',
                    woo_id, self.id,
                )
                self._kits_log_operation(
                    operation='woo_import_customers',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f"woo_customer_id={woo_id}",
                    job=job,
                )

        process_summary = Waiting._process_pending_for_instance(
            self, limit=CUSTOMER_WAITING_PROCESS_BATCH, job=job,
        )
        self.woo_last_customer_sync = fields.Datetime.now()
        summary = (
            f'Customer import finished: queued={queued}, skipped={skipped}, '
            f'failed={failed}. {process_summary}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_import_one_customer(self, woo_customer, job=None):
        self.ensure_one()
        woo_id = woo_customer.get('id')
        if not woo_id:
            self._woo_log_customer('Skipped a WooCommerce customer with no id.', 'warning')
            return 'skipped'
        woo_id = str(woo_id)

        if not self._woo_customer_role_allowed(woo_customer):
            self._woo_log_skipped_role(woo_customer, path='import')
            return 'skipped'

        name = self._woo_customer_person_name(woo_customer)
        if not name:
            self._woo_log_customer(
                self.env._(
                    'Skipped WooCommerce customer %(id)s on %(store)s because it has no first or last name.',
                    id=woo_id,
                    store=self.display_name,
                ),
                'warning',
            )
            return 'skipped'

        CustomerBinding = self.env['kits.woo.customer.binding']
        binding = CustomerBinding._find(self, external_id=woo_id)
        if binding and self._woo_payload_is_stale(binding, woo_customer):
            self._woo_log_customer(
                self.env._(
                    'Skipped older WooCommerce customer %(id)s on %(store)s '
                    '(remote modification is not newer than the binding).',
                    id=woo_id,
                    store=self.display_name,
                ),
            )
            return 'skipped'

        remote_modified = self._woo_customer_remote_modified(woo_customer)

        if binding and binding.partner_id:
            # Reactivate bindings that were soft-deleted on WooCommerce.
            CustomerBinding._bind(
                self, binding.partner_id.id, woo_id,
                remote_modified_at=remote_modified,
            )
            self._woo_ensure_customer_addresses(binding.partner_id, woo_customer)
            self._woo_log_customer(
                self.env._(
                    'Reused contact %(contact)s for WooCommerce customer %(id)s on %(store)s. Existing details were not changed.',
                    contact=binding.partner_id.display_name,
                    id=woo_id,
                    store=self.display_name,
                ),
            )
            return 'reused'

        email = self._woo_normalize_email(self._woo_customer_email(woo_customer))
        partners = self._woo_find_partners_by_email(email) if email else self.env['res.partner']
        if len(partners) > 1:
            self._woo_log_customer(
                self.env._(
                    'Ambiguous email %(email)s for WooCommerce customer %(id)s on %(store)s. '
                    '%(count)s contacts match. Manual review required.',
                    email=email,
                    id=woo_id,
                    store=self.display_name,
                    count=len(partners),
                ),
                'warning',
            )
            return 'ambiguous'
        partner = partners[:1]
        if partner:
            already = CustomerBinding._find(self, odoo_record_id=partner.id)
            if already and already.external_id != woo_id and already.active:
                self._woo_log_customer(
                    self.env._(
                        'Skipped WooCommerce customer %(id)s. Contact %(contact)s is already linked to customer %(other)s on %(store)s.',
                        id=woo_id,
                        contact=partner.display_name,
                        other=already.external_id,
                        store=self.display_name,
                    ),
                    'warning',
                )
                return 'skipped'
            self._woo_ensure_customer_addresses(partner, woo_customer)
            CustomerBinding._bind(
                self, partner.id, woo_id, remote_modified_at=remote_modified,
            )
            self._woo_log_customer(
                self.env._(
                    'Reused contact %(contact)s by email for WooCommerce customer %(id)s on %(store)s. Name, email, phone, and existing addresses were not changed.',
                    contact=partner.display_name,
                    id=woo_id,
                    store=self.display_name,
                ),
            )
            return 'reused'

        vals = self._woo_prepare_partner_vals(woo_customer, include_address=False)
        vals['name'] = name
        if woo_customer.get('username'):
            vals['ref'] = woo_customer.get('username')
        partner = self.env['res.partner'].with_company(self.company_id).with_context(
            kits_woo_skip_dirty=True,
        ).create(vals)
        self._woo_ensure_customer_addresses(partner, woo_customer)
        CustomerBinding._bind(
            self, partner.id, woo_id, remote_modified_at=remote_modified,
        )
        self._woo_log_customer(
            self.env._(
                'Created contact %(contact)s for WooCommerce customer %(id)s on %(store)s.',
                contact=partner.display_name,
                id=woo_id,
                store=self.display_name,
            ),
        )
        return 'created'

    def _woo_log_customer(self, message, level='info'):
        self._kits_log_operation(
            operation='woo_import_customers',
            status='success' if level == 'info' else 'failure',
            level=level,
            message=message,
        )

    def _woo_delete_customer_binding(self, woo_id, reason=False):
        """Soft-delete this store's customer binding; keep the Odoo contact and history."""
        self.ensure_one()
        binding = self.env['kits.woo.customer.binding']._find(self, external_id=str(woo_id))
        if not binding:
            return False
        if binding.sync_state == 'deleted_remotely' and not binding.active:
            return True
        partner = binding.partner_id
        message = reason or self.env._(
            'Marked the customer binding deleted remotely for WooCommerce customer '
            '%(id)s on %(store)s. The Odoo contact was kept.',
            id=woo_id,
            store=self.display_name,
        )
        binding._kits_mark_deleted_remotely(message=message)
        if partner:
            partner.invalidate_recordset()
        self._woo_log_customer(message)
        return True

    def _woo_customer_person_name(self, woo_customer):
        billing = (woo_customer or {}).get('billing') or {}
        return ' '.join(part for part in (
            (woo_customer.get('first_name') or billing.get('first_name') or '').strip(),
            (woo_customer.get('last_name') or billing.get('last_name') or '').strip(),
        ) if part).strip()

    def _woo_customer_email(self, woo_customer):
        billing = (woo_customer or {}).get('billing') or {}
        return (woo_customer.get('email') or billing.get('email') or '').strip()

    def _woo_ensure_customer_addresses(self, partner, woo_customer):
        """Add missing billing and shipping child contacts. Never overwrite an existing one."""
        self.ensure_one()
        billing = woo_customer.get('billing') or {}
        shipping = woo_customer.get('shipping') or {}
        self._woo_upsert_child_address(partner, billing, 'invoice')
        self._woo_upsert_child_address(partner, shipping, 'delivery')

    def _woo_upsert_child_address(self, partner, address, address_type):
        vals = self._woo_child_address_vals(partner, address, address_type)
        if not vals:
            return self.env['res.partner']
        existing = self._woo_find_exact_address(partner, vals, address_type)
        if existing:
            return existing
        return self.env['res.partner'].with_company(self.company_id).with_context(
            kits_woo_skip_dirty=True,
        ).create(vals)

    def _woo_child_address_vals(self, partner, address, address_type):
        address = address or {}
        name = ' '.join(part for part in (
            (address.get('first_name') or '').strip(),
            (address.get('last_name') or '').strip(),
        ) if part).strip() or partner.name
        vals = {
            'parent_id': partner.id,
            'type': address_type,
            'name': name,
            'street': (address.get('address_1') or '').strip() or False,
            'street2': (address.get('address_2') or '').strip() or False,
            'city': (address.get('city') or '').strip() or False,
            'zip': (address.get('postcode') or '').strip() or False,
            'phone': (address.get('phone') or '').strip() or False,
            'company_id': partner.company_id.id,
            'email': False,
        }
        self._woo_apply_address_vals(vals, address)
        if not any(vals.get(key) for key in ('street', 'city', 'zip', 'state_id', 'country_id')):
            return {}
        return vals

    def _woo_find_exact_address(self, partner, vals, address_type):
        children = partner.child_ids.filtered(lambda child: child.type == address_type)
        for child in children:
            if self._woo_address_matches(child, vals, compare_phone=address_type == 'invoice'):
                return child
        return self.env['res.partner']

    @staticmethod
    def _woo_address_text(value):
        return (value or '').strip().casefold()

    def _woo_address_matches(self, child, vals, compare_phone):
        if self._woo_address_text(child.name) != self._woo_address_text(vals.get('name')):
            return False
        if self._woo_address_text(child.street) != self._woo_address_text(vals.get('street')):
            return False
        if self._woo_address_text(child.street2) != self._woo_address_text(vals.get('street2')):
            return False
        if self._woo_address_text(child.city) != self._woo_address_text(vals.get('city')):
            return False
        if self._woo_address_text(child.zip) != self._woo_address_text(vals.get('zip')):
            return False
        if (child.state_id.id or False) != (vals.get('state_id') or False):
            return False
        if (child.country_id.id or False) != (vals.get('country_id') or False):
            return False
        if compare_phone and self._woo_address_text(child.phone) != self._woo_address_text(vals.get('phone')):
            return False
        return True

    @staticmethod
    def _woo_normalize_email(email):
        return (email or '').strip().lower()

    def _woo_find_partners_by_email(self, email):
        """All top-level contacts with this normalized email in the company."""
        self.ensure_one()
        email = self._woo_normalize_email(email)
        if not email:
            return self.env['res.partner']
        # =ilike treats _ and % as wildcards, so narrow to exact normalized email.
        candidates = self.env['res.partner'].with_company(self.company_id).search([
            ('email', '=ilike', email),
            ('parent_id', '=', False),
            ('company_id', 'in', [False, self.company_id.id]),
        ], order='id')
        return candidates.filtered(
            lambda partner: self._woo_normalize_email(partner.email) == email
        )

    def _woo_find_partner_by_email(self, email):
        """Unambiguous email match, or empty if none / multiple."""
        partners = self._woo_find_partners_by_email(email)
        if len(partners) == 1:
            return partners
        return self.env['res.partner']

    def _woo_prepare_partner_vals(self, woo_customer, include_address=True):
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
        if include_address:
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
        if not self._woo_customer_role_allowed(remote):
            self._woo_log_skipped_role(remote, path='fetch')
            raise ValueError(self.env._(
                'WooCommerce user %(id)s has role "%(role)s". Only role "customer" can be fetched.',
                id=woo_customer_id,
                role=remote.get('role') or '(missing)',
            ))
        result = self._woo_import_one_customer(remote)
        if result == 'ambiguous':
            raise ValueError(self.env._(
                'Multiple Odoo contacts share the email for WooCommerce customer %(id)s. '
                'Resolve the duplicate contacts, then fetch again.',
                id=woo_customer_id,
            ))
        if result == 'skipped':
            raise ValueError(self.env._(
                'Customer fetch finished with status skipped for WooCommerce customer %(id)s.',
                id=woo_customer_id,
            ))
        binding = self.env['kits.woo.customer.binding']._find(self, external_id=str(remote['id']))
        partner = binding.partner_id if binding and binding.active else self.env['res.partner']
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
        """Resolve partner for an order. Guests never get customer bindings.

        Registered customers use the same matcher as customer import. Ambiguous
        email matches raise so the order is not confirmed with a wrong contact.
        """
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
                    if not self._woo_customer_role_allowed(woo_customer):
                        self._woo_log_skipped_role(woo_customer, path='order import')
                    else:
                        result = self._woo_import_one_customer(woo_customer)
                        if result == 'ambiguous':
                            raise UserError(self.env._(
                                'WooCommerce order %(order)s customer %(customer)s matches '
                                'multiple Odoo contacts by email. Resolve the duplicates '
                                'before importing this order.',
                                order=woo_order.get('id'),
                                customer=customer_id,
                            ))
                        if result != 'skipped':
                            binding = self.env['kits.woo.customer.binding']._find(
                                self, external_id=str(woo_customer['id']),
                            )
                            if binding and binding.active and binding.partner_id:
                                return binding.partner_id
            except UserError:
                raise
            except Exception:  # noqa: BLE001
                _logger.info(
                    'Falling back to order billing for Woo customer %s on instance %s',
                    customer_id, self.id,
                )

        # Guest, non-customer role, or unreachable customer: email without binding
        email = self._woo_normalize_email(billing.get('email') or '')
        if email:
            partners = self._woo_find_partners_by_email(email)
            if len(partners) > 1:
                raise UserError(self.env._(
                    'WooCommerce order %(order)s billing email %(email)s matches '
                    'multiple Odoo contacts. Resolve the duplicates before importing '
                    'this order.',
                    order=woo_order.get('id'),
                    email=email,
                ))
            if len(partners) == 1:
                return partners

        fallback = email or f"WooCommerce Guest {woo_order.get('id')}"
        vals = self._woo_prepare_partner_vals({
            'email': email,
            'billing': billing,
        }, include_address=False)
        if not vals.get('name'):
            vals['name'] = fallback
        return self.env['res.partner'].with_company(self.company_id).with_context(
            kits_woo_skip_dirty=True,
        ).create(vals)

    def _woo_order_address_partners(self, woo_order, partner):
        """Return (invoice_partner, shipping_partner) child contacts when possible."""
        self.ensure_one()
        billing = woo_order.get('billing') or {}
        shipping = woo_order.get('shipping') or {}
        invoice = self._woo_upsert_child_address(partner, billing, 'invoice') or partner
        delivery = self._woo_upsert_child_address(partner, shipping, 'delivery') or partner
        return invoice, delivery
