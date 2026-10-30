# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError


class KitsWooSyncWizard(models.TransientModel):
    _name = 'kits.woo.sync.wizard'
    _description = 'WooCommerce Specific Sync Operation'

    instance_id = fields.Many2one(
        'kits.connector.instance',
        string='WooCommerce Store',
        required=True,
        domain="[('platform_type', '=', 'woocommerce'), ('active', '=', True)]",
        check_company=True,
    )
    company_id = fields.Many2one(
        related='instance_id.company_id',
        store=True,
        readonly=True,
    )
    direction = fields.Selection(
        selection=[
            ('import', 'WooCommerce → Odoo'),
            ('export', 'Odoo → WooCommerce'),
        ],
        required=True,
        default='import',
    )
    operation = fields.Selection(
        selection=[
            ('products', 'Products'),
            ('customers', 'Customers'),
            ('orders', 'Orders'),
            ('inventory', 'Inventory'),
            ('categories', 'Categories'),
            ('tags', 'Tags'),
            ('attributes', 'Attributes'),
            ('coupons', 'Coupons'),
        ],
        required=True,
        default='products',
    )
    import_mode = fields.Selection(
        selection=[
            ('all', 'Import All / Bulk'),
            ('ids', 'Specific IDs'),
            ('date', 'Modified After Date'),
        ],
        string='Filter',
        default='all',
        required=True,
    )
    record_ids_text = fields.Char(
        string='Record IDs',
        help='Comma-separated WooCommerce IDs (import) or Odoo product template IDs (export products).',
    )
    modified_after = fields.Datetime(
        string='Modified After',
    )

    @api.onchange('direction', 'operation')
    def _onchange_direction_operation(self):
        if self.direction == 'export' and self.operation == 'orders':
            self.operation = 'products'
        if self.direction == 'export' and self.operation == 'customers':
            self.operation = 'products'
        if self.direction == 'import' and self.operation == 'inventory':
            self.operation = 'products'
        if self.direction == 'export' and self.import_mode == 'date':
            self.import_mode = 'all'

    @api.constrains('direction', 'operation', 'import_mode', 'record_ids_text', 'modified_after')
    def _check_wizard_values(self):
        for wiz in self:
            if wiz.direction == 'export' and wiz.operation == 'orders':
                raise ValidationError(self.env._('Order export is not supported.'))
            if wiz.direction == 'export' and wiz.operation == 'customers':
                raise ValidationError(self.env._(
                    'Customer export is not supported. Import or Fetch instead.'
                ))
            if wiz.direction == 'import' and wiz.operation == 'inventory':
                raise ValidationError(self.env._(
                    'Inventory import from WooCommerce is not supported. '
                    'Odoo remains the stock source of truth.'
                ))
            if wiz.direction == 'export' and wiz.operation in ('tags', 'attributes'):
                raise ValidationError(self.env._(
                    'Export for tags/attributes is not supported from this wizard yet. '
                    'Use product export after importing attributes.'
                ))
            if wiz.import_mode == 'ids' and not (wiz.record_ids_text or '').strip():
                raise ValidationError(self.env._('Enter at least one record ID.'))
            if wiz.import_mode == 'date':
                if wiz.direction != 'import':
                    raise ValidationError(self.env._(
                        'Date filter is only available for import operations.'
                    ))
                if wiz.operation not in ('products', 'orders', 'customers', 'coupons'):
                    raise ValidationError(self.env._(
                        'Date filter is not supported for this operation.'
                    ))
                if not wiz.modified_after:
                    raise ValidationError(self.env._('Set the Modified After date.'))

    def action_execute(self):
        self.ensure_one()
        instance = self.instance_id
        require_wh = self.operation in ('orders', 'inventory')
        instance._ensure_woo_ready(require_warehouse=require_wh)

        job_type, name, payload = self._prepare_job()
        # Deduplicate identical pending bulk jobs for the same store/type/payload key
        existing = self.env['kits.connector.sync.job'].search([
            ('instance_id', '=', instance.id),
            ('job_type', '=', job_type),
            ('state', 'in', ('pending', 'running')),
        ], limit=20)
        for job in existing:
            if (job.payload or {}) == payload:
                return instance._woo_notify(self.env._(
                    'An identical sync job is already pending or running.'
                ))

        instance.action_create_sync_job(job_type, name=name, payload=payload)
        return instance._woo_notify(self.env._(
            'Queued %(name)s.',
            name=name,
        ))

    def _parse_ids(self):
        self.ensure_one()
        return self.env['kits.connector.instance']._woo_parse_id_list(self.record_ids_text)

    def _prepare_job(self):
        self.ensure_one()
        instance = self.instance_id
        ids = self._parse_ids() if self.import_mode == 'ids' else []
        modified = self.modified_after if self.import_mode == 'date' else False

        if self.direction == 'import' and self.operation == 'products':
            payload = {}
            if ids:
                payload['woo_ids'] = ids
                payload['allow_recreate'] = True
            if modified:
                payload['modified_after'] = fields.Datetime.to_string(modified)
            return (
                'woo_import_products',
                self.env._('WooCommerce: Import Products (%s)', instance.name),
                payload,
            )
        if self.direction == 'export' and self.operation == 'products':
            payload = {}
            if ids:
                payload['template_ids'] = ids
                payload['allow_recreate'] = True
            elif self.import_mode == 'all':
                # Explicit bulk export of flagged products only
                payload = {}
            else:
                raise UserError(self.env._('Provide Odoo product template IDs to export.'))
            return (
                'woo_export_products',
                self.env._('WooCommerce: Export Products (%s)', instance.name),
                payload,
            )
        if self.direction == 'import' and self.operation == 'customers':
            payload = {}
            if ids:
                payload['woo_ids'] = ids
            if modified:
                payload['modified_after'] = fields.Datetime.to_string(modified)
            return (
                'woo_import_customers',
                self.env._('WooCommerce: Import Customers (%s)', instance.name),
                payload,
            )
        if self.direction == 'export' and self.operation == 'customers':
            raise UserError(self.env._(
                'WooCommerce customer export is not supported. '
                'Use Import Customers or Fetch Customer instead.'
            ))
        if self.direction == 'import' and self.operation == 'orders':
            payload = {}
            if ids:
                payload['woo_ids'] = ids
            if modified:
                payload['after'] = fields.Datetime.to_string(modified)
            return (
                'woo_import_orders',
                self.env._('WooCommerce: Import Orders (%s)', instance.name),
                payload,
            )
        if self.direction == 'export' and self.operation == 'inventory':
            payload = {'full': True}
            if ids:
                payload = {'product_ids': ids, 'full': False}
            return (
                'woo_export_inventory',
                self.env._('WooCommerce: Export Inventory (%s)', instance.name),
                payload,
            )
        if self.direction == 'import' and self.operation == 'categories':
            payload = {'woo_ids': ids} if ids else {}
            return (
                'woo_import_categories',
                self.env._('WooCommerce: Import Categories (%s)', instance.name),
                payload,
            )
        if self.direction == 'export' and self.operation == 'categories':
            payload = {'category_ids': ids} if ids else {}
            return (
                'woo_export_categories',
                self.env._('WooCommerce: Export Categories (%s)', instance.name),
                payload,
            )
        if self.direction == 'import' and self.operation == 'tags':
            payload = {'woo_ids': ids} if ids else {}
            return (
                'woo_import_tags',
                self.env._('WooCommerce: Import Tags (%s)', instance.name),
                payload,
            )
        if self.direction == 'import' and self.operation == 'attributes':
            payload = {'woo_ids': ids} if ids else {}
            return (
                'woo_import_attributes',
                self.env._('WooCommerce: Import Attributes (%s)', instance.name),
                payload,
            )
        if self.direction == 'import' and self.operation == 'coupons':
            payload = {}
            if ids:
                payload['woo_ids'] = ids
            if modified:
                payload['modified_after'] = fields.Datetime.to_string(modified)
            return (
                'woo_import_coupons',
                self.env._('WooCommerce: Import Coupons (%s)', instance.name),
                payload,
            )
        if self.direction == 'export' and self.operation == 'coupons':
            payload = {'coupon_ids': ids} if ids else {}
            return (
                'woo_export_coupons',
                self.env._('WooCommerce: Export Coupons (%s)', instance.name),
                payload,
            )
        raise UserError(self.env._('Unsupported synchronization operation.'))
