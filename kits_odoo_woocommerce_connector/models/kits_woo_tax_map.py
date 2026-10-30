# -*- coding: utf-8 -*-
"""Per-instance WooCommerce tax rate / tax class → Odoo account.tax mapping."""

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class KitsWooTaxMap(models.Model):
    _name = 'kits.woo.tax.map'
    _description = 'WooCommerce Tax Mapping'
    _order = 'woo_tax_class, woo_rate_id, id'
    _check_company_auto = True

    instance_id = fields.Many2one(
        'kits.connector.instance',
        required=True,
        ondelete='cascade',
        index=True,
        domain="[('platform_type', '=', 'woocommerce')]",
        check_company=True,
    )
    company_id = fields.Many2one(
        related='instance_id.company_id',
        store=True,
        readonly=True,
    )
    name = fields.Char(
        string='Label',
        help='Optional display label (defaults to Woo rate name/class).',
    )
    woo_rate_id = fields.Char(
        string='Woo Tax Rate ID',
        required=True,
        index=True,
        help='WooCommerce tax rate id from /taxes. For class-only manual rows use a '
             'synthetic key such as class:standard:20.',
    )
    woo_tax_class = fields.Char(
        string='Woo Tax Class',
        default='',
        help='WooCommerce tax class slug (empty = standard).',
    )
    woo_rate = fields.Float(
        string='Woo Rate %',
        digits=(16, 4),
        help='Expected WooCommerce percentage rate. Used when rate id is absent '
             'or as a validation hint.',
    )
    woo_name = fields.Char(string='Woo Rate Name', readonly=True)
    tax_id = fields.Many2one(
        'account.tax',
        string='Odoo Sales Tax',
        required=False,
        check_company=True,
        domain="[('type_tax_use', '=', 'sale'), ('company_id', 'in', [company_id, False])]",
        help='Mapped Odoo tax. Leave empty and enable Zero/Exempt to force no tax.',
    )
    is_zero_rated = fields.Boolean(
        string='Zero / Exempt',
        help='When enabled, lines matching this map get no Odoo tax '
             '(never apply an unrelated default).',
    )
    match_price_include = fields.Selection(
        selection=[
            ('any', 'Any'),
            ('excluded', 'Tax Excluded Prices'),
            ('included', 'Tax Included Prices'),
        ],
        string='Price Include Match',
        default='any',
        required=True,
        help='Restrict this mapping to Woo orders whose prices_include_tax matches.',
    )
    active = fields.Boolean(default=True)
    notes = fields.Char()

    _tax_map_instance_rate_uniq = models.Constraint(
        'UNIQUE(instance_id, woo_rate_id)',
        'Each WooCommerce tax rate id can be mapped once per store. '
        'For class-only rows use a synthetic id such as class:standard:20.',
    )

    @api.constrains('tax_id', 'is_zero_rated')
    def _check_tax_or_zero(self):
        for row in self:
            if row.is_zero_rated and row.tax_id:
                raise ValidationError(self.env._(
                    'Tax mapping "%(name)s" cannot set both an Odoo tax and Zero/Exempt.',
                    name=row.display_name,
                ))
            if row.tax_id and row.tax_id.type_tax_use != 'sale':
                raise ValidationError(self.env._(
                    'Mapped tax must be a sales tax (type_tax_use=sale).'
                ))

    def is_configured(self):
        self.ensure_one()
        return bool(self.is_zero_rated or self.tax_id)

    @api.depends('name', 'woo_name', 'woo_rate_id', 'woo_tax_class', 'woo_rate')
    def _compute_display_name(self):
        for row in self:
            label = row.name or row.woo_name or row.woo_tax_class or self.env._('Tax map')
            bits = [label]
            if row.woo_rate_id:
                bits.append(f'#{row.woo_rate_id}')
            if row.woo_rate:
                bits.append(f'{row.woo_rate:g}%')
            row.display_name = ' '.join(bits)
