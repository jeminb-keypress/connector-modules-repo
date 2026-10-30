# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError


class KitsConnectorBinding(models.Model):
    """Maps an external platform record to an Odoo record for one connector instance.

    Bindings prevent duplicate imports/exports by uniquely identifying external
    IDs (and optionally the Odoo record) per instance.
    """

    _name = 'kits.connector.binding'
    _description = 'Connector External Binding'
    _order = 'sync_date desc, id desc'
    _check_company_auto = True
    _rec_name = 'external_id'

    instance_id = fields.Many2one(
        'kits.connector.instance',
        required=True,
        index=True,
        ondelete='cascade',
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        index=True,
        default=lambda self: self.env.company,
    )
    platform_type = fields.Selection(
        related='instance_id.platform_type',
        store=True,
        readonly=True,
    )
    odoo_model = fields.Char(
        string='Odoo Model',
        required=True,
        index=True,
        help='Technical Odoo model name, e.g. product.product or sale.order.',
    )
    odoo_record_id = fields.Integer(
        string='Odoo Record ID',
        required=True,
        index=True,
    )
    external_id = fields.Char(
        string='External ID',
        required=True,
        index=True,
        help='Stable identifier of the record on the external platform.',
    )
    external_data = fields.Json(
        help='Optional snapshot of external metadata useful for re-sync.',
    )
    sync_date = fields.Datetime(
        default=fields.Datetime.now,
        required=True,
    )
    sync_state = fields.Selection(
        selection=[
            ('synced', 'Synced'),
            ('not_synced', 'Not Synced'),
            ('error', 'Error'),
            ('deleted', 'Deleted Remote'),
            ('deleted_local', 'Deleted Local'),
        ],
        default='not_synced',
        required=True,
        index=True,
        help='Per-instance sync status for this Odoo ↔ external mapping. '
             'Mapped field edits set Not Synced; successful push/pull sets Synced. '
             'Remote deletion sets Deleted Remote and keeps the Odoo record. '
             'Deleting the Odoo record sets Deleted Local and does not delete the remote record. '
             'Neither state is a license to recreate the missing side automatically.',
    )
    sync_error = fields.Text(
        string='Last Sync Error',
        copy=False,
        help='Last failure message for this binding. Never store secrets here.',
    )
    sync_version = fields.Integer(
        default=0,
        copy=False,
        help='Monotonic counter used to ignore stale sync jobs that finish after a newer edit.',
    )
    active = fields.Boolean(default=True)

    _external_uniq = models.Constraint(
        'UNIQUE(instance_id, odoo_model, external_id)',
        'This external record is already bound for the connector instance.',
    )
    _odoo_uniq = models.Constraint(
        'UNIQUE(instance_id, odoo_model, odoo_record_id)',
        'This Odoo record is already bound for the connector instance.',
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
        return super().create(vals_list)

    @api.constrains('odoo_record_id', 'odoo_model')
    def _check_odoo_record(self):
        for binding in self:
            if binding.odoo_record_id <= 0:
                raise ValidationError(self.env._('Odoo record ID must be a positive integer.'))
            if binding.odoo_model and binding.odoo_model not in self.env:
                raise ValidationError(self.env._(
                    'Unknown Odoo model "%(model)s".',
                    model=binding.odoo_model,
                ))

    @api.depends('odoo_model', 'odoo_record_id', 'external_id')
    def _compute_display_name(self):
        for binding in self:
            binding.display_name = (
                f'{binding.odoo_model}:{binding.odoo_record_id} ↔ {binding.external_id}'
            )

    # -------------------------------------------------------------------------
    # Mapping helpers for platform modules
    # -------------------------------------------------------------------------

    @api.model
    def _kits_find_binding(self, instance, odoo_model, *, external_id=False, odoo_record_id=False):
        """Find an existing binding by external ID and/or Odoo record ID."""
        if not instance:
            raise UserError(self.env._('A connector instance is required to resolve bindings.'))
        domain = [
            ('instance_id', '=', instance.id),
            ('odoo_model', '=', odoo_model),
        ]
        if external_id:
            domain.append(('external_id', '=', str(external_id)))
        elif odoo_record_id:
            domain.append(('odoo_record_id', '=', int(odoo_record_id)))
        else:
            raise UserError(self.env._('Provide external_id or odoo_record_id to find a binding.'))
        return self.search(domain, limit=1)

    @api.model
    def _kits_bind_record(self, instance, odoo_model, odoo_record_id, external_id,
                          *, external_data=None, sync_date=False):
        """Create or update a binding. Prevents duplicate external/Odoo mappings."""
        if not instance or not odoo_model or not odoo_record_id or not external_id:
            raise UserError(self.env._(
                'instance, odoo_model, odoo_record_id, and external_id are required to bind.'
            ))
        external_id = str(external_id)
        odoo_record_id = int(odoo_record_id)

        existing_ext = self._kits_find_binding(instance, odoo_model, external_id=external_id)
        existing_odoo = self._kits_find_binding(
            instance, odoo_model, odoo_record_id=odoo_record_id,
        )

        if existing_ext and existing_odoo and existing_ext != existing_odoo:
            raise UserError(self.env._(
                'Binding conflict: external ID %(external)s and Odoo record %(model)s,%(record)s '
                'point to different bindings.',
                external=external_id,
                model=odoo_model,
                record=odoo_record_id,
            ))

        binding = existing_ext or existing_odoo
        vals = {
            'instance_id': instance.id,
            'company_id': instance.company_id.id,
            'odoo_model': odoo_model,
            'odoo_record_id': odoo_record_id,
            'external_id': external_id,
            'sync_date': sync_date or fields.Datetime.now(),
            'sync_state': 'synced',
            'sync_error': False,
            'active': True,
        }
        if external_data is not None:
            vals['external_data'] = external_data

        if binding:
            # Guard against remapping an existing external ID onto another Odoo record
            if binding.odoo_record_id != odoo_record_id or binding.external_id != external_id:
                conflict = self.search([
                    ('instance_id', '=', instance.id),
                    ('odoo_model', '=', odoo_model),
                    ('id', '!=', binding.id),
                    '|',
                    ('external_id', '=', external_id),
                    ('odoo_record_id', '=', odoo_record_id),
                ], limit=1)
                if conflict:
                    raise UserError(self.env._(
                        'Cannot update binding: another binding already uses this mapping.'
                    ))
            vals['sync_version'] = binding.sync_version + 1
            binding.write(vals)
            return binding
        vals['sync_version'] = 1
        return self.create(vals)

    def _kits_get_odoo_record(self):
        """Browse the bound Odoo record (empty recordset if missing/deleted)."""
        self.ensure_one()
        if self.odoo_model not in self.env:
            return self.browse()
        return self.env[self.odoo_model].browse(self.odoo_record_id).exists()

    def _kits_mark_not_synced(self, *, error=False):
        """Mark bindings as needing export/import. Bumps sync_version for stale-job checks."""
        for binding in self:
            vals = {
                'sync_state': 'error' if error else 'not_synced',
                'sync_version': binding.sync_version + 1,
            }
            if error:
                vals['sync_error'] = error
            else:
                vals['sync_error'] = False
            binding.write(vals)
        return True

    def _kits_mark_synced(self, *, sync_date=False):
        self.write({
            'sync_state': 'synced',
            'sync_error': False,
            'sync_date': sync_date or fields.Datetime.now(),
        })
        return True

    def _kits_mark_error(self, message):
        self.write({
            'sync_state': 'error',
            'sync_error': message or False,
        })
        return True

    def _kits_mark_deleted_remote(self, *, message=False):
        """Mark bindings as deleted on the external platform without archiving the Odoo record.

        The binding stays searchable (active) so operators can see the unavailable mapping.
        Repeated calls are idempotent.
        """
        self.write({
            'sync_state': 'deleted',
            'sync_error': message or False,
            'sync_date': fields.Datetime.now(),
        })
        return True

    def _kits_mark_deleted_local(self, *, message=False):
        """Mark bindings when the Odoo record is removed. Does not call any remote API.

        Repeated calls are idempotent. The binding stays active for history.
        """
        self.write({
            'sync_state': 'deleted_local',
            'sync_error': message or False,
            'sync_date': fields.Datetime.now(),
        })
        return True

    @api.model
    def _kits_mark_odoo_records_deleted_local(self, odoo_model, record_ids, message=False):
        """Flag bindings for records that are being deleted in Odoo.

        Search uses sudo so a user who can delete the business record but is not a
        connector administrator still leaves an accurate local-deletion flag. The
        domain is limited to that user's companies — this does not grant access to
        other companies or perform a remote delete.
        """
        ids = [int(i) for i in record_ids if i]
        if not ids:
            return self.browse()
        bindings = self.sudo().search([
            ('odoo_model', '=', odoo_model),
            ('odoo_record_id', 'in', ids),
            ('company_id', 'in', self.env.companies.ids),
        ])
        if bindings:
            bindings._kits_mark_deleted_local(message=message)
        return bindings

    @api.model
    def _kits_mark_records_not_synced(self, odoo_model, record_ids, *, instance=None, company=None):
        """Mark bindings for Odoo records as Not Synced (optionally limited to one instance).

        Deleted-remote and deleted-local mappings are left unchanged so a later edit
        cannot queue them for automatic recreation.
        """
        if not record_ids:
            return self.browse()
        domain = [
            ('odoo_model', '=', odoo_model),
            ('odoo_record_id', 'in', [int(i) for i in record_ids]),
            ('active', '=', True),
            ('sync_state', 'not in', ('deleted', 'deleted_local')),
        ]
        if instance:
            domain.append(('instance_id', '=', instance.id))
        if company:
            domain.append(('company_id', '=', company.id))
        bindings = self.search(domain)
        if bindings:
            bindings._kits_mark_not_synced()
        return bindings

    def _kits_is_stale_job(self, expected_version):
        """Return True when a newer edit happened after the job was enqueued."""
        self.ensure_one()
        if expected_version is None or expected_version is False:
            return False
        return int(expected_version) < int(self.sync_version or 0)

    def action_open_odoo_record(self):
        self.ensure_one()
        record = self._kits_get_odoo_record()
        if not record:
            raise UserError(self.env._('The bound Odoo record no longer exists.'))
        return {
            'type': 'ir.actions.act_window',
            'res_model': self.odoo_model,
            'res_id': record.id,
            'view_mode': 'form',
            'target': 'current',
        }
