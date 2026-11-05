# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError


class KitsWooCustomerBinding(models.Model):
    """Links one Odoo contact to one WooCommerce customer on one store.

    Name, email, phone, and addresses stay on ``res.partner``. This record stores
    the store, external customer id, and sync bookkeeping. Remote WooCommerce
    deletion archives the binding (``deleted_remotely``) and keeps history.
    """

    _name = 'kits.woo.customer.binding'
    _description = 'WooCommerce Customer Binding'
    _order = 'id desc'
    _check_company_auto = True
    _rec_name = 'external_id'

    instance_id = fields.Many2one(
        'kits.connector.instance',
        string='WooCommerce Store',
        required=True,
        index=True,
        ondelete='cascade',
        domain="[('platform_type', '=', 'woocommerce')]",
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        index=True,
        default=lambda self: self.env.company,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Odoo Contact',
        required=True,
        index=True,
        ondelete='cascade',
        check_company=True,
    )
    external_id = fields.Char(
        string='External Customer ID',
        required=True,
        index=True,
    )
    active = fields.Boolean(default=True)
    sync_state = fields.Selection(
        selection=[
            ('synced', 'Synced'),
            ('not_synced', 'Not Synced'),
            ('error', 'Error'),
            ('deleted_remotely', 'Deleted Remotely'),
            ('manual_review', 'Manual Review'),
        ],
        default='synced',
        required=True,
        index=True,
    )
    sync_error = fields.Text(copy=False)
    sync_date = fields.Datetime(default=fields.Datetime.now)
    deleted_remotely_at = fields.Datetime(
        string='Deleted Remotely At',
        readonly=True,
        copy=False,
    )

    _external_uniq = models.Constraint(
        'UNIQUE(instance_id, external_id)',
        'This WooCommerce customer is already linked for this store.',
    )
    _partner_uniq = models.Constraint(
        'UNIQUE(instance_id, partner_id)',
        'This contact is already linked to this WooCommerce store.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('instance_id') and not vals.get('company_id'):
                instance = self.env['kits.connector.instance'].browse(vals['instance_id'])
                vals['company_id'] = instance.company_id.id
            if vals.get('external_id'):
                vals['external_id'] = str(vals['external_id'])
        return super().create(vals_list)

    @api.depends('partner_id', 'external_id', 'instance_id', 'sync_state')
    def _compute_display_name(self):
        for binding in self:
            contact = binding.partner_id.display_name or binding.external_id
            store = binding.instance_id.display_name or ''
            suffix = ''
            if binding.sync_state == 'deleted_remotely':
                suffix = ' [deleted remotely]'
            binding.display_name = f'{contact} ↔ {binding.external_id} ({store}){suffix}'

    @api.constrains('external_id')
    def _check_external_id(self):
        for binding in self:
            if not (binding.external_id or '').strip():
                raise ValidationError(self.env._('External Customer ID is required.'))

    @api.model
    def _find(self, instance, *, external_id=False, odoo_record_id=False):
        if not instance:
            raise UserError(self.env._('A WooCommerce store is required.'))
        domain = [('instance_id', '=', instance.id)]
        if external_id:
            domain.append(('external_id', '=', str(external_id)))
        elif odoo_record_id:
            domain.append(('partner_id', '=', int(odoo_record_id)))
        else:
            raise UserError(self.env._('Provide an external customer id or a contact.'))
        return self.with_context(active_test=False).search(domain, limit=1)

    @api.model
    def _bind(self, instance, partner_id, external_id):
        if not instance or not partner_id or not external_id:
            raise UserError(self.env._(
                'Store, contact, and external customer id are required.'
            ))
        external_id = str(external_id)
        partner_id = int(partner_id)
        by_external = self._find(instance, external_id=external_id)
        by_partner = self._find(instance, odoo_record_id=partner_id)
        if by_external and by_partner and by_external != by_partner:
            raise UserError(self.env._(
                'Customer %(customer)s and contact %(partner)s are already linked to different records on %(store)s.',
                customer=external_id,
                partner=partner_id,
                store=instance.display_name,
            ))
        binding = by_external or by_partner
        if binding:
            if binding.partner_id.id != partner_id or binding.external_id != external_id:
                raise UserError(self.env._(
                    'This store already has a different customer link for that contact or external id.'
                ))
            vals = {
                'active': True,
                'sync_state': 'synced',
                'sync_error': False,
                'sync_date': fields.Datetime.now(),
                'deleted_remotely_at': False,
            }
            binding.write(vals)
            return binding
        return self.create({
            'instance_id': instance.id,
            'company_id': instance.company_id.id,
            'partner_id': partner_id,
            'external_id': external_id,
            'sync_state': 'synced',
            'sync_date': fields.Datetime.now(),
        })

    def _kits_get_odoo_record(self):
        self.ensure_one()
        return self.partner_id.exists()

    def _kits_mark_synced(self):
        self.write({
            'active': True,
            'sync_state': 'synced',
            'sync_error': False,
            'sync_date': fields.Datetime.now(),
            'deleted_remotely_at': False,
        })
        return True

    def _kits_mark_error(self, message):
        self.write({
            'sync_state': 'error',
            'sync_error': message or False,
            'sync_date': fields.Datetime.now(),
        })
        return True

    def _kits_mark_manual_review(self, message):
        self.write({
            'sync_state': 'manual_review',
            'sync_error': message or False,
            'sync_date': fields.Datetime.now(),
        })
        return True

    def _kits_mark_deleted_remotely(self, *, message=False):
        """Archive this store binding. Keep the Odoo contact and history."""
        self.write({
            'active': False,
            'sync_state': 'deleted_remotely',
            'sync_error': message or False,
            'deleted_remotely_at': fields.Datetime.now(),
            'sync_date': fields.Datetime.now(),
        })
        return True
