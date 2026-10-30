# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Link product templates to Woo stores from existing bindings."""
    cr.execute(
        """
        SELECT DISTINCT b.odoo_record_id, b.instance_id
          FROM kits_connector_binding b
          JOIN kits_connector_instance i ON i.id = b.instance_id
         WHERE b.odoo_model = 'product.template'
           AND COALESCE(i.platform_type, '') = 'woocommerce'
           AND COALESCE(b.active, true) = true
        """
    )
    rows = cr.fetchall()
    if not rows:
        _logger.info('kits_odoo_woocommerce_connector 20.0.1.9.0: no template bindings to migrate')
        return

    cr.execute(
        """
        CREATE TABLE IF NOT EXISTS product_template_woo_instance_rel (
            product_tmpl_id integer NOT NULL REFERENCES product_template(id) ON DELETE CASCADE,
            instance_id integer NOT NULL REFERENCES kits_connector_instance(id) ON DELETE CASCADE,
            PRIMARY KEY (product_tmpl_id, instance_id)
        )
        """
    )
    inserted = 0
    for tmpl_id, instance_id in rows:
        cr.execute(
            """
            INSERT INTO product_template_woo_instance_rel (product_tmpl_id, instance_id)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            (tmpl_id, instance_id),
        )
        inserted += cr.rowcount
    _logger.info(
        'kits_odoo_woocommerce_connector 20.0.1.9.0: linked %s product/store rows from bindings',
        inserted,
    )
