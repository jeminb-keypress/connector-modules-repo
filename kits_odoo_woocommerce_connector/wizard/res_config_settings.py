# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    """Centralized WooCommerce settings under the standard Settings app.

    Webhook switches and Public Webhook URL are transient mirrors of the
    selected ``kits.woo.instance``. They are never stored as
    ``ir.config_parameter``. Saving calls ``instance.write()`` so existing
    save-diff webhook orchestration runs only when the user clicks Save.
    """

    _inherit = 'res.config.settings'

    woo_instance_id = fields.Many2one(
        'kits.woo.instance',
        string='Select Instance',
        domain="[('platform_type', '=', 'woocommerce')]",
        help='WooCommerce store whose webhook configuration will be shown '
             'and updated. Only instances for companies you can access appear.',
    )

    public_webhook_url = fields.Char(
        string='Public Webhook URL',
        help='Optional externally reachable Odoo origin for WooCommerce '
             'webhook deliveries. Leave empty to use get_base_url().',
    )
    effective_webhook_base_url = fields.Char(
        string='Effective Webhook Base URL',
        readonly=True,
    )
    public_webhook_url_warning = fields.Char(
        string='Public Webhook URL Warning',
        readonly=True,
    )

    woo_webhook_product_enabled = fields.Boolean(
        string='Manage Products via WooCommerce webhooks',
        help='Register or pause product.created / product.updated / '
             'product.deleted webhooks for the selected store. Product '
             'webhooks never overwrite Odoo stock.',
    )
    woo_webhook_customer_enabled = fields.Boolean(
        string='Manage Customers via WooCommerce webhooks',
        help='Register or pause customer.created / customer.updated / '
             'customer.deleted webhooks for the selected store.',
    )
    woo_webhook_order_enabled = fields.Boolean(
        string='Manage Orders via WooCommerce webhooks',
        help='Register or pause order.created / order.updated / '
             'order.deleted webhooks for the selected store.',
    )
    woo_webhook_coupon_enabled = fields.Boolean(
        string='Manage Coupons via WooCommerce webhooks',
        help='Register or pause coupon.created / coupon.updated / '
             'coupon.deleted webhooks for the selected store.',
    )

    kits_woo_use_sales_description = fields.Boolean(
        string='Use Sales Description',
        config_parameter='kits_woo.use_sales_description',
        help='When enabled, WooCommerce long description maps to Odoo sales '
             'description and short description maps to internal description '
             'on newly created products.',
    )
    kits_woo_odoo_weight_unit = fields.Selection(
        selection=[
            ('kg', 'Kilogram (kg)'),
            ('lbs', 'Pound (lbs)'),
        ],
        string='Odoo Weight Unit',
        config_parameter='kits_woo.odoo_weight_unit',
        default='kg',
        help='Target unit when converting WooCommerce product weights into Odoo.',
    )

    # Scheduler setup (linked to ir.cron records)
    woo_cron_product_waiting_id = fields.Many2one(
        'ir.cron', compute='_compute_woo_product_crons',
    )
    woo_cron_product_images_id = fields.Many2one(
        'ir.cron', compute='_compute_woo_product_crons',
    )
    woo_cron_auto_import_id = fields.Many2one(
        'ir.cron', compute='_compute_woo_product_crons',
    )
    woo_cron_product_waiting_active = fields.Boolean(
        string='Product Waiting List Cron Active',
    )
    woo_cron_product_waiting_interval = fields.Integer(
        string='Product Waiting Interval',
    )
    woo_cron_product_waiting_user_id = fields.Many2one(
        'res.users', string='Product Waiting Running User',
    )
    woo_cron_product_images_active = fields.Boolean(
        string='Product Image Cron Active',
    )
    woo_cron_product_images_interval = fields.Integer(
        string='Product Image Interval (minutes)',
    )
    woo_cron_product_images_user_id = fields.Many2one(
        'res.users', string='Product Image Running User',
    )
    woo_cron_auto_import_active = fields.Boolean(
        string='Daily Product Import Cron Active',
    )
    woo_cron_auto_import_interval = fields.Integer(
        string='Daily Product Import Interval',
    )
    woo_cron_auto_import_user_id = fields.Many2one(
        'res.users', string='Daily Product Import Running User',
    )

    _WOO_SWITCH_FIELDS = (
        'woo_webhook_product_enabled',
        'woo_webhook_customer_enabled',
        'woo_webhook_order_enabled',
        'woo_webhook_coupon_enabled',
    )

    @api.depends()
    def _compute_woo_product_crons(self):
        waiting = self.env.ref(
            'kits_odoo_woocommerce_connector.ir_cron_woo_product_waiting',
            raise_if_not_found=False,
        )
        images = self.env.ref(
            'kits_odoo_woocommerce_connector.ir_cron_woo_product_images',
            raise_if_not_found=False,
        )
        auto = self.env.ref(
            'kits_odoo_woocommerce_connector.ir_cron_woo_auto_import_products',
            raise_if_not_found=False,
        )
        for settings in self:
            settings.woo_cron_product_waiting_id = waiting
            settings.woo_cron_product_images_id = images
            settings.woo_cron_auto_import_id = auto

    def _woo_load_cron_settings(self):
        self._compute_woo_product_crons()
        for settings in self:
            for prefix, cron in (
                ('woo_cron_product_waiting', settings.woo_cron_product_waiting_id),
                ('woo_cron_product_images', settings.woo_cron_product_images_id),
                ('woo_cron_auto_import', settings.woo_cron_auto_import_id),
            ):
                settings[f'{prefix}_active'] = bool(cron.active) if cron else False
                settings[f'{prefix}_interval'] = cron.interval_number if cron else 0
                settings[f'{prefix}_user_id'] = cron.user_id if cron else False

    def _woo_save_cron_settings(self):
        self._compute_woo_product_crons()
        for settings in self:
            mapping = (
                (settings.woo_cron_product_waiting_id, 'woo_cron_product_waiting'),
                (settings.woo_cron_product_images_id, 'woo_cron_product_images'),
                (settings.woo_cron_auto_import_id, 'woo_cron_auto_import'),
            )
            for cron, prefix in mapping:
                if not cron:
                    continue
                cron.sudo().write({
                    'active': bool(settings[f'{prefix}_active']),
                    'interval_number': settings[f'{prefix}_interval'] or cron.interval_number,
                    'user_id': settings[f'{prefix}_user_id'].id or cron.user_id.id,
                })

    def _woo_load_instance_settings(self):
        """Copy the selected store's URL and switches onto this record."""
        for settings in self:
            instance = settings.woo_instance_id
            if instance:
                settings.public_webhook_url = instance.public_webhook_url
                settings.effective_webhook_base_url = instance.effective_webhook_base_url
                settings.public_webhook_url_warning = instance.public_webhook_url_warning
                settings.woo_webhook_product_enabled = instance.woo_webhook_product_enabled
                settings.woo_webhook_customer_enabled = instance.woo_webhook_customer_enabled
                settings.woo_webhook_order_enabled = instance.woo_webhook_order_enabled
                settings.woo_webhook_coupon_enabled = instance.woo_webhook_coupon_enabled
            else:
                settings.public_webhook_url = False
                settings.effective_webhook_base_url = False
                settings.public_webhook_url_warning = False
                settings.woo_webhook_product_enabled = False
                settings.woo_webhook_customer_enabled = False
                settings.woo_webhook_order_enabled = False
                settings.woo_webhook_coupon_enabled = False

    @api.onchange('woo_instance_id')
    def _onchange_woo_instance_id(self):
        self._woo_load_instance_settings()
        self._woo_load_cron_settings()

    @api.onchange('public_webhook_url')
    def _onchange_public_webhook_url(self):
        for settings in self:
            if not settings.woo_instance_id:
                settings.effective_webhook_base_url = False
                settings.public_webhook_url_warning = False
                continue
            Instance = self.env['kits.woo.instance']
            try:
                normalized = Instance._woo_normalize_public_webhook_url(
                    settings.public_webhook_url, raise_error=False,
                )
            except Exception:  # noqa: BLE001
                normalized = ''
            base = normalized or (settings.woo_instance_id.get_base_url() or '').rstrip('/')
            settings.effective_webhook_base_url = base or False
            settings.public_webhook_url_warning = (
                Instance._woo_public_webhook_url_warning(base) if base else False
            )

    @api.model_create_multi
    def create(self, vals_list):
        Instance = self.env['kits.woo.instance']
        for vals in vals_list:
            instance_id = vals.get('woo_instance_id')
            if not instance_id:
                continue
            instance = Instance.browse(instance_id)
            if 'public_webhook_url' not in vals:
                vals['public_webhook_url'] = instance.public_webhook_url
            for field_name in self._WOO_SWITCH_FIELDS:
                if field_name not in vals:
                    vals[field_name] = instance[field_name]
            vals.setdefault(
                'effective_webhook_base_url',
                instance.effective_webhook_base_url,
            )
            vals.setdefault(
                'public_webhook_url_warning',
                instance.public_webhook_url_warning,
            )
        records = super().create(vals_list)
        records._woo_load_cron_settings()
        return records

    def set_values(self):
        super().set_values()
        self._woo_save_cron_settings()
        for settings in self:
            instance = settings.woo_instance_id
            if not instance:
                continue
            # Persist URL first so enable orchestration uses the new base.
            instance.write({
                'public_webhook_url': settings.public_webhook_url or False,
            })
            instance.write({
                'woo_webhook_product_enabled': settings.woo_webhook_product_enabled,
                'woo_webhook_customer_enabled': settings.woo_webhook_customer_enabled,
                'woo_webhook_order_enabled': settings.woo_webhook_order_enabled,
                'woo_webhook_coupon_enabled': settings.woo_webhook_coupon_enabled,
            })

    def action_woo_create_instance(self):
        """Open the existing WooCommerce instance form in create mode."""
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'kits_odoo_woocommerce_connector.kits_woo_instance_action',
        )
        action.update({
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'current',
            'context': {
                **self.env.context,
                'default_platform_type': 'woocommerce',
            },
        })
        return action

    def action_woo_rebuild_webhook_delivery_urls(self):
        """Rebuild remote delivery URLs for the selected store."""
        self.ensure_one()
        if not self.woo_instance_id:
            return False
        return self.woo_instance_id.action_woo_rebuild_webhook_delivery_urls()
