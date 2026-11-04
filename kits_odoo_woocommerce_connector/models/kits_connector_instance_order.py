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
        payload = (job.payload if job else None) or {}
        specific_ids = self._woo_parse_id_list(payload.get('woo_ids'))
        order_scope = payload.get('order_scope') or False
        date_from = self._woo_parse_datetime(payload.get('date_from') or payload.get('after'))
        date_to = self._woo_parse_datetime(payload.get('date_to') or payload.get('before'))
        statuses = [
            s.strip() for s in (self.woo_order_statuses or 'processing,completed').split(',')
            if s.strip()
        ]
        if order_scope == 'unshipped':
            status_param = 'pending,processing,on-hold'
        elif order_scope == 'shipped':
            status_param = 'completed'
        elif order_scope == 'cancelled':
            status_param = 'cancelled'
        else:
            status_param = ','.join(statuses) if statuses else 'processing,completed'
        params = {
            'status': status_param,
            'orderby': 'date',
            'order': 'asc',
        }
        scoped = bool(order_scope) or bool(specific_ids)
        if date_from:
            params['after'] = fields.Datetime.to_string(date_from).replace(' ', 'T')
        elif self.woo_last_order_sync and not scoped:
            params['after'] = fields.Datetime.to_string(self.woo_last_order_sync).replace(' ', 'T')
        if date_to:
            params['before'] = fields.Datetime.to_string(date_to).replace(' ', 'T')

        created = skipped = failed = retried = 0
        errors = []
        latest_date = self.woo_last_order_sync

        # 1) Retry previously failed orders by ID (independent of date watermark)
        #    Skipped when the job already targets an explicit ID list.
        retry_ids = [] if specific_ids or order_scope else list(self._woo_order_failure_ids())
        for failed_id in retry_ids:
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

        # 2) Import by specific IDs or after watermark / date filter
        if specific_ids:
            order_iter = []
            for oid in specific_ids:
                try:
                    remote = api.get_order(oid)
                    if remote.get('id'):
                        order_iter.append(remote)
                    else:
                        skipped += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    self._woo_remember_order_failure(oid)
                    errors.append(f"Order {oid}: {exc}")
            order_iter = iter(order_iter)
        else:
            order_iter = api.get_orders(**params)

        for woo_order in order_iter:
            woo_id = woo_order.get('id')
            if not specific_ids and not self._woo_order_matches_import(
                woo_order, order_scope, date_from, date_to,
            ):
                skipped += 1
                continue
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

        if not scoped:
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
            self._kits_log_operation(
                operation='woo_import_orders',
                status='success',
                level='info',
                message=self.env._(
                    'Skipped WooCommerce order %(woo)s. It is %(status)s and has no '
                    'linked Odoo sales order, so nothing was created or cancelled.',
                    woo=woo_id,
                    status=woo_status,
                ),
                job=job,
            )
            return 'skipped', order_date

        warehouse = self._woo_get_warehouse()
        if not warehouse:
            raise UserError(self.env._(
                'A warehouse is required to import and confirm WooCommerce orders '
                '(sale_stock). Configure one on the store "%(name)s".',
                name=self.name,
            ))

        partner = self._woo_partner_from_order(woo_order)
        # Billing and shipping stay on the customer contact. Do not create
        # invoice or delivery child contacts for the same Woo customer.
        invoice_partner = partner
        shipping_partner = partner

        # Resolve all product lines first — fail before creating SO if mapping required
        line_vals_list = []
        line_items_src = []
        unmapped = []
        for item in woo_order.get('line_items') or []:
            try:
                line_vals_list.append(self._woo_prepare_order_line_vals(item))
                line_items_src.append(item)
            except UserError as exc:
                unmapped.append(str(exc))
        if unmapped and not self.woo_allow_unmapped_order_lines:
            raise UserError(self.env._(
                'WooCommerce order %(woo_id)s has unmapped products:\n%(errors)s',
                woo_id=woo_id,
                errors='\n'.join(unmapped),
            ))

        payment_method = woo_order.get('payment_method') or False
        payment_title = woo_order.get('payment_method_title') or False
        payment_map = self._woo_find_payment_gateway_map(payment_method)

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
            'woo_payment_method': payment_method,
            'woo_payment_method_title': payment_title,
            'woo_payment_method_id': payment_map.payment_method_id.id if payment_map else False,
        }
        # Apply first mapped shipping carrier when configured (delivery module)
        if 'carrier_id' in self.env['sale.order']._fields:
            for shipping in woo_order.get('shipping_lines') or []:
                smap = self._woo_find_shipping_method_map(shipping.get('method_id'))
                if smap and smap.carrier_id:
                    order_vals['carrier_id'] = smap.carrier_id.id
                    break
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

        # Map taxes via configured kits.woo.tax.map (validated before confirm)
        line_items_meta = []
        product_like = [
            line for line in order.order_line
            if line.display_type not in ('line_section', 'line_note')
            and line.product_id
            and line.product_id.default_code not in ('WOO_SHIPPING', 'WOO_DISCOUNT')
        ]
        for item, line in zip(line_items_src, product_like):
            line_items_meta.append({'item': item, 'line': line})
        self._woo_apply_order_taxes(order, woo_order, line_items_meta)

        # Bind before confirmation to prevent duplicate imports on retry
        self._woo_bind('sale.order', order.id, woo_id, external_data={
            'number': woo_order.get('number'),
            'status': woo_order.get('status'),
            'total': woo_order.get('total'),
        })

        if order.state in ('draft', 'sent'):
            order.action_confirm()

        # Refunds never invent payments; credit notes only when invoices already exist
        self._woo_sync_order_refunds(order, woo_order, job=job)

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
        if woo_order.get('payment_method') and order.woo_payment_method != woo_order.get('payment_method'):
            updates['woo_payment_method'] = woo_order.get('payment_method')
            updates['woo_payment_method_title'] = woo_order.get('payment_method_title') or False
            payment_map = self._woo_find_payment_gateway_map(woo_order.get('payment_method'))
            updates['woo_payment_method_id'] = (
                payment_map.payment_method_id.id if payment_map else False
            )

        # Configurable unsupported-status logging
        known = {
            'pending', 'processing', 'on-hold', 'completed', 'cancelled',
            'refunded', 'failed', 'trash',
        }
        if woo_status and woo_status not in known:
            self._kits_log_operation(
                operation='woo_import_orders',
                status='failure',
                level='warning',
                message=self.env._(
                    'Unsupported/unmapped WooCommerce order status "%(status)s" on order %(woo)s. '
                    'No Odoo state transition was forced.',
                    status=woo_status,
                    woo=woo_order.get('id'),
                ),
            )

        if woo_status in ('cancelled', 'trash', 'failed'):
            block = self._woo_cancel_block_reason(order)
            if block:
                updates['woo_sync_conflict'] = block
                self._kits_log_operation(
                    operation='woo_import_orders',
                    status='success',
                    level='warning',
                    message=block,
                    details=f'sale.order,{order.id}',
                )
            elif order.state not in ('cancel', 'done'):
                try:
                    order.with_context(disable_cancel_warning=True).action_cancel()
                    updates['woo_sync_conflict'] = False
                    self._kits_log_operation(
                        operation='woo_import_orders',
                        status='success',
                        level='info',
                        message=self.env._(
                            'Cancelled Odoo order %(order)s because WooCommerce order '
                            '%(woo)s is %(status)s.',
                            order=order.name,
                            woo=woo_order.get('id'),
                            status=woo_status,
                        ),
                        details=f'sale.order,{order.id}',
                    )
                except Exception as exc:  # noqa: BLE001
                    updates['woo_sync_conflict'] = self.env._(
                        'WooCommerce order %(woo)s is %(status)s. Odoo order %(order)s '
                        'was not cancelled: %(error)s',
                        woo=woo_order.get('id'),
                        status=woo_status,
                        order=order.name,
                        error=exc,
                    )
                    self._kits_log_operation(
                        operation='woo_import_orders',
                        status='failure',
                        level='warning',
                        message=updates['woo_sync_conflict'],
                        details=f'sale.order,{order.id}',
                    )
            else:
                updates.setdefault('woo_sync_conflict', self.env._(
                    'WooCommerce order %(woo)s is %(status)s. Odoo order %(order)s '
                    'is already %(state)s.',
                    woo=woo_order.get('id'),
                    status=woo_status,
                    order=order.name,
                    state=order.state,
                ))

        if updates:
            order.write(updates)

        if woo_status in ('refunded', 'completed', 'processing', 'on-hold') or (
            woo_order.get('refunds')
        ):
            self._woo_sync_order_refunds(order, woo_order)

        return 'skipped', order_date

    def _woo_export_order_tracking(self, order):
        """Export tracking number/URL to WooCommerce order meta + customer note."""
        self.ensure_one()
        order.ensure_one()
        if not order.woo_order_id:
            raise UserError(self.env._('Sales order has no WooCommerce order id.'))
        tracking = (order.woo_tracking_number or '').strip()
        tracking_url = (order.woo_tracking_url or '').strip()
        if not tracking and 'carrier_tracking_ref' in order.picking_ids._fields:
            done = order.picking_ids.filtered(
                lambda p: p.state == 'done' and p.carrier_tracking_ref
            )
            if done:
                tracking = done[0].carrier_tracking_ref
        if not tracking:
            raise UserError(self.env._(
                'Set a tracking number on the sales order (or complete a delivery with '
                'carrier tracking) before exporting.'
            ))
        api = self._woo_api()
        meta = [
            {'key': '_kits_tracking_number', 'value': tracking},
        ]
        if tracking_url:
            meta.append({'key': '_kits_tracking_url', 'value': tracking_url})
        note = self.env._('Tracking: %(tracking)s', tracking=tracking)
        if tracking_url:
            note = f'{note}\n{tracking_url}'
        api.update_order(int(order.woo_order_id), {
            'meta_data': meta,
        })
        api.create_order_note(int(order.woo_order_id), {
            'note': note,
            'customer_note': True,
        })
        if not order.woo_tracking_number:
            order.woo_tracking_number = tracking
        return self._woo_notify(self.env._(
            'Tracking exported to WooCommerce order %(woo)s.',
            woo=order.woo_order_id,
        ))

    def _woo_prepare_order_line_vals(self, item):
        self.ensure_one()
        product = self._woo_match_order_product(item)
        qty = float(item.get('quantity') or 1.0)
        # Woo `price` is unit price (generally after line discounts, tax excl. depending on store)
        price = float(item.get('price') or 0.0)
        name = item.get('name') or (product.display_name if product else 'WooCommerce line')
        # Clear taxes at create; mapped taxes applied in _woo_apply_order_taxes
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
                'tax_ids': [Command.clear()],
            }
        return {
            'name': name,
            'product_id': product.id,
            'product_uom_id': product.uom_id.id,
            'product_uom_qty': qty,
            'price_unit': price,
            'tax_ids': [Command.clear()],
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
                binding = self._woo_record_binding(
                    'product.product', odoo_record_id=product.id,
                )
                if not binding or str(binding.external_id) in {
                    str(variation_id or ''), str(product_id or ''),
                }:
                    return product
        return self.env['product.product']

    def _woo_prepare_shipping_line_vals(self, shipping, total):
        self.ensure_one()
        smap = self._woo_find_shipping_method_map(shipping.get('method_id'))
        product = False
        if smap and smap.delivery_product_id:
            product = smap.delivery_product_id
        if not product:
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

    def _woo_order_matches_import(self, woo_order, order_scope, date_from, date_to):
        status = (woo_order.get('status') or '').lower()
        if order_scope == 'unshipped' and status not in ('pending', 'processing', 'on-hold'):
            return False
        if order_scope == 'shipped' and status != 'completed':
            return False
        if order_scope == 'cancelled' and status != 'cancelled':
            return False
        moment = self._woo_parse_datetime(
            woo_order.get('date_created_gmt') or woo_order.get('date_created')
        )
        if (date_from or date_to) and not moment:
            return False
        if date_from and moment < date_from:
            return False
        if date_to and moment > date_to:
            return False
        return True

    def _woo_cancel_block_reason(self, order):
        """Return why this Odoo order must not be cancelled, or False."""
        self.ensure_one()
        reasons = []
        done = order.picking_ids.filtered(lambda picking: picking.state == 'done')
        if done:
            reasons.append(self.env._('a delivery is already done'))
        invoices = order.invoice_ids.filtered(
            lambda move: move.state != 'cancel' and move.move_type == 'out_invoice'
        )
        if invoices or order.invoice_status == 'invoiced':
            reasons.append(self.env._('it already has an invoice'))
        if not reasons:
            return False
        return self.env._(
            'WooCommerce order %(woo)s was not used to cancel Odoo order %(order)s '
            'because %(reason)s.',
            woo=order.woo_order_id or '',
            order=order.name,
            reason=' and '.join(reasons),
        )

    def _woo_order_ready_to_complete(self, order):
        """True when every outgoing delivery is done and at least one exists."""
        self.ensure_one()
        if not order.woo_order_id or order.state != 'sale':
            return False
        if (order.woo_order_status or '').lower() in (
            'completed', 'cancelled', 'refunded', 'failed', 'trash',
        ):
            return False
        outgoing = order.picking_ids.filtered(
            lambda picking: picking.picking_type_id.code == 'outgoing'
            and picking.state != 'cancel'
        )
        if not outgoing or any(picking.state != 'done' for picking in outgoing):
            return False
        return True

    def _woo_export_shipments(self, job=None):
        """Set linked WooCommerce orders to completed after Odoo delivery is done."""
        self.ensure_one()
        api = self._woo_api()
        orders = self.env['sale.order'].search([
            ('woo_instance_id', '=', self.id),
            ('woo_order_id', '!=', False),
            ('state', '=', 'sale'),
            ('company_id', '=', self.company_id.id),
        ])
        updated = skipped = failed = 0
        errors = []
        for order in orders:
            if not self._woo_order_ready_to_complete(order):
                skipped += 1
                continue
            try:
                remote = api.update_order(int(order.woo_order_id), {'status': 'completed'})
                order.woo_order_status = (remote.get('status') or 'completed')
                updated += 1
                self._kits_log_operation(
                    operation='woo_export_shipments',
                    status='success',
                    level='info',
                    message=self.env._(
                        'Set WooCommerce order %(woo)s to completed from %(order)s.',
                        woo=order.woo_order_id,
                        order=order.name,
                    ),
                    details=f'sale.order,{order.id}',
                    job=job,
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f'{order.name}: {exc}')
                self._kits_log_operation(
                    operation='woo_export_shipments',
                    status='failure',
                    level='error',
                    message=str(exc),
                    details=f'sale.order,{order.id}',
                    job=job,
                )
        summary = (
            f'Shipment export finished: updated={updated}, skipped={skipped}, failed={failed}'
        )
        if errors:
            summary = f'{summary}\n' + '\n'.join(errors[:20])
        return summary

    def _woo_cancel_remote_order(self, order):
        """Ask WooCommerce to cancel the linked order. Leave the Odoo order as it is."""
        self.ensure_one()
        order.ensure_one()
        if not order.woo_order_id:
            raise UserError(self.env._('This sales order is not linked to a WooCommerce order.'))
        if (order.woo_order_status or '').lower() == 'cancelled':
            return self._woo_notify(self.env._(
                'WooCommerce order %(woo)s is already cancelled. The Odoo order was not changed.',
                woo=order.woo_order_id,
            ))
        try:
            remote = self._woo_api().update_order(int(order.woo_order_id), {'status': 'cancelled'})
        except Exception as exc:  # noqa: BLE001
            self._kits_log_operation(
                operation='woo_cancel_order',
                status='failure',
                level='error',
                message=self.env._(
                    'WooCommerce did not cancel order %(woo)s: %(error)s',
                    woo=order.woo_order_id,
                    error=exc,
                ),
                details=f'sale.order,{order.id}',
            )
            raise UserError(self.env._(
                'WooCommerce did not cancel order %(woo)s: %(error)s',
                woo=order.woo_order_id,
                error=exc,
            )) from exc
        order.woo_order_status = remote.get('status') or 'cancelled'
        self._kits_log_operation(
            operation='woo_cancel_order',
            status='success',
            level='info',
            message=self.env._(
                'Requested cancellation of WooCommerce order %(woo)s. Odoo order %(order)s was not cancelled.',
                woo=order.woo_order_id,
                order=order.name,
            ),
            details=f'sale.order,{order.id}',
        )
        return self._woo_notify(self.env._(
            'WooCommerce order %(woo)s was set to %(status)s. Odoo order %(order)s was not cancelled.',
            woo=order.woo_order_id,
            status=order.woo_order_status,
            order=order.name,
        ))

    def _woo_refundable_amount(self, order):
        self.ensure_one()
        done = self.env['kits.woo.refund'].search([
            ('instance_id', '=', self.id),
            ('sale_order_id', '=', order.id),
            ('state', '=', 'done'),
        ])
        return max(float(order.amount_total or 0.0) - sum(done.mapped('amount')), 0.0)

    def _woo_export_credit_note_refund(self, credit_note, order):
        """Create one WooCommerce refund for this credit note. Do not create another credit note."""
        self.ensure_one()
        credit_note.ensure_one()
        order.ensure_one()
        if credit_note.move_type != 'out_refund':
            raise UserError(self.env._('Only a customer credit note can be refunded in WooCommerce.'))
        if credit_note.state == 'cancel':
            raise UserError(self.env._('A cancelled credit note cannot be refunded in WooCommerce.'))
        if not order.woo_order_id or order.woo_instance_id != self:
            raise UserError(self.env._(
                'Sales order %(order)s is not linked to this WooCommerce store.',
                order=order.display_name,
            ))
        existing = self.env['kits.woo.refund'].search([
            ('instance_id', '=', self.id),
            ('credit_note_id', '=', credit_note.id),
            ('state', 'in', ('done', 'pending')),
        ], limit=1)
        if existing:
            raise UserError(self.env._(
                'Credit note %(name)s is already linked to WooCommerce refund %(refund)s.',
                name=credit_note.display_name,
                refund=existing.woo_refund_id,
            ))
        amount = abs(float(credit_note.amount_total or 0.0))
        if amount <= 0:
            raise UserError(self.env._('The credit note amount must be greater than zero.'))
        available = self._woo_refundable_amount(order)
        if amount - available > 0.0001:
            raise UserError(self.env._(
                'Refund amount %(amount)s exceeds the remaining refundable amount %(available)s '
                'on sales order %(order)s.',
                amount=amount,
                available=available,
                order=order.display_name,
            ))
        reason = credit_note.ref or credit_note.payment_reference or credit_note.name or ''
        try:
            remote = self._woo_api().create_order_refund(int(order.woo_order_id), {
                'amount': f'{amount:.2f}',
                'reason': reason,
                'api_refund': False,
            })
        except Exception as exc:  # noqa: BLE001
            self._kits_log_operation(
                operation='woo_export_refund',
                status='failure',
                level='error',
                message=str(exc),
                details=f'account.move,{credit_note.id}',
            )
            raise UserError(self.env._(
                'WooCommerce did not create the refund: %(error)s',
                error=exc,
            )) from exc
        refund_id = remote.get('id')
        if not refund_id:
            raise UserError(self.env._('WooCommerce did not return a refund id.'))
        self.env['kits.woo.refund'].create({
            'instance_id': self.id,
            'sale_order_id': order.id,
            'woo_order_id': str(order.woo_order_id),
            'woo_refund_id': str(refund_id),
            'amount': amount,
            'currency_id': credit_note.currency_id.id or order.currency_id.id,
            'state': 'done',
            'credit_note_id': credit_note.id,
            'woo_reason': reason,
        })
        self._kits_log_operation(
            operation='woo_export_refund',
            status='success',
            level='info',
            message=self.env._(
                'Created WooCommerce refund %(refund)s for %(amount)s on order %(woo)s.',
                refund=refund_id,
                amount=amount,
                woo=order.woo_order_id,
            ),
            details=f'account.move,{credit_note.id}',
        )
        return self._woo_notify(self.env._(
            'WooCommerce refund %(refund)s was created. No additional credit note was created.',
            refund=refund_id,
        ))

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
