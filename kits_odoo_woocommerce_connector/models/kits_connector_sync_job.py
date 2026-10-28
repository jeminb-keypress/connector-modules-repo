# -*- coding: utf-8 -*-
from odoo import models
from odoo.exceptions import UserError


class KitsConnectorSyncJob(models.Model):
    _inherit = 'kits.connector.sync.job'

    def _run_woo_import_products(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_import_products(job=self)

    def _run_woo_export_products(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_export_products(job=self)

    def _run_woo_import_orders(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_import_orders(job=self)

    def _run_woo_export_inventory(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_export_inventory(job=self)

    def _run_woo_import_customers(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_import_customers(job=self)

    def _run_woo_export_customers(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_export_customers(job=self)

    def _woo_ensure_instance(self):
        self.ensure_one()
        if self.instance_id.platform_type != 'woocommerce':
            raise UserError(self.env._(
                'Job %(job)s is not linked to a WooCommerce instance.',
                job=self.display_name,
            ))
