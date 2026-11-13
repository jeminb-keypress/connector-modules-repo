# -*- coding: utf-8 -*-
"""Migrate legacy shared webhook JSON/secret into kits.woo.webhook rows."""

import json
import logging

_logger = logging.getLogger(__name__)

_TOPIC_RESOURCE = {
    'product.created': 'products',
    'product.updated': 'products',
    'product.deleted': 'products',
    'customer.created': 'customers',
    'customer.updated': 'customers',
    'customer.deleted': 'customers',
    'order.created': 'orders',
    'order.updated': 'orders',
    'order.deleted': 'orders',
    'coupon.created': 'coupons',
    'coupon.updated': 'coupons',
    'coupon.deleted': 'coupons',
}


def _table_exists(cr, name):
    cr.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = %s AND table_schema = current_schema()",
        (name,),
    )
    return bool(cr.fetchone())


def _columns(cr, table):
    cr.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND table_schema = current_schema()",
        (table,),
    )
    return {row[0] for row in cr.fetchall()}


def _parse_webhook_ids(raw):
    if raw is None or raw is False:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode('utf-8', errors='ignore')
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            _logger.warning('Malformed woo_webhook_ids JSON during migration')
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _sync_remote_status(env, instance_id, remote_id):
    """Best-effort GET of remote webhook status; never creates remotes."""
    try:
        instance = env['kits.woo.instance'].browse(instance_id).exists()
        if not instance or not instance.active:
            return None
        remote = instance._woo_api().get_webhook(int(remote_id))
        status = (remote or {}).get('status')
        if status in ('active', 'paused', 'disabled'):
            return status
    except Exception as exc:  # noqa: BLE001
        _logger.info(
            'Could not fetch remote webhook %s for instance %s during migration: %s',
            remote_id, instance_id, exc,
        )
    return None


def migrate(cr, version):
    if not version:
        return
    if not _table_exists(cr, 'kits_woo_instance'):
        return
    if not _table_exists(cr, 'kits_woo_webhook'):
        _logger.warning('kits_woo_webhook table missing; skip webhook migration')
        return

    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})

    inst_cols = _columns(cr, 'kits_woo_instance')
    if 'woo_webhook_ids' not in inst_cols:
        return

    # Ensure new switch columns exist (ORM may have created them already)
    for col in (
        'woo_webhook_product_enabled',
        'woo_webhook_customer_enabled',
        'woo_webhook_order_enabled',
        'woo_webhook_coupon_enabled',
    ):
        if col not in inst_cols:
            cr.execute(
                f'ALTER TABLE kits_woo_instance ADD COLUMN IF NOT EXISTS {col} boolean DEFAULT false'
            )

    select_cols = ['id', 'name', 'woo_webhook_ids', 'base_url']
    for optional in (
        'woo_webhook_secret',
        'woo_webhook_public_base_url',
        'woo_webhook_enabled',
        'woo_resource_webhooks_enabled',
        'woo_webhook_product_enabled',
        'woo_webhook_customer_enabled',
        'woo_webhook_order_enabled',
        'woo_webhook_coupon_enabled',
        'company_id',
    ):
        if optional in inst_cols or optional.startswith('woo_webhook_'):
            # re-read columns after possible ALTER
            pass
    inst_cols = _columns(cr, 'kits_woo_instance')
    for optional in (
        'woo_webhook_secret',
        'woo_webhook_public_base_url',
        'woo_webhook_enabled',
        'woo_resource_webhooks_enabled',
        'company_id',
    ):
        if optional in inst_cols:
            select_cols.append(optional)

    cr.execute(
        f"SELECT {', '.join(select_cols)} FROM kits_woo_instance"
    )
    rows = cr.dictfetchall()
    created = 0

    for row in rows:
        instance_id = row['id']
        legacy_map = _parse_webhook_ids(row.get('woo_webhook_ids'))
        secret = row.get('woo_webhook_secret') or ''
        public_base = (row.get('woo_webhook_public_base_url') or '').rstrip('/')
        company_id = row.get('company_id')

        topics_present = set()
        for topic, remote_id in legacy_map.items():
            if topic not in _TOPIC_RESOURCE:
                _logger.warning(
                    'Skipping unknown legacy webhook topic %s on instance %s',
                    topic, instance_id,
                )
                continue
            if remote_id in (None, False, ''):
                continue
            try:
                remote_id_str = str(int(remote_id))
            except (TypeError, ValueError):
                _logger.warning(
                    'Invalid legacy webhook id %r for %s on instance %s',
                    remote_id, topic, instance_id,
                )
                continue

            cr.execute(
                "SELECT id FROM kits_woo_webhook "
                "WHERE instance_id = %s AND topic = %s LIMIT 1",
                (instance_id, topic),
            )
            if cr.fetchone():
                topics_present.add(topic)
                continue

            resource = _TOPIC_RESOURCE[topic]
            delivery_url = ''
            if public_base:
                delivery_url = f'{public_base}/kits_woo/webhook/{instance_id}/{resource}'
            name = f"Kits Odoo — {topic} ({row.get('name') or instance_id})"

            status = _sync_remote_status(env, instance_id, remote_id_str) or 'active'

            cr.execute(
                """
                INSERT INTO kits_woo_webhook (
                    name, instance_id, company_id, topic, webhook_id, status,
                    delivery_url, secret, active, create_uid, write_uid,
                    create_date, write_date
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, TRUE, 1, 1,
                    NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC'
                )
                """,
                (
                    name,
                    instance_id,
                    company_id,
                    topic,
                    remote_id_str,
                    status,
                    delivery_url or '',
                    secret or '',
                ),
            )
            created += 1
            topics_present.add(topic)

        # Initialize switches from legacy flags / migrated topics
        customer_on = bool(row.get('woo_webhook_enabled')) or any(
            t.startswith('customer.') for t in topics_present
        )
        resource_on = bool(row.get('woo_resource_webhooks_enabled'))
        product_on = resource_on or any(t.startswith('product.') for t in topics_present)
        order_on = resource_on or any(t.startswith('order.') for t in topics_present)
        coupon_on = resource_on or any(t.startswith('coupon.') for t in topics_present)

        cr.execute(
            """
            UPDATE kits_woo_instance SET
                woo_webhook_customer_enabled = COALESCE(%s, FALSE),
                woo_webhook_product_enabled = COALESCE(%s, FALSE),
                woo_webhook_order_enabled = COALESCE(%s, FALSE),
                woo_webhook_coupon_enabled = COALESCE(%s, FALSE)
            WHERE id = %s
            """,
            (customer_on, product_on, order_on, coupon_on, instance_id),
        )

    _logger.info(
        'Migrated %s kits.woo.webhook row(s) from legacy woo_webhook_ids',
        created,
    )
