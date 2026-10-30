# -*- coding: utf-8 -*-
"""Local mirror of a WooCommerce coupon for one connector instance."""

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class KitsWooCoupon(models.Model):
    _name = 'kits.woo.coupon'
    _description = 'WooCommerce Coupon'
    _order = 'code, id'
    _check_company_auto = True

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    instance_id = fields.Many2one(
        'kits.connector.instance',
        required=True,
        ondelete='cascade',
        index=True,
        domain="[('platform_type', '=', 'woocommerce')]",
        check_company=True,
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    discount_type = fields.Selection(
        selection=[
            ('percent', 'Percentage'),
            ('fixed_cart', 'Fixed Cart'),
            ('fixed_product', 'Fixed Product'),
        ],
        required=True,
        default='percent',
    )
    amount = fields.Float(required=True)
    date_expires = fields.Date(string='Expiry Date')
    usage_limit = fields.Integer(string='Usage Limit (Total)')
    usage_limit_per_user = fields.Integer(string='Usage Limit Per User')
    individual_use = fields.Boolean(default=False)
    free_shipping = fields.Boolean(default=False)
    minimum_amount = fields.Float()
    maximum_amount = fields.Float()
    product_ids = fields.Many2many(
        'product.product',
        'kits_woo_coupon_product_rel',
        'coupon_id',
        'product_id',
        string='Included Products',
        help='Odoo products this coupon applies to (resolved via this store’s bindings).',
        check_company=True,
    )
    excluded_product_ids = fields.Many2many(
        'product.product',
        'kits_woo_coupon_excluded_product_rel',
        'coupon_id',
        'product_id',
        string='Excluded Products',
        check_company=True,
    )
    product_category_ids = fields.Many2many(
        'product.category',
        'kits_woo_coupon_categ_rel',
        'coupon_id',
        'category_id',
        string='Included Categories',
    )
    excluded_product_category_ids = fields.Many2many(
        'product.category',
        'kits_woo_coupon_excluded_categ_rel',
        'coupon_id',
        'category_id',
        string='Excluded Categories',
    )
    product_ids_text = fields.Char(
        string='Included Woo Product IDs',
        help='Raw WooCommerce product/variation IDs for included restrictions. '
             'Kept when bindings are missing so eligibility is never silently broadened.',
    )
    excluded_product_ids_text = fields.Char(
        string='Excluded Woo Product IDs',
        help='Raw WooCommerce product/variation IDs for exclusions.',
    )
    product_categories_text = fields.Char(
        string='Included Woo Category IDs',
    )
    excluded_product_categories_text = fields.Char(
        string='Excluded Woo Category IDs',
    )
    unresolved_restriction_ids = fields.Text(
        string='Unresolved Restriction IDs',
        readonly=True,
        help='Woo IDs that could not be mapped to Odoo bindings for this store.',
    )
    restriction_error = fields.Text(
        string='Restriction Sync Error',
        readonly=True,
        help='Set when included/excluded restrictions cannot be represented safely.',
    )
    email_restrictions = fields.Char(
        help='Comma-separated allowed emails (WooCommerce email_restrictions).',
    )
    description = fields.Text()
    active = fields.Boolean(default=True)
    sync_to_woo = fields.Boolean(
        string='Sync to WooCommerce',
        default=True,
        help='When enabled, export/update pushes this coupon to the store.',
    )
    woo_external_id = fields.Char(
        string='Woo Coupon ID',
        compute='_compute_woo_external_id',
    )
    sync_state = fields.Selection(
        selection=[
            ('synced', 'Synced'),
            ('not_synced', 'Not Synced'),
            ('error', 'Error'),
            ('deleted', 'Deleted Remote'),
        ],
        compute='_compute_woo_external_id',
    )

    _code_instance_uniq = models.Constraint(
        'UNIQUE(instance_id, code)',
        'Coupon code must be unique per WooCommerce store.',
    )

    @api.onchange('instance_id')
    def _onchange_instance_id(self):
        if self.instance_id:
            self.company_id = self.instance_id.company_id

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('instance_id') and not vals.get('company_id'):
                instance = self.env['kits.connector.instance'].browse(vals['instance_id'])
                vals['company_id'] = instance.company_id.id
            if vals.get('code'):
                vals['code'] = vals['code'].strip()
            if vals.get('code') and not vals.get('name'):
                vals['name'] = vals['code']
        return super().create(vals_list)

    @api.constrains('amount', 'discount_type')
    def _check_amount(self):
        for coupon in self:
            if coupon.amount < 0:
                raise ValidationError(self.env._('Coupon amount cannot be negative.'))
            if coupon.discount_type == 'percent' and coupon.amount > 100:
                raise ValidationError(self.env._('Percentage coupon amount cannot exceed 100.'))

    @api.depends('instance_id')
    def _compute_woo_external_id(self):
        Binding = self.env['kits.connector.binding']
        for coupon in self:
            binding = Binding.search([
                ('instance_id', '=', coupon.instance_id.id),
                ('odoo_model', '=', 'kits.woo.coupon'),
                ('odoo_record_id', '=', coupon.id),
            ], limit=1)
            coupon.woo_external_id = binding.external_id if binding else False
            coupon.sync_state = binding.sync_state if binding else 'not_synced'

    def write(self, vals):
        if 'code' in vals and vals['code']:
            vals = dict(vals, code=vals['code'].strip())
        res = super().write(vals)
        mapped = {
            'name', 'code', 'discount_type', 'amount', 'date_expires', 'usage_limit',
            'usage_limit_per_user', 'individual_use', 'free_shipping', 'minimum_amount',
            'maximum_amount', 'product_ids_text', 'excluded_product_ids_text',
            'product_categories_text', 'excluded_product_categories_text',
            'product_ids', 'excluded_product_ids',
            'product_category_ids', 'excluded_product_category_ids',
            'email_restrictions', 'description', 'active',
        }
        if mapped.intersection(vals):
            Binding = self.env['kits.connector.binding']
            for coupon in self:
                binding = Binding.search([
                    ('instance_id', '=', coupon.instance_id.id),
                    ('odoo_model', '=', 'kits.woo.coupon'),
                    ('odoo_record_id', '=', coupon.id),
                ], limit=1)
                if binding:
                    binding._kits_mark_not_synced()
        return res
