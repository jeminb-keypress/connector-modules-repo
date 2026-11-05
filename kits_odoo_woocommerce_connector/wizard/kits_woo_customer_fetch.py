# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError


class KitsWooCustomerFetch(models.TransientModel):
    """Fetch a WooCommerce customer by ID into Odoo (create or update by binding)."""

    _name = 'kits.woo.customer.fetch'
    _description = 'Fetch Customer from WooCommerce'

    instance_id = fields.Many2one(
        'kits.connector.instance',
        string='WooCommerce Store',
        required=True,
        domain="[('platform_type', '=', 'woocommerce'), ('active', '=', True)]",
        check_company=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
    )
    woo_customer_id = fields.Char(
        string='WooCommerce Customer ID',
        required=True,
        help='Numeric customer ID from the WooCommerce / WordPress admin URL. '
             'Only WooCommerce users with role "customer" are imported.',
    )
    source_partner_id = fields.Many2one(
        'res.partner',
        string='Opened From',
        readonly=True,
        help='Contact form that opened this wizard (for defaults only). '
             'Fetch matches by store + WooCommerce customer id first. '
             'If no binding exists, a single company contact with the same email '
             'is reused and linked. Multiple email matches require manual review.',
    )

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if 'company_id' in fields_list and not values.get('company_id'):
            values['company_id'] = self.env.company.id
        instance = self.env['kits.connector.instance'].browse(values.get('instance_id'))
        if instance.company_id and 'company_id' in fields_list:
            values['company_id'] = instance.company_id.id
        return values

    @api.onchange('company_id')
    def _onchange_company_id(self):
        if self.instance_id.company_id and self.instance_id.company_id != self.company_id:
            self.instance_id = False

    def action_fetch(self):
        self.ensure_one()
        if not self.env.user.has_group(
            'kits_odoo_base_connector.group_kits_connector_manager'
        ):
            raise UserError(self.env._(
                'Only Connector Administrators can fetch WooCommerce customers.'
            ))
        raw = (self.woo_customer_id or '').strip()
        if not raw.isdigit():
            raise UserError(self.env._(
                'Enter a numeric WooCommerce customer ID.'
            ))
        try:
            partner = self.instance_id._woo_fetch_customer_by_id(int(raw))
        except Exception as exc:  # noqa: BLE001
            raise UserError(self.env._(
                'Could not fetch customer %(id)s from %(store)s: %(error)s',
                id=raw,
                store=self.instance_id.name,
                error=exc,
            )) from exc

        return {
            'type': 'ir.actions.act_window',
            'name': partner.display_name,
            'res_model': 'res.partner',
            'res_id': partner.id,
            'view_mode': 'form',
            'target': 'current',
        }
