# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError


class KitsWooWpMediaCredentials(models.TransientModel):
    """Set WordPress media credentials used for product image export (XML-RPC)."""

    _name = 'kits.woo.wp.media.credentials'
    _description = 'Set WordPress Media Credentials'

    instance_id = fields.Many2one(
        'kits.connector.instance',
        string='WooCommerce Store',
        required=True,
        readonly=True,
        domain="[('platform_type', '=', 'woocommerce')]",
        check_company=True,
    )
    company_id = fields.Many2one(
        related='instance_id.company_id',
        readonly=True,
    )
    username = fields.Char(
        string='Username',
        required=True,
        help='WordPress user that can upload media (application password recommended).',
    )
    password = fields.Char(
        string='Password',
        required=True,
        help='WordPress application password or user password for XML-RPC media upload.',
    )

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        instance = self.env['kits.connector.instance'].browse(
            values.get('instance_id') or self.env.context.get('default_instance_id')
        )
        if instance and instance.exists() and instance._is_woocommerce():
            user, _password = instance._woo_get_wp_media_credentials()
            if 'username' in fields_list and user and not values.get('username'):
                values['username'] = user
            if 'instance_id' in fields_list and not values.get('instance_id'):
                values['instance_id'] = instance.id
        return values

    def action_save(self):
        self.ensure_one()
        if not self.env.user.has_group(
            'kits_odoo_base_connector.group_kits_connector_manager'
        ):
            raise UserError(self.env._(
                'Only Connector Administrators can set WordPress media credentials.'
            ))
        if not self.instance_id._is_woocommerce():
            raise UserError(self.env._(
                'WordPress media credentials are only available for WooCommerce stores.'
            ))
        username = (self.username or '').strip()
        password = (self.password or '').strip()
        if not username or not password:
            raise UserError(self.env._(
                'Username and password are required.'
            ))
        self.instance_id._woo_set_wp_media_credentials(username, password)
        return {'type': 'ir.actions.act_window_close'}
