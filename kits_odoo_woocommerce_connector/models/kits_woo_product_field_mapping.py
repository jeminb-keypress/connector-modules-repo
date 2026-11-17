# -*- coding: utf-8 -*-
"""Configurable WooCommerce → Odoo product/variant field mappings."""

import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

_FORBIDDEN_ODOO_FIELDS = frozenset({
    'id', 'create_uid', 'create_date', 'write_uid', 'write_date',
    'company_id', 'product_tmpl_id', 'product_variant_ids', 'product_variant_id',
    'attribute_line_ids', 'message_ids', 'message_follower_ids', 'activity_ids',
    'woo_instance_ids', '__last_update', 'display_name',
})

_ALLOWED_TTYPES = frozenset({
    'char', 'text', 'html', 'float', 'integer', 'boolean', 'selection', 'monetary',
})


class KitsWooProductFieldMapping(models.Model):
    _name = 'kits.woo.product.field.mapping'
    _description = 'WooCommerce Product Extra Field Mapping'
    _order = 'sequence, id'
    _check_company_auto = True

    name = fields.Char(compute='_compute_name', store=True)
    sequence = fields.Integer(default=10)
    instance_id = fields.Many2one(
        'kits.woo.instance',
        string='WooCommerce Store',
        required=True,
        ondelete='cascade',
        index=True,
        domain="[('platform_type', '=', 'woocommerce')]",
    )
    company_id = fields.Many2one(
        'res.company',
        related='instance_id.company_id',
        store=True,
        index=True,
    )
    woo_source = fields.Selection(
        selection=[
            ('field', 'Product/Variation Field'),
            ('meta', 'Meta Data Key'),
        ],
        required=True,
        default='field',
    )
    woo_field = fields.Char(
        string='WooCommerce Source',
        required=True,
        help='Top-level JSON field name, or meta_data key when source is Meta Data.',
    )
    odoo_model = fields.Selection(
        selection=[
            ('product.template', 'Product Template'),
            ('product.product', 'Product Variant'),
        ],
        required=True,
        default='product.template',
    )
    odoo_field = fields.Char(
        string='Odoo Field',
        required=True,
    )
    active = fields.Boolean(default=True)

    @api.depends('woo_field', 'odoo_model', 'odoo_field')
    def _compute_name(self):
        for row in self:
            row.name = f'{row.woo_field or "?"} → {row.odoo_model or "?"}.{row.odoo_field or "?"}'

    @api.constrains('odoo_model', 'odoo_field')
    def _check_odoo_field(self):
        for row in self:
            row._validate_destination_field()

    def _validate_destination_field(self):
        self.ensure_one()
        field_name = (self.odoo_field or '').strip()
        if not field_name or field_name in _FORBIDDEN_ODOO_FIELDS or field_name.startswith('_'):
            raise ValidationError(self.env._(
                'Odoo field %(field)s is not allowed for WooCommerce extra mapping.',
                field=field_name or '?',
            ))
        Model = self.env[self.odoo_model]
        if field_name not in Model._fields:
            raise ValidationError(self.env._(
                'Unknown field %(field)s on %(model)s.',
                field=field_name,
                model=self.odoo_model,
            ))
        field = Model._fields[field_name]
        if field.type not in _ALLOWED_TTYPES:
            raise ValidationError(self.env._(
                'Field %(field)s on %(model)s has unsupported type %(ttype)s.',
                field=field_name,
                model=self.odoo_model,
                ttype=field.type,
            ))
        if getattr(field, 'compute', None) and not getattr(field, 'inverse', None) and not field.related:
            raise ValidationError(self.env._(
                'Computed field %(field)s cannot be written by WooCommerce mapping.',
                field=field_name,
            ))
        return field

    @api.model
    def _extract_woo_value(self, payload, woo_source, woo_field):
        payload = payload or {}
        key = (woo_field or '').strip()
        if not key:
            return None
        if woo_source == 'meta':
            for meta in payload.get('meta_data') or []:
                if not isinstance(meta, dict):
                    continue
                if (meta.get('key') or '') == key:
                    return meta.get('value')
            return None
        return payload.get(key)

    def _convert_value(self, field, value):
        if value is None or value is False:
            return False
        if field.type == 'boolean':
            if isinstance(value, str):
                return value.strip().lower() in ('1', 'true', 'yes', 'on')
            return bool(value)
        if field.type == 'integer':
            return int(float(value))
        if field.type in ('float', 'monetary'):
            return float(value)
        if field.type in ('char', 'text', 'html', 'selection'):
            return str(value) if value is not False else False
        return value

    def _apply_to_records(self, template, variants, product_payload, variant_rows):
        """Apply active mappings. Logs failures; does not raise."""
        for mapping in self:
            try:
                mapping._validate_destination_field()
                field = mapping.env[mapping.odoo_model]._fields[mapping.odoo_field]
                if mapping.odoo_model == 'product.template':
                    raw = mapping._extract_woo_value(
                        product_payload, mapping.woo_source, mapping.woo_field,
                    )
                    if raw is None:
                        continue
                    converted = mapping._convert_value(field, raw)
                    template.with_context(kits_woo_skip_dirty=True).write({
                        mapping.odoo_field: converted,
                    })
                else:
                    for row, variant in variant_rows:
                        raw = mapping._extract_woo_value(
                            row.get('payload') or {}, mapping.woo_source, mapping.woo_field,
                        )
                        if raw is None:
                            continue
                        converted = mapping._convert_value(field, raw)
                        variant.with_context(kits_woo_skip_dirty=True).write({
                            mapping.odoo_field: converted,
                        })
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    'Woo field mapping %s failed on instance %s: %s',
                    mapping.id, mapping.instance_id.id, exc,
                )
                mapping.instance_id._kits_log_operation(
                    operation='woo_product_field_mapping',
                    status='failure',
                    level='warning',
                    message=str(exc),
                    details=f'mapping_id={mapping.id} field={mapping.odoo_field}',
                )
