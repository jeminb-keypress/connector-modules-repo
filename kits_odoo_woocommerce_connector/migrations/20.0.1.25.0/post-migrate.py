# -*- coding: utf-8 -*-
"""Drop obsolete customer form/Action sync helpers replaced by Sync Operation."""


def migrate(cr, version):
    # Remove server actions opened from Contacts Action menu.
    cr.execute(
        """
        DELETE FROM ir_act_server
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'kits_odoo_woocommerce_connector'
              AND name IN (
                  'action_woo_import_customers_selected',
                  'action_woo_fetch_customer_selected'
              )
              AND model = 'ir.actions.server'
        )
        """
    )
    # Remove fetch wizard view if still present.
    cr.execute(
        """
        DELETE FROM ir_ui_view
        WHERE model = 'kits.woo.customer.fetch'
           OR id IN (
                SELECT res_id FROM ir_model_data
                WHERE module = 'kits_odoo_woocommerce_connector'
                  AND name = 'kits_woo_customer_fetch_view_form'
                  AND model = 'ir.ui.view'
           )
        """
    )
    # Odoo 20 stores ACL rows in ir_access (not ir_model_access).
    cr.execute(
        """
        DELETE FROM ir_access
        WHERE name = 'kits.woo.customer.fetch manager'
           OR model_id IN (
                SELECT id FROM ir_model WHERE model = 'kits.woo.customer.fetch'
           )
        """
    )
    cr.execute(
        """
        DELETE FROM ir_model_data
        WHERE module = 'kits_odoo_woocommerce_connector'
          AND name IN (
              'action_woo_import_customers_selected',
              'action_woo_fetch_customer_selected',
              'kits_woo_customer_fetch_view_form',
              'access_kits_woo_customer_fetch_manager',
              'model_kits_woo_customer_fetch'
          )
        """
    )
    cr.execute(
        """
        DELETE FROM ir_model
        WHERE model = 'kits.woo.customer.fetch'
        """
    )
