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
        """Legacy job type — customer export is no longer supported."""
        self.ensure_one()
        raise UserError(self.env._(
            'WooCommerce customer export is not supported. '
            'Import or Fetch customers from the store instead.'
        ))

    def _run_woo_webhook_customer(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_process_customer_webhook(job=self)

    def _run_woo_import_categories(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_import_categories(job=self)

    def _run_woo_export_categories(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_export_categories(job=self)

    def _run_woo_import_tags(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_import_tags(job=self)

    def _run_woo_import_attributes(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_import_attributes(job=self)

    def _run_woo_import_coupons(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_import_coupons(job=self)

    def _run_woo_export_coupons(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_export_coupons(job=self)

    def _run_woo_webhook_product(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_process_product_webhook(job=self)

    def _run_woo_webhook_order(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_process_order_webhook(job=self)

    def _run_woo_webhook_coupon(self):
        self.ensure_one()
        self._woo_ensure_instance()
        return self.instance_id._woo_process_coupon_webhook(job=self)

    def _woo_ensure_instance(self):
        self.ensure_one()
        if self.instance_id.platform_type != 'woocommerce':
            raise UserError(self.env._(
                'Job %(job)s is not linked to a WooCommerce instance.',
                job=self.display_name,
            ))
