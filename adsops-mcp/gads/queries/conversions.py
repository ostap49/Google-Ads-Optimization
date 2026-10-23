"""GAQL builders: conversion tracking."""
from __future__ import annotations

from core.dates import DateRange

CONVERSION_ACTIONS = """
SELECT conversion_action.id, conversion_action.name, conversion_action.status, conversion_action.type,
       conversion_action.category, conversion_action.origin, conversion_action.primary_for_goal,
       conversion_action.include_in_conversions_metric, conversion_action.counting_type,
       conversion_action.attribution_model_settings.attribution_model,
       conversion_action.attribution_model_settings.data_driven_model_status,
       conversion_action.value_settings.default_value, conversion_action.value_settings.always_use_default_value,
       conversion_action.click_through_lookback_window_days, conversion_action.view_through_lookback_window_days,
       conversion_action.owner_customer
FROM conversion_action
WHERE conversion_action.status != 'REMOVED'
"""

TRACKING_SETTINGS = """
SELECT customer.id, customer.auto_tagging_enabled,
       customer.conversion_tracking_setting.conversion_tracking_status,
       customer.conversion_tracking_setting.accepted_customer_data_terms,
       customer.conversion_tracking_setting.enablement_setting,
       customer.conversion_tracking_setting.effective_enablement_setting,
       customer.conversion_tracking_setting.google_ads_conversion_customer
FROM customer
"""

CONVERSION_GOALS = """
SELECT customer_conversion_goal.category, customer_conversion_goal.origin, customer_conversion_goal.biddable
FROM customer_conversion_goal
"""


def daily_by_action(dr: DateRange) -> str:
    return ("SELECT segments.conversion_action, segments.date, metrics.all_conversions, metrics.all_conversions_value, "
            f"metrics.conversions, metrics.conversions_value FROM customer WHERE {dr.gaql()}")
