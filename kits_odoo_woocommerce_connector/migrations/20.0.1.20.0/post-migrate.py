# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Product import waiting-list release — schema comes from ORM upgrade."""
    _logger.info(
        'kits_odoo_woocommerce_connector 20.0.1.20.0: product waiting list, '
        'image jobs, daily import, weight/price settings applied.'
    )
