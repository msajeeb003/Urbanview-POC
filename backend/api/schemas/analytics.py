"""Analytics event names (BRD §6.2 plus the two interest events added in the build plan)."""

from __future__ import annotations

from enum import StrEnum


class AnalyticsEvent(StrEnum):
    map_loaded = "map_loaded"
    search_performed = "search_performed"
    parcel_selected = "parcel_selected"
    layer_toggled = "layer_toggled"
    panel_viewed = "panel_viewed"
    financials_viewed = "financials_viewed"
    source_reference_opened = "source_reference_opened"
    order_started = "order_started"
    checkout_completed = "checkout_completed"
    return_visit = "return_visit"
    sessions_per_user = "sessions_per_user"
    market_data_interest = "market_data_interest"
    ai_interest = "ai_interest"
