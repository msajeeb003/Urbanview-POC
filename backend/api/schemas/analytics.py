"""Analytics: event names (BRD §6.2 plus the two interest events added in the build plan), the
ingest contract of ``POST /v1/events`` and the dashboard payload of ``GET /v1/admin/analytics``.

Ingest rules (the prototype is a validation instrument, not a tracking product):
- exactly the 13 names; anything else is rejected;
- ids are anonymous and client-generated (``session_id`` required, ``client_id`` optional for
  repeat usage, ``event_id`` optional for de-duplicating retried batches);
- ``properties`` is small and flat (≤ 20 scalar entries, strings ≤ 200 chars) and may never carry
  personal data: a denylist of keys (name, email, phone, ip …) and a scan of string values for
  e-mail addresses and IP addresses reject the whole batch;
- known properties are typed (ids are positive integers, ``search_kind`` is address | click |
  parcel_number, ``amount_eur`` ≥ 0 …) and a few are required per event.
"""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


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


# --- ingest --------------------------------------------------------------------------------------

ID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"
PROPERTY_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_PROPERTIES = 20
MAX_STRING_LENGTH = 200
MAX_PROPERTIES_BYTES = 2048
MAX_BATCH = 100
FUTURE_TOLERANCE = timedelta(minutes=5)

# Keys that would carry personal data whatever the value (the order purchaser's data lives with
# the order, never in events).
FORBIDDEN_PROPERTY_KEYS = frozenset(
    {
        "email",
        "e_mail",
        "mail",
        "name",
        "first_name",
        "last_name",
        "full_name",
        "surname",
        "username",
        "phone",
        "telephone",
        "mobile",
        "ip",
        "ip_address",
        "client_ip",
        "remote_addr",
        "user_agent",
        "password",
        "token",
        "access_token",
        "address",
        "street_address",
    }
)
EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}")
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
IPV6_CANDIDATE_RE = re.compile(r"[0-9A-Fa-f:]{4,45}")

SEARCH_KINDS = ("address", "click", "parcel_number")
PANEL_TYPES = ("zone", "document", "cadastral", "urban")

# Typed properties (validated when present). Ids are positive integers.
INT_PROPERTIES: dict[str, int] = {  # key -> minimum
    "parcel_id": 1,
    "urban_parcel_id": 1,
    "zone_id": 1,
    "block_id": 1,
    "document_id": 1,
    "page": 1,
    "value_id": 1,
    "sessions": 1,
    "days_since_last": 0,
}
NUMBER_PROPERTIES: dict[str, float] = {"amount_eur": 0.0}
STRING_PROPERTIES = frozenset(
    {"layer_id", "order_id", "product", "trigger", "panel_type", "search_kind", "currency", "via"}
)
BOOL_PROPERTIES = frozenset({"visible", "on", "matched", "recent"})
ENUM_PROPERTIES: dict[str, tuple[str, ...]] = {
    "search_kind": SEARCH_KINDS,
    "result": ("address", "zone", "parcel"),
    "panel_type": PANEL_TYPES,
    "currency": ("EUR",),
}
REQUIRED_PROPERTIES: dict[AnalyticsEvent, tuple[str, ...]] = {
    AnalyticsEvent.search_performed: ("search_kind",),
    AnalyticsEvent.layer_toggled: ("layer_id",),
    AnalyticsEvent.source_reference_opened: ("document_id", "page"),
    AnalyticsEvent.checkout_completed: ("amount_eur",),
}

Scalar = str | int | float | bool | None


