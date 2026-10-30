# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError

# Fields that affect the WooCommerce product payload (template level).
_WOO_MAPPED_TEMPLATE_FIELDS = frozenset({
    'name',
    'list_price',
    'description',
    'description_sale',
    'default_code',
    'barcode',
    'active',
    'categ_id',
    'product_tag_ids',
    'image_1920',
    'attribute_line_ids',
    'woo_catalog_visibility',
    'woo_featured',
})


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    woo_instance_ids = fields.Many2many(
        'kits.connector.instance',
        'product_template_woo_instance_rel',
        'product_tmpl_id',
        'instance_id',
        string='WooCommerce Stores',
        domain="[('platform_type', '=', 'woocommerce'), ('active', '=', True)]",
        check_company=True,
        copy=False,
        help='Product is exported only to these WooCommerce stores '
             '(manual export, store Export Products, and auto export cron).',
    )
    woo_sync_to_store = fields.Boolean(
        string='Sync to WooCommerce',
        compute='_compute_woo_sync_to_store',
        inverse='_inverse_woo_sync_to_store',
        store=True,
        help='On when at least one WooCommerce store is selected below. '
             'Kept for search/list compatibility.',
    )
    woo_catalog_visibility = fields.Selection(
        selection=[
            ('visible', 'Shop and search results'),
            ('catalog', 'Shop only'),
            ('search', 'Search results only'),
            ('hidden', 'Hidden'),
        ],
        string='Woo Catalog Visibility',
        default='visible',
        copy=False,
    )
    woo_featured = fields.Boolean(
        string='Woo Featured',
        default=False,
        copy=False,
    )
    woo_binding_count = fields.Integer(
        string='Woo Bindings',
        compute='_compute_woo_sync_info',
    )
    woo_sync_state = fields.Selection(
        selection=[
            ('none', 'Not Linked'),
            ('synced', 'Synced'),
            ('not_synced', 'Not Synced'),
            ('error', 'Error'),
            ('deleted', 'Deleted'),
            ('mixed', 'Mixed'),
        ],
        string='Woo Sync Status',
        compute='_compute_woo_sync_info',
        help='Aggregated status across WooCommerce store bindings for this template.',
    )

    def _compute_woo_sync_info(self):
        Binding = self.env['kits.connector.binding']
        for template in self:
            bindings = Binding.search([
                ('odoo_model', '=', 'product.template'),
                ('odoo_record_id', '=', template.id),
                ('platform_type', '=', 'woocommerce'),
                ('active', '=', True),
            ])
            template.woo_binding_count = len(bindings)
            if not bindings:
                template.woo_sync_state = 'none'
                continue
            states = set(bindings.mapped('sync_state'))
            if states == {'synced'}:
                template.woo_sync_state = 'synced'
            elif states == {'not_synced'}:
                template.woo_sync_state = 'not_synced'
            elif states == {'error'}:
                template.woo_sync_state = 'error'
            elif states <= {'deleted', 'deleted_local'}:
                template.woo_sync_state = 'deleted'
            else:
                template.woo_sync_state = 'mixed'

    @api.depends('woo_instance_ids')
    def _compute_woo_sync_to_store(self):
        for template in self:
            template.woo_sync_to_store = bool(template.woo_instance_ids)

    def _inverse_woo_sync_to_store(self):
        """Checkbox helper: turning OFF clears stores; turning ON alone does nothing
        until the user picks stores (or a single-company store is auto-assigned).
        """
        for template in self:
            if not template.woo_sync_to_store:
                template.woo_instance_ids = [(5, 0, 0)]
            elif not template.woo_instance_ids:
                sole = template._woo_get_instances_for_company()
                if len(sole) == 1:
                    template.woo_instance_ids = sole

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get('kits_woo_skip_dirty'):
            return res
        dirty_keys = _WOO_MAPPED_TEMPLATE_FIELDS.intersection(vals)
        if dirty_keys or 'woo_instance_ids' in vals:
            self.env['kits.connector.binding']._kits_mark_records_not_synced(
                'product.template',
                self.ids,
            )
        return res

    def unlink(self):
        variants = self.with_context(active_test=False).mapped('product_variant_ids')
        Binding = self.env['kits.connector.binding']
        message = self.env._(
            'Odoo product was deleted. The WooCommerce product was left unchanged.'
        )
        Binding._kits_mark_odoo_records_deleted_local('product.product', variants.ids, message)
        Binding._kits_mark_odoo_records_deleted_local('product.template', self.ids, message)
        return super().unlink()

    def _woo_get_instances_for_company(self):
        return self.env['kits.connector.instance'].search([
            ('platform_type', '=', 'woocommerce'),
            ('active', '=', True),
            ('company_id', 'in', [False, self.env.company.id]),
        ])

    def _woo_resolve_instance(self, instance=None):
        """Resolve a single store for import (picker when multiple)."""
        if instance:
            return instance
        selected = self.mapped('woo_instance_ids').filtered(
            lambda i: i.active and i.platform_type == 'woocommerce'
        )
        instances = selected or self._woo_get_instances_for_company()
        if not instances:
            raise UserError(self.env._(
                'No active WooCommerce store exists for the current company.'
            ))
        if len(instances) > 1:
            return False
        return instances

    def action_woo_export_to_store(self):
        """Push products to every WooCommerce store selected on each product."""
        self._check_woo_sync_access()
        selected = self.mapped('woo_instance_ids').filtered(
            lambda i: i.active and i.platform_type == 'woocommerce'
        )
        if not selected:
            # Fall back to picker so user can choose a store (also adds it on enqueue)
            instance = self._woo_resolve_instance()
            if not instance:
                return self._woo_open_instance_picker('export')
            return self._woo_enqueue_export(instance)

        last_notify = False
        for instance in selected:
            templates = self.filtered(lambda t, inst=instance: inst in t.woo_instance_ids)
            if templates:
                last_notify = templates._woo_enqueue_export(instance)
        if not last_notify:
            raise UserError(self.env._(
                'Select WooCommerce store(s) on the product WooCommerce tab before exporting.'
            ))
        return last_notify

    def action_woo_import_from_store(self):
        """Pull already-linked products from WooCommerce into Odoo."""
        self._check_woo_sync_access()
        instance = self._woo_resolve_instance()
        if not instance:
            return self._woo_open_instance_picker('import')
        return self._woo_enqueue_import(instance)

    def _check_woo_sync_access(self):
        if not self.env.user.has_group(
            'kits_odoo_base_connector.group_kits_connector_manager'
        ):
            raise UserError(self.env._(
                'Only Connector Administrators can synchronize WooCommerce products.'
            ))

    def _woo_open_instance_picker(self, operation):
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Choose WooCommerce Store'),
            'res_model': 'kits.woo.instance.picker',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_operation': operation,
                'default_template_ids': [(6, 0, self.ids)],
            },
        }

    def _woo_enqueue_export(self, instance):
        instance._ensure_woo_ready()
        # Ensure this store is selected on the products being exported.
        to_link = self.filtered(lambda t: instance not in t.woo_instance_ids)
        if to_link:
            to_link.write({'woo_instance_ids': [(4, instance.id)]})
        Binding = self.env['kits.connector.binding']
        bindings = Binding.search([
            ('instance_id', '=', instance.id),
            ('odoo_model', '=', 'product.template'),
            ('odoo_record_id', 'in', self.ids),
        ])
        if bindings:
            bindings._kits_mark_not_synced()
        sync_versions = {
            str(b.odoo_record_id): b.sync_version for b in bindings
        }
        instance.action_create_sync_job(
            'woo_export_products',
            name=self.env._(
                'WooCommerce: Export Products (%(store)s) — %(count)s selected',
                store=instance.name,
                count=len(self),
            ),
            payload={
                'template_ids': self.ids,
                'sync_versions': sync_versions,
                'allow_recreate': True,
            },
        )
        return instance._woo_notify(self.env._(
            'Queued export of %(count)s product(s) to %(store)s.',
            count=len(self),
            store=instance.name,
        ))

    def _woo_enqueue_import(self, instance):
        instance._ensure_woo_ready()
        Binding = self.env['kits.connector.binding']
        woo_ids = []
        missing = []
        for template in self:
            binding = Binding._kits_find_binding(
                instance, 'product.template', odoo_record_id=template.id,
            )
            if not binding:
                missing.append(template.display_name)
                continue
            woo_ids.append(int(binding.external_id))
        if missing and not woo_ids:
            raise UserError(self.env._(
                'None of the selected products are linked to %(store)s. '
                'Import them once from the store or export them first.',
                store=instance.name,
            ))
        if missing:
            # Partial: still pull linked ones
            pass
        instance.action_create_sync_job(
            'woo_import_products',
            name=self.env._(
                'WooCommerce: Import Products (%(store)s) — %(count)s selected',
                store=instance.name,
                count=len(woo_ids),
            ),
            payload={'woo_ids': woo_ids, 'allow_recreate': True},
        )
        message = self.env._(
            'Queued update of %(count)s linked product(s) from %(store)s.',
            count=len(woo_ids),
            store=instance.name,
        )
        if missing:
            message = self.env._(
                '%(message)s Skipped %(skipped)s unlinked product(s).',
                message=message,
                skipped=len(missing),
            )
        return instance._woo_notify(message)
