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
        'res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
    )
    operation = fields.Selection(
        selection=[
            ('import_customers', 'Import Customers'),
            ('import_products', 'Import Products'),
            ('import_specific_products', 'Import Specific Products'),
            ('import_unshipped_orders', 'Import Unshipped Orders'),
            ('import_shipped_orders', 'Import Shipped Orders'),
            ('import_cancelled_orders', 'Import Cancelled Orders'),
            ('import_specific_orders', 'Import/Update Specific Orders'),
            ('import_categories', 'Import Categories'),
            ('import_tags', 'Import Tags'),
            ('import_attributes', 'Import Attributes'),
            ('import_coupons', 'Import Coupons'),
            ('map_products', 'Map Products using CSV/Excel'),
            ('export_shipment', 'Export Shipment Information / Update Order Status'),
            ('export_stock', 'Export Stock'),
            ('export_categories', 'Export Categories'),
            ('update_categories', 'Update Categories'),
            ('export_tags', 'Export Tags'),
            ('update_tags', 'Update Tags'),
            ('export_coupons', 'Export Coupons'),
            ('update_coupons', 'Update Coupons'),
        ],
        required=True,
        default='import_customers',
    )
    product_import_mode = fields.Selection(
        selection=[
            ('all', 'Import All'),
            ('new_and_updated', 'Import New and Updated Only'),
        ],
        string='Import Mode',
        default='all',
    )
    skip_existing = fields.Boolean(string='Do Not Update Existing Products')
    product_ids_text = fields.Char(
        string='WooCommerce Product IDs',
        help='Comma-separated WooCommerce product ids.',
    )
    order_ids_text = fields.Char(
        string='WooCommerce Order IDs',
        help='Comma-separated WooCommerce order ids.',
    )
    date_from = fields.Datetime(string='Date From')
    date_to = fields.Datetime(string='Date To')
    stock_from = fields.Datetime(string='Export Stock From')
    mapping_file = fields.Binary(string='Mapping File')
    mapping_filename = fields.Char(string='Filename')

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

    @api.constrains(
        'operation', 'product_ids_text', 'order_ids_text', 'date_from', 'date_to',
        'mapping_file', 'mapping_filename',
    )
    def _check_operation_values(self):
        for wiz in self:
            if wiz.operation == 'import_specific_products' and not wiz._parse_ids(wiz.product_ids_text):
                raise ValidationError(self.env._('Enter at least one WooCommerce product id.'))
            if wiz.operation == 'import_specific_orders' and not wiz._parse_ids(wiz.order_ids_text):
                raise ValidationError(self.env._('Enter at least one WooCommerce order id.'))
            if wiz.date_from and wiz.date_to and wiz.date_to < wiz.date_from:
                raise ValidationError(self.env._('Date To must be on or after Date From.'))
            if wiz.operation == 'map_products':
                if not wiz.mapping_file:
                    raise ValidationError(self.env._('Upload a CSV or Excel mapping file.'))
                name = (wiz.mapping_filename or '').lower()
                if name and not name.endswith(('.csv', '.xlsx')):
                    raise ValidationError(self.env._('Upload a .csv or .xlsx file.'))

    def action_execute(self):
        self.ensure_one()
        instance = self.instance_id
        require_wh = self.operation in (
            'import_unshipped_orders', 'import_shipped_orders', 'import_cancelled_orders',
            'import_specific_orders', 'export_stock', 'export_shipment',
        )
        instance._ensure_woo_ready(require_warehouse=require_wh)
        if self.operation == 'map_products':
            return instance._woo_map_products(self.mapping_file, self.mapping_filename)

        job_type, name, payload = self._prepare_job()
        existing = self.env['kits.connector.sync.job'].search([
            ('instance_id', '=', instance.id),
            ('job_type', '=', job_type),
            ('state', 'in', ('pending', 'running')),
        ], limit=20)
        for job in existing:
            if (job.payload or {}) == payload:
                return instance._woo_open_jobs(job, self.env._(
                    'This operation is already pending or running for %(store)s.',
                    store=instance.display_name,
                ))
        job = instance.action_create_sync_job(job_type, name=name, payload=payload)
        return instance._woo_open_jobs(job, self.env._('Queued %(name)s.', name=name))

    def _parse_ids(self, raw):
        return self.env['kits.connector.instance']._woo_parse_id_list(raw)

    def _prepare_job(self):
        self.ensure_one()
        instance = self.instance_id
        operation = self.operation
        label = dict(self._fields['operation']._description_selection(self.env)).get(operation, operation)
        name = self.env._('WooCommerce: %(operation)s (%(store)s)', operation=label, store=instance.name)

        if operation == 'import_customers':
            return 'woo_import_customers', name, {}
        if operation == 'import_products':
            payload = {}
            if self.product_import_mode == 'new_and_updated':
                payload['product_mode'] = 'new_and_updated'
            if self.skip_existing:
                payload['skip_existing'] = True
            return 'woo_import_products', name, payload
        if operation == 'import_specific_products':
            return 'woo_import_products', name, {
                'woo_ids': self._parse_ids(self.product_ids_text),
                'allow_recreate': True,
            }
        if operation == 'import_unshipped_orders':
            return 'woo_import_orders', name, self._order_payload('unshipped')
        if operation == 'import_shipped_orders':
            return 'woo_import_orders', name, self._order_payload('shipped')
        if operation == 'import_cancelled_orders':
            return 'woo_import_orders', name, {'order_scope': 'cancelled'}
        if operation == 'import_specific_orders':
            return 'woo_import_orders', name, {
                'order_scope': 'specific',
                'woo_ids': self._parse_ids(self.order_ids_text),
            }
        if operation == 'import_categories':
            return 'woo_import_categories', name, {}
        if operation == 'import_tags':
            return 'woo_import_tags', name, {}
        if operation == 'import_attributes':
            return 'woo_import_attributes', name, {}
        if operation == 'import_coupons':
            return 'woo_import_coupons', name, {}
        if operation == 'export_shipment':
            return 'woo_export_shipments', name, {}
        if operation == 'export_stock':
            payload = {'full': not bool(self.stock_from)}
            if self.stock_from:
                payload['stock_from'] = fields.Datetime.to_string(self.stock_from)
            return 'woo_export_inventory', name, payload
        if operation == 'export_categories':
            return 'woo_export_categories', name, {'category_mode': 'export'}
        if operation == 'update_categories':
            return 'woo_export_categories', name, {'category_mode': 'update'}
        if operation == 'export_tags':
            return 'woo_export_tags', name, {'tag_mode': 'export'}
        if operation == 'update_tags':
            return 'woo_export_tags', name, {'tag_mode': 'update'}
        if operation == 'export_coupons':
            return 'woo_export_coupons', name, {'coupon_mode': 'export'}
        if operation == 'update_coupons':
            return 'woo_export_coupons', name, {'coupon_mode': 'update'}
        raise UserError(self.env._('Unsupported synchronization operation.'))

    def _order_payload(self, scope):
        self.ensure_one()
        payload = {'order_scope': scope}
        if self.date_from:
            payload['date_from'] = fields.Datetime.to_string(self.date_from)
        if self.date_to:
            payload['date_to'] = fields.Datetime.to_string(self.date_to)
        return payload
