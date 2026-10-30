# -*- coding: utf-8 -*-
"""WooCommerce coupon import/export with binding-based product restrictions."""

import logging

from odoo import fields, models
from odoo.exceptions import UserError
from odoo.fields import Command

_logger = logging.getLogger(__name__)


class KitsConnectorInstanceCoupon(models.Model):
    _inherit = 'kits.connector.instance'

    woo_last_coupon_sync = fields.Datetime(readonly=True, copy=False)

    def action_woo_import_coupons(self):
        self._ensure_woo_ready()
        for instance in self:
            instance.action_create_sync_job(
                'woo_import_coupons',
                name=self.env._('WooCommerce: Import Coupons (%s)', instance.name),
            )
        return self._woo_notify(self.env._('Coupon import job(s) queued.'))

    def action_woo_export_coupons(self):
        self._ensure_woo_ready()
        for instance in self:
            instance.action_create_sync_job(
                'woo_export_coupons',
                name=self.env._('WooCommerce: Export Coupons (%s)', instance.name),
            )
        return self._woo_notify(self.env._('Coupon export job(s) queued.'))

    def _woo_import_coupons(self, job=None):
        self.ensure_one()
        api = self._woo_api()
        payload = (job.payload if job else None) or {}
        woo_ids = self._woo_parse_id_list(payload.get('woo_ids'))
        modified_after = payload.get('modified_after')
        created = updated = skipped = failed = 0
        errors = []

        if woo_ids:
            coupons = []
            for cid in woo_ids:
                try:
                    remote = api.get_coupon(cid)
                    if remote.get('id'):
                        coupons.append(remote)
                    else:
                        skipped += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    errors.append(f'Coupon {cid}: {exc}')
            coupon_iter = coupons
        else:
            params = {}
            if modified_after:
                params['modified_after'] = str(modified_after).replace(' ', 'T')
            coupon_iter = api.get_coupons(**params)

        for woo_coupon in coupon_iter:
            try:
                result = self._woo_import_one_coupon(woo_coupon)
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f"Coupon {woo_coupon.get('id')}: {exc}")
                _logger.exception(
                    'Woo coupon import failed for %s on instance %s',
                    woo_coupon.get('id'), self.id,
                )

        self.woo_last_coupon_sync = fields.Datetime.now()
        summary = (
            f'Coupon import finished: created={created}, updated={updated}, '
            f'skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_validate_woo_coupon_payload(self, woo_coupon):
        woo_id = woo_coupon.get('id')
        code = (woo_coupon.get('code') or '').strip()
        discount_type = woo_coupon.get('discount_type') or 'percent'
        if not woo_id:
            raise UserError(self.env._('Coupon payload is missing id.'))
        if not code:
            raise UserError(self.env._(
                'WooCommerce coupon %(id)s is missing required code.', id=woo_id,
            ))
        if discount_type not in ('percent', 'fixed_cart', 'fixed_product'):
            raise UserError(self.env._(
                'Unsupported WooCommerce coupon discount_type "%(dtype)s" for coupon %(id)s.',
                dtype=discount_type,
                id=woo_id,
            ))
        try:
            amount = float(woo_coupon.get('amount') or 0.0)
        except (TypeError, ValueError) as exc:
            raise UserError(self.env._(
                'Invalid coupon amount for WooCommerce coupon %(id)s.', id=woo_id,
            )) from exc
        return code, discount_type, amount

    def _woo_resolve_coupon_product_ids(self, woo_ids):
        """Map Woo product/variation ids → product.product using this instance only."""
        self.ensure_one()
        products = self.env['product.product']
        unresolved = []
        deleted = []
        for wid in woo_ids or []:
            binding = self._woo_binding()._kits_find_binding(
                self, 'product.product', external_id=str(wid),
            ) or self._woo_binding()._kits_find_binding(
                self, 'product.template', external_id=str(wid),
            )
            if binding and binding.sync_state == 'deleted':
                deleted.append(str(wid))
                continue
            product = self.env['product.product']
            if binding and binding.sync_state != 'deleted':
                record = binding._kits_get_odoo_record()
                if record._name == 'product.product':
                    product = record
                elif record._name == 'product.template' and record.product_variant_id:
                    product = record.product_variant_id
            if not product:
                unresolved.append(str(wid))
                continue
            products |= product
        return products, unresolved, deleted

    def _woo_resolve_coupon_category_ids(self, woo_ids):
        self.ensure_one()
        categories = self.env['product.category']
        unresolved = []
        deleted = []
        for wid in woo_ids or []:
            binding = self._woo_binding()._kits_find_binding(
                self, 'product.category', external_id=str(wid),
            )
            if binding and binding.sync_state == 'deleted':
                deleted.append(str(wid))
                continue
            if binding and binding.sync_state != 'deleted':
                categ = binding._kits_get_odoo_record()
                if categ:
                    categories |= categ
                    continue
            unresolved.append(str(wid))
        return categories, unresolved, deleted

    def _woo_coupon_restriction_vals(self, woo_coupon):
        """Build M2M + text restriction vals; never silently drop Woo restrictions."""
        self.ensure_one()
        include_ids = list(woo_coupon.get('product_ids') or [])
        exclude_ids = list(woo_coupon.get('excluded_product_ids') or [])
        include_categ = list(woo_coupon.get('product_categories') or [])
        exclude_categ = list(woo_coupon.get('excluded_product_categories') or [])

        products, unresolved_p, deleted_p = self._woo_resolve_coupon_product_ids(include_ids)
        excl_products, unresolved_ep, deleted_ep = self._woo_resolve_coupon_product_ids(exclude_ids)
        categs, unresolved_c, deleted_c = self._woo_resolve_coupon_category_ids(include_categ)
        excl_categs, unresolved_ec, deleted_ec = self._woo_resolve_coupon_category_ids(exclude_categ)

        unresolved = (
            [f'product:{i}' for i in unresolved_p]
            + [f'excluded_product:{i}' for i in unresolved_ep]
            + [f'category:{i}' for i in unresolved_c]
            + [f'excluded_category:{i}' for i in unresolved_ec]
            + [f'deleted_product:{i}' for i in deleted_p + deleted_ep]
            + [f'deleted_category:{i}' for i in deleted_c + deleted_ec]
        )
        error = False
        if unresolved:
            error = self.env._(
                'Coupon restrictions reference Woo IDs that are unmapped or deleted '
                'for this store: %(ids)s. Restrictions were NOT dropped. Import or bind '
                'the products/categories, then re-import or retry export.',
                ids=', '.join(unresolved),
            )

        return {
            'product_ids': [Command.set(products.ids)],
            'excluded_product_ids': [Command.set(excl_products.ids)],
            'product_category_ids': [Command.set(categs.ids)],
            'excluded_product_category_ids': [Command.set(excl_categs.ids)],
            'product_ids_text': ','.join(str(i) for i in include_ids) or False,
            'excluded_product_ids_text': ','.join(str(i) for i in exclude_ids) or False,
            'product_categories_text': ','.join(str(i) for i in include_categ) or False,
            'excluded_product_categories_text': ','.join(str(i) for i in exclude_categ) or False,
            'unresolved_restriction_ids': '\n'.join(unresolved) or False,
            'restriction_error': error,
        }

    def _woo_import_one_coupon(self, woo_coupon):
        self.ensure_one()
        code, discount_type, amount = self._woo_validate_woo_coupon_payload(woo_coupon)
        woo_id = woo_coupon['id']
        Coupon = self.env['kits.woo.coupon']
        existing = self._woo_find_odoo('kits.woo.coupon', woo_id)
        vals = {
            'name': woo_coupon.get('description') or code,
            'code': code,
            'instance_id': self.id,
            'company_id': self.company_id.id,
            'discount_type': discount_type,
            'amount': amount,
            'date_expires': woo_coupon.get('date_expires') or False,
            'usage_limit': int(woo_coupon.get('usage_limit') or 0) or False,
            'usage_limit_per_user': int(woo_coupon.get('usage_limit_per_user') or 0) or False,
            'individual_use': bool(woo_coupon.get('individual_use')),
            'free_shipping': bool(woo_coupon.get('free_shipping')),
            'minimum_amount': float(woo_coupon.get('minimum_amount') or 0.0),
            'maximum_amount': float(woo_coupon.get('maximum_amount') or 0.0),
            'email_restrictions': ','.join(
                woo_coupon.get('email_restrictions') or []
            ) or False,
            'description': woo_coupon.get('description') or False,
            'active': True,
            **self._woo_coupon_restriction_vals(woo_coupon),
        }
        if existing:
            existing.write(vals)
            coupon = existing
            result = 'updated'
        else:
            same_code = Coupon.search([
                ('instance_id', '=', self.id),
                ('code', '=', code),
            ], limit=1)
            if same_code:
                same_code.write(vals)
                coupon = same_code
                result = 'updated'
            else:
                coupon = Coupon.create(vals)
                result = 'created'
        binding_state_msg = None
        if coupon.restriction_error:
            binding_state_msg = coupon.restriction_error
        self._woo_bind('kits.woo.coupon', coupon.id, woo_id, external_data={
            'code': code,
            'discount_type': discount_type,
            'restriction_error': bool(coupon.restriction_error),
        })
        if binding_state_msg:
            binding = self._woo_binding()._kits_find_binding(
                self, 'kits.woo.coupon', odoo_record_id=coupon.id,
            )
            if binding:
                binding._kits_mark_error(binding_state_msg)
        return result

    def _woo_export_coupons(self, job=None):
        self.ensure_one()
        api = self._woo_api()
        payload = (job.payload if job else None) or {}
        coupon_ids = self._woo_parse_id_list(payload.get('coupon_ids'))
        domain = [
            ('instance_id', '=', self.id),
            ('sync_to_woo', '=', True),
            ('active', '=', True),
        ]
        if coupon_ids:
            domain.append(('id', 'in', coupon_ids))
        coupons = self.env['kits.woo.coupon'].search(domain)
        created = updated = skipped = failed = 0
        errors = []
        for coupon in coupons:
            try:
                result = self._woo_export_one_coupon(coupon, api)
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f'{coupon.code}: {exc}')
                binding = self._woo_binding()._kits_find_binding(
                    self, 'kits.woo.coupon', odoo_record_id=coupon.id,
                )
                if binding:
                    binding._kits_mark_error(str(exc))
        self.woo_last_coupon_sync = fields.Datetime.now()
        summary = (
            f'Coupon export finished: created={created}, updated={updated}, '
            f'skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_products_to_woo_ids(self, products, *, label):
        self.ensure_one()
        woo_ids = []
        missing = []
        for product in products:
            binding = self._woo_binding()._kits_find_binding(
                self, 'product.product', odoo_record_id=product.id,
            )
            if binding and binding.sync_state != 'deleted':
                woo_ids.append(int(binding.external_id))
                continue
            tmpl = product.product_tmpl_id
            binding = self._woo_binding()._kits_find_binding(
                self, 'product.template', odoo_record_id=tmpl.id,
            )
            if binding and binding.sync_state != 'deleted':
                woo_ids.append(int(binding.external_id))
                continue
            missing.append(product.display_name)
        if missing:
            raise UserError(self.env._(
                'Cannot export coupon %(label)s restrictions: products without a '
                'binding on this store: %(names)s. Bind/export products first.',
                label=label,
                names=', '.join(missing[:10]),
            ))
        return woo_ids

    def _woo_categories_to_woo_ids(self, categories, *, label):
        self.ensure_one()
        woo_ids = []
        missing = []
        for categ in categories:
            binding = self._woo_binding()._kits_find_binding(
                self, 'product.category', odoo_record_id=categ.id,
            )
            if binding and binding.sync_state != 'deleted':
                woo_ids.append(int(binding.external_id))
            else:
                missing.append(categ.display_name)
        if missing:
            raise UserError(self.env._(
                'Cannot export coupon %(label)s category restrictions: categories '
                'without a binding on this store: %(names)s.',
                label=label,
                names=', '.join(missing[:10]),
            ))
        return woo_ids

    def _woo_coupon_to_payload(self, coupon):
        self.ensure_one()
        if not coupon.code:
            raise UserError(self.env._('Coupon code is required before export.'))
        if coupon.restriction_error or coupon.unresolved_restriction_ids:
            raise UserError(self.env._(
                'Coupon %(code)s has unresolved product/category restrictions for '
                'store "%(store)s". Resolve bindings (see Restriction Sync Error) '
                'before export to avoid broadening eligibility.\n%(error)s',
                code=coupon.code,
                store=self.name,
                error=coupon.restriction_error or coupon.unresolved_restriction_ids,
            ))
        payload = {
            'code': coupon.code,
            'discount_type': coupon.discount_type,
            'amount': str(coupon.amount),
            'individual_use': coupon.individual_use,
            'free_shipping': coupon.free_shipping,
            'description': coupon.description or coupon.name or '',
        }
        if coupon.date_expires:
            payload['date_expires'] = fields.Date.to_string(coupon.date_expires)
        if coupon.usage_limit:
            payload['usage_limit'] = coupon.usage_limit
        if coupon.usage_limit_per_user:
            payload['usage_limit_per_user'] = coupon.usage_limit_per_user
        if coupon.minimum_amount:
            payload['minimum_amount'] = str(coupon.minimum_amount)
        if coupon.maximum_amount:
            payload['maximum_amount'] = str(coupon.maximum_amount)

        # Prefer M2M bindings; fall back to preserved Woo ID text only when M2M empty
        # and text exists WITHOUT unresolved errors (already blocked above).
        if coupon.product_ids:
            payload['product_ids'] = self._woo_products_to_woo_ids(
                coupon.product_ids, label='included products',
            )
        elif coupon.product_ids_text:
            payload['product_ids'] = self._woo_parse_id_list(coupon.product_ids_text)

        if coupon.excluded_product_ids:
            payload['excluded_product_ids'] = self._woo_products_to_woo_ids(
                coupon.excluded_product_ids, label='excluded products',
            )
        elif coupon.excluded_product_ids_text:
            payload['excluded_product_ids'] = self._woo_parse_id_list(
                coupon.excluded_product_ids_text,
            )

        if coupon.product_category_ids:
            payload['product_categories'] = self._woo_categories_to_woo_ids(
                coupon.product_category_ids, label='included categories',
            )
        elif coupon.product_categories_text:
            payload['product_categories'] = self._woo_parse_id_list(
                coupon.product_categories_text,
            )

        if coupon.excluded_product_category_ids:
            payload['excluded_product_categories'] = self._woo_categories_to_woo_ids(
                coupon.excluded_product_category_ids, label='excluded categories',
            )
        elif coupon.excluded_product_categories_text:
            payload['excluded_product_categories'] = self._woo_parse_id_list(
                coupon.excluded_product_categories_text,
            )

        if coupon.email_restrictions:
            emails = [e.strip() for e in coupon.email_restrictions.split(',') if e.strip()]
            if emails:
                payload['email_restrictions'] = emails
        return payload

    def _woo_export_one_coupon(self, coupon, api=None):
        self.ensure_one()
        api = api or self._woo_api()
        payload = self._woo_coupon_to_payload(coupon)
        binding = self._woo_binding()._kits_find_binding(
            self, 'kits.woo.coupon', odoo_record_id=coupon.id,
        )
        if binding and binding.sync_state != 'deleted':
            remote = api.update_coupon(int(binding.external_id), payload)
            self._woo_bind(
                'kits.woo.coupon', coupon.id, remote.get('id') or binding.external_id,
                external_data={'code': coupon.code},
            )
            return 'updated'
        remote = api.create_coupon(payload)
        if not remote.get('id'):
            raise UserError(self.env._('WooCommerce did not return a coupon id.'))
        self._woo_bind(
            'kits.woo.coupon', coupon.id, remote['id'],
            external_data={'code': coupon.code},
        )
        return 'created'
