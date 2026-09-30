"""Analytics: the ingest contract of ``POST /v1/events`` and the dashboard payload of
``GET /v1/admin/analytics``. The 13 event names are ``core.models.analytics.AnalyticsEvent``
(BRD §6.2's eleven + the pilot scope's two intent buttons).

Ingest rules (the prototype is a validation instrument, not a tracking product):
- exactly the 13 names; a row with any other name is rejected;
- rows are judged one by one: a malformed row is rejected (and reported with its index and the
  problems) without dropping the valid rows of the batch; only a malformed batch (no ``events``
  list, 0 or more than 100 rows, unknown top-level keys) or a batch whose every row is malformed
  is a 422;
- ids are anonymous and client-generated (``session_id`` required, ``client_id`` optional for
  repeat usage, ``event_id`` optional for de-duplicating retried batches);
- ``properties`` is small and flat (≤ 20 scalar entries, strings ≤ 200 chars) and may never carry
  personal data: a denylist of keys (name, email, phone, ip …) and a scan of string values for
  e-mail addresses and IP addresses reject the row;
- known properties are typed (ids are positive integers, ``search_kind`` is address | click |
  parcel_number, ``amount_eur`` ≥ 0, ``lat`` / ``lng`` in degrees …) and a few are required per
  event.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from core.models.analytics import AnalyticsEvent

__all__ = ["AnalyticsEvent"]

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
# What a point search, map click, parcel lookup or zone pick found (`search_performed.coverage`):
# a parcel / plan feature inside coverage, covered land without a parcel, a place no adopted plan
# covers (the S6 "outside current coverage" hit), or a lookup that failed.
SEARCH_COVERAGE = ("covered", "no_parcel", "uncovered", "failed")

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
NUMBER_PROPERTIES: dict[str, tuple[float, float]] = {  # key -> (minimum, maximum)
    "amount_eur": (0.0, math.inf),
    "lat": (-90.0, 90.0),  # where a search landed (the map sends 4 decimals, ≈ 11 m)
    "lng": (-180.0, 180.0),
}
STRING_PROPERTIES = frozenset(
    {"layer_id", "order_id", "product", "trigger", "panel_type", "search_kind", "currency", "via"}
)
BOOL_PROPERTIES = frozenset({"visible", "on", "matched", "recent"})
ENUM_PROPERTIES: dict[str, tuple[str, ...]] = {
    "search_kind": SEARCH_KINDS,
    "result": ("address", "zone", "parcel"),
    "panel_type": PANEL_TYPES,
    "currency": ("EUR",),
    "coverage": SEARCH_COVERAGE,
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
        for key, (minimum, maximum) in NUMBER_PROPERTIES.items():
            if key in props and props[key] is not None:
                item = props[key]
                if (
                    isinstance(item, bool)
                    or not isinstance(item, int | float)
                    or not minimum <= item <= maximum
                ):
                    bounds = f"≥ {minimum}" if maximum == math.inf else f"in [{minimum}, {maximum}]"
                    raise ValueError(f"property {key!r} must be a number {bounds}")
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


class RowProblem(BaseModel):
    loc: list[str | int] = Field(description='Where in the row, e.g. ["properties"]')
    msg: str
    type: str


class RejectedEvent(BaseModel):
    index: int = Field(description="Position of the row in the batch (0-based)")
    problems: list[RowProblem]


class EventBatch(BaseModel):
    """1 to 100 events. Rows are validated one by one (see the module docstring): the valid ones
    land in ``events``, the malformed ones in ``rejected``."""

    model_config = ConfigDict(extra="forbid")

    events: list[EventIn] = Field(max_length=MAX_BATCH, json_schema_extra={"minItems": 1})
    _rejected: list[RejectedEvent] = PrivateAttr(default_factory=list)

    @model_validator(mode="wrap")
    @classmethod
    def _rows_one_by_one(
        cls, data: Any, handler: ModelWrapValidatorHandler[EventBatch]
    ) -> EventBatch:
        rows = data.get("events") if isinstance(data, dict) else None
        if not isinstance(rows, list) or len(rows) > MAX_BATCH:
            return handler(data)  # the envelope's own errors (missing, not a list, too long)
        if not rows:
            raise ValueError(f"a batch holds 1 to {MAX_BATCH} events")
        valid: list[EventIn] = []
        rejected: list[RejectedEvent] = []
        for index, row in enumerate(rows):
            try:
                valid.append(EventIn.model_validate(row))
            except ValidationError as exc:
                rejected.append(
                    RejectedEvent(
                        index=index,
                        problems=[
                            RowProblem(loc=list(e["loc"]), msg=e["msg"], type=e["type"])
                            for e in exc.errors(include_url=False)
                        ],
                    )
                )
        batch = handler({**data, "events": valid})
        batch._rejected = rejected
        return batch

    @property
    def rejected(self) -> list[RejectedEvent]:
        return self._rejected


class IngestResult(BaseModel):
    received: int
    accepted: int = Field(description="Rows stored")
    duplicates: int = Field(description="Valid rows whose event_id was already stored (retries)")
    rejected: list[RejectedEvent] = Field(
        default_factory=list, description="Malformed rows, not stored; the valid rows were"
    )


# --- dashboard -----------------------------------------------------------------------------------


class DateRange(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: datetime = Field(alias="from", description="Inclusive, UTC")
    to: datetime = Field(description="Exclusive, UTC")
    days: float


class FunnelStep(BaseModel):
    step: str = Field(
        description="map_loaded | parcel_resolved | panel_opened | order_started | "
        "order_submitted | paid"
    )
    event_names: list[str] = Field(
        description="The events that put a session at this step (none for paid: the orders table)"
    )
    sessions: int = Field(
        description="Sessions that reached the step and every earlier one in the range"
    )
    conversion_from_previous_pct: float = Field(
        description="sessions / previous step's sessions × 100 (0 when the previous step is 0)"
    )
    conversion_from_start_pct: float


class Funnel(BaseModel):
    basis: Literal["sessions"] = "sessions"
    steps: list[FunnelStep]
    overall_conversion_pct: float = Field(description="paid sessions / map_loaded sessions × 100")


class OrderStatusCount(BaseModel):
    status: str
    orders: int
    amount_eur: float = Field(description="Sum of the orders' prices")


class Orders(BaseModel):
    placed: int = Field(description="Orders placed in the range")
    by_status: list[OrderStatusCount] = Field(
        description="Every order status in flow order, 0 when none; no customer data"
    )


class ZoneHits(BaseModel):
    """A zone by the searches and parcel picks made in it: the event's ``zone_id``, else the zone
    containing its ``lat`` / ``lng``, so searches outside coverage count for their district."""

    zone_id: int | None = Field(
        description="null = events with no zone_id and no point inside any zone"
    )
    zone_name: str | None
    covered: bool | None = Field(
        default=None, description="The zone has an adopted, live plan (null without a zone)"
    )
    events: int = Field(description="search_performed + parcel_selected")
    searches: int
    uncovered_searches: int = Field(
        description="search_performed with coverage 'uncovered' (S6: no adopted plan there)"
    )
    selections: int
    sessions: int
    share_pct: float


class UncoveredHit(BaseModel):
    """Searches and map clicks that landed where no adopted plan covers the point, grouped by
    their position (3 decimals, ≈ 110 m)."""

    lat: float
    lng: float
    searches: int
    sessions: int


class RepeatSessions(BaseModel):
    min_sessions: int = Field(description="Visits that make a repeat visitor (the target: 3)")
    sessions: int = Field(description="Distinct sessions in the range")
    visitors: int = Field(description="Distinct anonymous client ids")
    repeat_visitors: int = Field(description="Visitors with ≥ min_sessions sessions")
    repeat_visitors_pct: float
    repeat_sessions: int = Field(description="Sessions of the repeat visitors")


class IntentCount(BaseModel):
    events: int
    sessions: int


class IntentCounts(BaseModel):
    market_data_interest: IntentCount
    ai_interest: IntentCount


class AnalyticsDashboard(BaseModel):
    municipality_id: str
    range: DateRange
    generated_at: datetime
    funnel: Funnel
    orders: Orders
    top_zones: list[ZoneHits] = Field(description="Most searched / selected zones, descending")
    uncovered_hits: list[UncoveredHit] = Field(
        description="Where searches outside coverage landed, most frequent first"
    )
    repeat_sessions: RepeatSessions
    intent_counts: IntentCounts