def _looks_personal(value: str) -> bool:
    if EMAIL_RE.search(value):
        return True
    candidates = IPV4_RE.findall(value) + [
        m for m in IPV6_CANDIDATE_RE.findall(value) if m.count(":") >= 2
    ]
    for candidate in candidates:
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        return True
    return False


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: AnalyticsEvent
    session_id: str = Field(pattern=ID_PATTERN, description="Anonymous, client-generated")
    client_id: str | None = Field(
        default=None,
        pattern=ID_PATTERN,
        description="Anonymous persistent client id (random; never derived from personal data)",
    )
    event_id: str | None = Field(
        default=None, pattern=ID_PATTERN, description="Client id for de-duplicating retries"
    )
    occurred_at: AwareDatetime = Field(description="Client clock with timezone")
    properties: dict[str, Scalar] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _not_in_the_future(cls, value: datetime) -> datetime:
        if value > datetime.now(UTC) + FUTURE_TOLERANCE:
            raise ValueError("occurred_at is in the future")
        return value.astimezone(UTC)

    @field_validator("properties")
    @classmethod
    def _small_flat_and_impersonal(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > MAX_PROPERTIES:
            raise ValueError(f"at most {MAX_PROPERTIES} properties")
        for key, item in value.items():
            if not PROPERTY_KEY_PATTERN.match(key):
                raise ValueError(f"property key {key!r} must be snake_case, ≤ 40 chars")
            if key in FORBIDDEN_PROPERTY_KEYS:
                raise ValueError(f"property {key!r} would carry personal data; not accepted")
            if isinstance(item, str):
                if len(item) > MAX_STRING_LENGTH:
                    raise ValueError(f"property {key!r} longer than {MAX_STRING_LENGTH} chars")
                if _looks_personal(item):
                    raise ValueError(f"property {key!r} looks like personal data; not accepted")
        if len(json.dumps(value, separators=(",", ":"))) > MAX_PROPERTIES_BYTES:
            raise ValueError(f"properties larger than {MAX_PROPERTIES_BYTES} bytes")
        return value

    @model_validator(mode="after")
    def _known_properties_are_typed(self) -> EventIn:
        props = self.properties
        for key in REQUIRED_PROPERTIES.get(self.name, ()):
            if key not in props or props[key] is None:
                raise ValueError(f"{self.name.value} requires property {key!r}")
        for key, minimum in INT_PROPERTIES.items():
            if key in props and props[key] is not None:
                item = props[key]
                if isinstance(item, bool) or not isinstance(item, int) or item < minimum:
                    raise ValueError(f"property {key!r} must be an integer ≥ {minimum}")
        for key, minimum in NUMBER_PROPERTIES.items():
            if key in props and props[key] is not None:
                item = props[key]
                if isinstance(item, bool) or not isinstance(item, int | float) or item < minimum:
                    raise ValueError(f"property {key!r} must be a number ≥ {minimum}")
        for key in STRING_PROPERTIES:
            if key in props and props[key] is not None and not isinstance(props[key], str):
                raise ValueError(f"property {key!r} must be a string")
        for key in BOOL_PROPERTIES:
            if key in props and props[key] is not None and not isinstance(props[key], bool):
                raise ValueError(f"property {key!r} must be a boolean")
        for key, allowed in ENUM_PROPERTIES.items():
            if key in props and props[key] is not None and props[key] not in allowed:
                raise ValueError(f"property {key!r} must be one of {', '.join(allowed)}")
        return self


class EventBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[EventIn] = Field(min_length=1, max_length=MAX_BATCH)


class IngestResult(BaseModel):
    received: int
    accepted: int = Field(description="Rows stored")
    duplicates: int = Field(description="Events whose event_id was already stored (retries)")


# --- dashboard -----------------------------------------------------------------------------------


class DateRange(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: datetime = Field(alias="from", description="Inclusive, UTC")
    to: datetime = Field(description="Exclusive, UTC")
    days: float


class Totals(BaseModel):
    events: int
    sessions: int = Field(description="Distinct anonymous session ids")
    clients: int = Field(description="Distinct anonymous client ids (events that carried one)")
    by_name: dict[str, int] = Field(description="Event counts, every name present (0 when none)")


class FunnelStep(BaseModel):
    step: str
    event_names: list[str]
    sessions: int = Field(description="Sessions with at least one of the step's events")
    conversion_from_previous_pct: float | None = Field(
        description="sessions / previous step's sessions × 100; null when the previous step is 0"
    )
    conversion_from_start_pct: float | None


class Funnel(BaseModel):
    basis: Literal["sessions"] = "sessions"
    steps: list[FunnelStep]
    overall_conversion_pct: float | None = Field(
        description="checkout_completed sessions / map_loaded sessions × 100"
    )


class ProductRevenue(BaseModel):
    product: str
    orders: int
    revenue_eur: float


class Orders(BaseModel):
    source: Literal["analytics_events"] = Field(
        default="analytics_events",
        description="Derived from order_started / checkout_completed events (no orders table yet)",
    )
    order_started_events: int
    orders_started: int = Field(description="Distinct order_id (events without one count singly)")
    orders_completed: int
    revenue_eur: float = Field(description="Sum of checkout_completed.amount_eur per order")
    average_order_eur: float | None
    completion_pct: float | None = Field(description="orders_completed / orders_started × 100")
    by_product: list[ProductRevenue]


class District(BaseModel):
    zone_id: int | None = Field(description="null = events that carried no zone_id")
    zone_name: str | None
    events: int = Field(description="search_performed + parcel_selected")
    searches: int
    selections: int
    sessions: int
    share_pct: float


class ReportedSessions(BaseModel):
    """What the client itself reports through ``sessions_per_user`` events."""

    users: int
    users_at_target: int
    users_at_target_pct: float | None
    sessions_per_user: float | None


class RepeatUsage(BaseModel):
    target_sessions_per_user: int
    sessions: int
    returning_sessions: int = Field(description="Sessions that emitted return_visit")
    repeat_usage_rate_pct: float | None = Field(description="returning_sessions / sessions × 100")
    clients: int = Field(description="Distinct client_id")
    sessions_per_client: float | None
    clients_at_target: int = Field(description="Clients with ≥ target sessions in the range")
    clients_at_target_pct: float | None
    reported: ReportedSessions


class InterestCount(BaseModel):
    events: int
    sessions: int


class Interest(BaseModel):
    market_data_interest: InterestCount
    ai_interest: InterestCount


class PanelToFinancials(BaseModel):
    panel_views: int = Field(description="panel_viewed events")
    panel_view_pairs: int = Field(description="Distinct (session, parcel) pairs with a panel view")
    pairs_reaching_financials: int = Field(
        description="Pairs that also viewed financials for the same parcel"
    )
    reaching_financials_pct: float | None = Field(
        description="pairs_reaching_financials / panel_view_pairs × 100"
    )
    panel_sessions: int
    panel_sessions_reaching_financials: int
    sessions_reaching_financials_pct: float | None


class AnalyticsDashboard(BaseModel):
    municipality_id: str
    range: DateRange
    generated_at: datetime
    totals: Totals
    funnel: Funnel
    orders: Orders
    districts: list[District] = Field(description="Most searched / selected zones, descending")
    repeat_usage: RepeatUsage
    interest: Interest
    panel_to_financials: PanelToFinancials
