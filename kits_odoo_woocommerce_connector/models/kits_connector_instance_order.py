# -*- coding: utf-8 -*-
import logging
from datetime import datetime

from odoo import fields, models
from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tools.misc import DEFAULT_SERVER_DATETIME_FORMAT

_logger = logging.getLogger(__name__)


class KitsConnectorInstanceOrder(models.Model):
    _inherit = 'kits.connector.instance'

    def _woo_import_orders(self, job=None):
        """Import WooCommerce orders into confirmed Odoo sale orders (idempotent).

        Failed order IDs are stored and always retried by ID so the date checkpoint
        never permanently skips them when later orders succeed.
        """
        self.ensure_one()
        api = self._woo_api()
        statuses = [
            s.strip() for s in (self.woo_order_statuses or 'processing,completed').split(',')
            if s.strip()
        ]
        params = {
            'status': ','.join(statuses) if statuses else 'processing,completed',
            'orderby': 'date',
            'order': 'asc',
        }
        if self.woo_last_order_sync:
            params['after'] = fields.Datetime.to_string(self.woo_last_order_sync).replace(' ', 'T')

        created = skipped = failed = retried = 0
        errors = []
        latest_date = self.woo_last_order_sync

        # 1) Retry previously failed orders by ID (independent of date watermark)
        for failed_id in list(self._woo_order_failure_ids()):
            try:
                woo_order = api.get_order(failed_id)
                if not woo_order.get('id'):
                    self._woo_clear_order_failure(failed_id)
                    continue
                result, order_date = self._woo_import_one_order(woo_order, job=job)
                self._woo_clear_order_failure(failed_id)
                retried += 1
                if result == 'created':
                    created += 1
                else:
                    skipped += 1
                if order_date and (not latest_date or order_date > latest_date):
                    latest_date = order_date
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f"Order {failed_id} (retry): {exc}")
                _logger.exception(
                    'WooCommerce order retry failed for %s on instance %s',
                    failed_id, self.id,
                )
                self._kits_log_operation(
                    operation='woo_import_orders',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f"woo_order_id={failed_id}",
                    job=job,
                )

        # 2) Import new orders after watermark
        for woo_order in api.get_orders(**params):
            woo_id = woo_order.get('id')
            try:
                result, order_date = self._woo_import_one_order(woo_order, job=job)
                if result == 'created':
                    created += 1
                else:
                    skipped += 1
                if order_date and (not latest_date or order_date > latest_date):
                    latest_date = order_date
                if woo_id:
                    self._woo_clear_order_failure(woo_id)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if woo_id:
                    self._woo_remember_order_failure(woo_id)
                errors.append(f"Order {woo_id}: {exc}")
                _logger.exception(
                    'WooCommerce order import failed for %s on instance %s',
                    woo_id,
                    self.id,
                )
                self._kits_log_operation(
                    operation='woo_import_orders',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f"woo_order_id={woo_id}",
                    job=job,
                )
                # Continue other orders; watermark still advances — failures retried by ID
                continue

        if latest_date:
            self.woo_last_order_sync = latest_date
        elif not self.woo_last_order_sync:
            self.woo_last_order_sync = fields.Datetime.now()

        summary = (
            f'Order import finished: created={created}, skipped={skipped}, '
            f'failed={failed}, retried_ok={retried}, '
            f'pending_failures={len(self._woo_order_failure_ids())}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_import_one_order(self, woo_order, job=None):
        self.ensure_one()
        woo_id = woo_order.get('id')
        if not woo_id:
            return 'skipped', None

        existing = self._woo_find_odoo('sale.order', woo_id)
        order_date = self._woo_parse_datetime(
            woo_order.get('date_created_gmt') or woo_order.get('date_created')
        )
        woo_status = (woo_order.get('status') or '').lower()

        if existing:
            return self._woo_handle_existing_order(existing, woo_order, order_date)

        if woo_status in ('cancelled', 'trash'):
            # Do not create Odoo orders for cancelled remote orders
            return 'skipped', order_date

        warehouse = self._woo_get_warehouse()
        if not warehouse:
            raise UserError(self.env._(
                'A warehouse is required to import and confirm WooCommerce orders '
                '(sale_stock). Configure one on the store "%(name)s".',
                name=self.name,
            ))

        partner = self._woo_partner_from_order(woo_order)
        invoice_partner = self._woo_find_or_create_address_partner(
            woo_order.get('billing') or {},
            parent=partner,
            address_type='invoice',
        ) or partner
        shipping_partner = self._woo_find_or_create_address_partner(
            woo_order.get('shipping') or {},
            parent=partner,
            address_type='delivery',
        ) or partner

        # Resolve all product lines first — fail before creating SO if mapping required
        line_vals_list = []
        unmapped = []
        for item in woo_order.get('line_items') or []:
            try:
                line_vals_list.append(self._woo_prepare_order_line_vals(item))
            except UserError as exc:
                unmapped.append(str(exc))
        if unmapped and not self.woo_allow_unmapped_order_lines:
            raise UserError(self.env._(
                'WooCommerce order %(woo_id)s has unmapped products:\n%(errors)s',
                woo_id=woo_id,
                errors='\n'.join(unmapped),
            ))

        order_vals = {
            'partner_id': partner.id,
            'partner_invoice_id': invoice_partner.id,
            'partner_shipping_id': shipping_partner.id,
            'company_id': self.company_id.id,
            'warehouse_id': warehouse.id,
            'team_id': self.sales_team_id.id if self.sales_team_id else False,
            'pricelist_id': self.pricelist_id.id if self.pricelist_id else False,
            'client_order_ref': woo_order.get('number') or str(woo_id),
            'origin': f'WooCommerce #{woo_order.get("number") or woo_id}',
            'date_order': order_date or fields.Datetime.now(),
            'woo_instance_id': self.id,
            'woo_order_id': str(woo_id),
            'woo_order_status': woo_order.get('status'),
        }
        currency_code = (woo_order.get('currency') or '').upper()
        if currency_code:
            currency = self.env['res.currency'].search([('name', '=', currency_code)], limit=1)
            if currency:
                order_vals['currency_id'] = currency.id

        order = self.env['sale.order'].with_company(self.company_id).create(order_vals)

        line_commands = [Command.create(vals) for vals in line_vals_list]

        # Shipping as separate line(s) — amounts from Woo are typically tax-exclusive total
        for shipping in woo_order.get('shipping_lines') or []:
            total = float(shipping.get('total') or 0.0)
            if total:
                line_commands.append(Command.create(
                    self._woo_prepare_shipping_line_vals(shipping, total)
                ))

        # Order-level discount only for remainder not already reflected in line prices.
        # Woo line `price` is typically unit price after line discounts; `discount_total`
        # is the cart coupon total — use it once as a negative service line.
        discount_total = float(woo_order.get('discount_total') or 0.0)
        if discount_total > 0:
            line_commands.append(Command.create(
                self._woo_prepare_discount_line_vals(discount_total)
            ))

        # Fee lines (positive or negative)
        for fee in woo_order.get('fee_lines') or []:
            total = float(fee.get('total') or 0.0)
            if total:
                line_commands.append(Command.create(
                    self._woo_prepare_fee_line_vals(fee, total)
                ))

        if not line_commands:
            order.unlink()
            raise ValueError(self.env._(
                'WooCommerce order %(woo_id)s has no importable lines.',
                woo_id=woo_id,
            ))

        order.write({'order_line': line_commands})

        # Apply tax policy: avoid double tax when amounts already include tax context
        self._woo_apply_order_tax_policy(order, woo_order)

        # Bind before confirmation to prevent duplicate imports on retry
        self._woo_bind('sale.order', order.id, woo_id, external_data={
            'number': woo_order.get('number'),
            'status': woo_order.get('status'),
            'total': woo_order.get('total'),
        })

        if order.state in ('draft', 'sent'):
            order.action_confirm()

        self._kits_log_operation(
            operation='woo_import_orders',
            status='success',
            level='info',
            message=self.env._(
                'Imported WooCommerce order %(woo)s as %(odoo)s',
                woo=woo_id,
                odoo=order.name,
            ),
            job=job,
        )
        return 'created', order_date

    def _woo_handle_existing_order(self, order, woo_order, order_date):
        """Idempotent re-import: update status metadata; never rewrite confirmed lines."""
        self.ensure_one()
        woo_status = (woo_order.get('status') or '').lower()
        updates = {}
        if woo_order.get('status') and order.woo_order_status != woo_order.get('status'):
            updates['woo_order_status'] = woo_order.get('status')

        if woo_status in ('cancelled', 'refunded', 'trash'):
            note = self.env._(
                'WooCommerce order status is now "%(status)s". Odoo order was not overwritten.',
                status=woo_status,
            )
            if order.state not in ('cancel', 'done'):
                # Record conflict; cancel only when still cancellable draft/sale without deliveries
                if order.state in ('draft', 'sent'):
                    order.action_cancel()
                    updates['woo_sync_conflict'] = note
                elif order.state == 'sale' and not order.picking_ids.filtered(
                    lambda p: p.state == 'done'
                ):
                    try:
                        order.with_context(disable_cancel_warning=True).action_cancel()
                        updates['woo_sync_conflict'] = note
                    except Exception:  # noqa: BLE001
                        updates['woo_sync_conflict'] = note
                else:
                    updates['woo_sync_conflict'] = note
            else:
                updates.setdefault('woo_sync_conflict', note)

        if updates:
            order.write(updates)
        return 'skipped', order_date

    def _woo_apply_order_tax_policy(self, order, woo_order):
        """Keep line prices as provided; clear fiscal taxes when Woo prices are tax-inclusive
        or when we cannot safely map tax rates — prevents double taxation.
        """
        self.ensure_one()
        prices_include_tax = bool(woo_order.get('prices_include_tax'))
        # If Woo prices include tax, disable taxes on lines so Odoo total stays closer to Woo.
        # Full fiscal mapping is out of scope for this phase.
        if prices_include_tax:
            for line in order.order_line:
                if line.tax_ids:
                    line.tax_ids = [Command.clear()]
            if 'tax_calculation_rounding_method' in order.company_id._fields:
                pass
        # Leave exclusive-tax orders with Odoo default taxes from the product;
        # do not invent tax rates from Woo tax_lines in this phase.

    def _woo_prepare_order_line_vals(self, item):
        self.ensure_one()
        product = self._woo_match_order_product(item)
        qty = float(item.get('quantity') or 1.0)
        # Woo `price` is unit price (generally after line discounts, tax excl. depending on store)
        price = float(item.get('price') or 0.0)
        name = item.get('name') or (product.display_name if product else 'WooCommerce line')
        if not product:
            if not self.woo_allow_unmapped_order_lines:
                raise UserError(self.env._(
                    'Unmapped product (sku=%(sku)s, product_id=%(pid)s, variation_id=%(vid)s)',
                    sku=item.get('sku') or '',
                    pid=item.get('product_id') or '',
                    vid=item.get('variation_id') or '',
                ))
            fallback = self._woo_get_or_create_misc_product()
            return {
                'name': f"{name} [unmapped SKU={item.get('sku') or item.get('product_id')}]",
                'product_id': fallback.id,
                'product_uom_id': fallback.uom_id.id,
                'product_uom_qty': qty,
                'price_unit': price,
            }
        return {
            'name': name,
            'product_id': product.id,
            'product_uom_id': product.uom_id.id,
            'product_uom_qty': qty,
            'price_unit': price,
        }

    def _woo_match_order_product(self, item):
        self.ensure_one()
        variation_id = item.get('variation_id')
        product_id = item.get('product_id')
        if variation_id:
            product = self._woo_find_odoo('product.product', variation_id)
            if product:
                return product
        if product_id:
            product = self._woo_find_odoo('product.product', product_id)
            if product:
                return product
            # Template binding for simple products
            template = self._woo_find_odoo('product.template', product_id)
            if template and template.product_variant_id:
                return template.product_variant_id
        sku = (item.get('sku') or '').strip()
        if sku and self.woo_match_by_sku:
            product = self.env['product.product'].search([
                ('default_code', '=', sku),
                ('company_id', 'in', [False, self.company_id.id]),
            ], limit=1)
            if product:
                # Prefer unbound or already bound to this store — avoid cross-store silent merge
                binding = self._woo_binding()._kits_find_binding(
                    self, 'product.product', odoo_record_id=product.id,
                )
                if not binding or str(binding.external_id) in {
                    str(variation_id or ''), str(product_id or ''),
                }:
                    return product
        return self.env['product.product']

    def _woo_prepare_shipping_line_vals(self, shipping, total):
        self.ensure_one()
        product = self.env.ref(
            'kits_odoo_woocommerce_connector.product_woo_shipping',
            raise_if_not_found=False,
        ) or self._woo_get_or_create_shipping_product()
        return {
            'product_id': product.id,
            'name': shipping.get('method_title') or self.env._('Shipping'),
            'product_uom_qty': 1.0,
            'product_uom_id': product.uom_id.id,
            'price_unit': total,
            'tax_ids': [Command.clear()],
        }

    def _woo_prepare_discount_line_vals(self, discount_total):
        self.ensure_one()
        product = self.env.ref(
            'kits_odoo_woocommerce_connector.product_woo_discount',
            raise_if_not_found=False,
        ) or self._woo_get_or_create_discount_product()
        return {
            'product_id': product.id,
            'name': self.env._('WooCommerce Discount'),
            'product_uom_qty': 1.0,
            'product_uom_id': product.uom_id.id,
            'price_unit': -abs(discount_total),
            'tax_ids': [Command.clear()],
        }

    def _woo_prepare_fee_line_vals(self, fee, total):
        self.ensure_one()
        product = self._woo_get_or_create_misc_product()
        return {
            'product_id': product.id,
            'name': fee.get('name') or self.env._('WooCommerce Fee'),
            'product_uom_qty': 1.0,
            'product_uom_id': product.uom_id.id,
            'price_unit': total,
            'tax_ids': [Command.clear()],
        }

    def _woo_get_or_create_shipping_product(self):
        Product = self.env['product.product'].sudo()
        product = Product.search([('default_code', '=', 'WOO_SHIPPING')], limit=1)
        if product:
            return product
        template = self.env['product.template'].sudo().create({
            'name': 'WooCommerce Shipping',
            'type': 'service',
            'default_code': 'WOO_SHIPPING',
            'list_price': 0.0,
            'sale_ok': True,
            'purchase_ok': False,
        })
        return template.product_variant_id

    def _woo_get_or_create_discount_product(self):
        Product = self.env['product.product'].sudo()
        product = Product.search([('default_code', '=', 'WOO_DISCOUNT')], limit=1)
        if product:
            return product
        template = self.env['product.template'].sudo().create({
            'name': 'WooCommerce Discount',
            'type': 'service',
            'default_code': 'WOO_DISCOUNT',
            'list_price': 0.0,
            'sale_ok': True,
            'purchase_ok': False,
        })
        return template.product_variant_id

    def _woo_get_or_create_misc_product(self):
        Product = self.env['product.product'].sudo()
        product = Product.search([('default_code', '=', 'WOO_MISC')], limit=1)
        if product:
            return product
        template = self.env['product.template'].sudo().create({
            'name': 'WooCommerce Unmapped Item',
            'type': 'consu',
            'is_storable': False,
            'default_code': 'WOO_MISC',
            'list_price': 0.0,
            'sale_ok': True,
        })
        return template.product_variant_id

    def _woo_find_or_create_address_partner(self, address, parent, address_type='delivery'):
        self.ensure_one()
        if not any(address.get(k) for k in ('address_1', 'city', 'postcode', 'first_name')):
            return parent
        # Reuse existing child of same type with same street when possible
        street = address.get('address_1') or ''
        existing = parent.child_ids.filtered(
            lambda c, address_type=address_type, street=street: (
                c.type == address_type and (c.street or '') == street
            )
        )[:1]
        name = ' '.join(filter(None, [
            address.get('first_name'),
            address.get('last_name'),
        ])).strip() or parent.name
        vals = {
            'name': name,
            'parent_id': parent.id,
            'type': address_type,
            'street': address.get('address_1') or False,
            'street2': address.get('address_2') or False,
            'city': address.get('city') or False,
            'zip': address.get('postcode') or False,
            'phone': address.get('phone') or False,
            'company_id': self.company_id.id,
        }
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
        if existing:
            existing.write(vals)
            return existing
        return self.env['res.partner'].create(vals)

    @staticmethod
    def _woo_parse_datetime(value):
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).replace('T', ' ').replace('Z', '')
        for fmt in (DEFAULT_SERVER_DATETIME_FORMAT, '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
            try:
                return datetime.strptime(text[:19], fmt) if fmt != '%Y-%m-%d' else datetime.strptime(text[:10], fmt)
            except ValueError:
                continue
        return None
