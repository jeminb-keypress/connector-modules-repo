# -*- coding: utf-8 -*-
"""Reusable dashboard period and card helpers."""

from datetime import timedelta

from odoo import fields, models


class KitsConnectorDashboardUtils(models.AbstractModel):
    _name = 'kits.connector.dashboard.utils'
    _description = 'Kits Connector Dashboard Utilities'

    @staticmethod
    def _kits_dashboard_period_dates(period='last_30'):
        """Return ``(date_from, date_to)`` datetimes for a named period key."""
        now = fields.Datetime.now()
        mapping = {
            'today': 0,
            'last_7': 7,
            'last_30': 30,
            'last_90': 90,
        }
        days = mapping.get(period, 30)
        if days == 0:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return start, now
        return now - timedelta(days=days), now

    def _kits_dashboard_card(self, key, label, value, *, tone='default', hint=False):
        """Build one dashboard card dict."""
        return {
            'key': key,
            'label': label,
            'value': value,
            'tone': tone,
            'hint': hint or False,
        }

    def _kits_dashboard_count(self, model_name, domain):
        """Safe search_count for dashboard stats."""
        if model_name not in self.env:
            return 0
        return self.env[model_name].search_count(domain)
