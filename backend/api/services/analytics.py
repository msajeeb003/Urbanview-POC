"""Analytics: batch ingest (``POST /v1/events``) and the dashboard aggregates
(``GET /v1/admin/analytics``).

The repository does the SQL (one statement per aggregate, all filtered by municipality and the
``[from, to)`` range, grouped in the database, never in the browser); the service turns rows into
the dashboard payload with the percentages, so the assembly is unit-tested on canned rows and the
SQL on PostGIS. An empty range (no events, no orders, or ``from`` = ``to``) answers zeros.
Definitions:

- funnel: map_loaded → parcel_resolved (``parcel_selected``) → panel_opened (``panel_viewed``) →
  order_started → order_submitted (``checkout_completed``) → paid (the order that
  ``checkout_completed.order_id`` names has been paid: ``orders.paid_at``). A session counts at a
  step when it emitted that step's event and every earlier step's inside the range (in any
  order), so the counts never grow along the funnel; conversions are session ratios;
- orders by status: the orders placed in the range grouped by their status (count and sum of the
  prices; no customer data);
- top zones: ``search_performed`` + ``parcel_selected`` grouped by zone: the event's ``zone_id``,
  else the zone containing its ``lat`` / ``lng`` (the smallest one), so a search outside coverage
  (``coverage: uncovered``, which locate answers with ``zone: null``) still counts for the district
  it was made in (BRD §2.10 location demand); each zone says whether it is covered and how many of
  its searches were uncovered;
- uncovered hits: ``search_performed`` with ``coverage: uncovered`` grouped by ``lat`` / ``lng``
  (3 decimals, ≈ 110 m);
- repeat sessions: anonymous visitors (``client_id``) with at least 3 sessions in the range (the
  prototype target), and their sessions;
- intent counts: the two intent buttons' events and sessions.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.analytics import (
    AnalyticsDashboard,
    DateRange,
    EventBatch,
    Funnel,
    FunnelStep,
    IngestResult,
    IntentCount,
    IntentCounts,
    Orders,
    OrderStatusCount,
    RepeatSessions,
    UncoveredHit,
    ZoneHits,
)
from core.coverage import ZONE_COVERED
from core.errors import AppError
from core.models.analytics import AnalyticsEventRecord
from core.models.orders import ORDER_STATUSES

DEFAULT_RANGE_DAYS = 30
MAX_RANGE_DAYS = 366
REPEAT_MIN_SESSIONS = 3
TOP_ZONES_LIMIT = 10
UNCOVERED_HITS_LIMIT = 20

# (step, the events that put a session there); ``paid`` comes from the orders table
FUNNEL_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("map_loaded", ("map_loaded",)),
    ("parcel_resolved", ("parcel_selected",)),
    ("panel_opened", ("panel_viewed",)),
    ("order_started", ("order_started",)),
    ("order_submitted", ("checkout_completed",)),
    ("paid", ()),
)


@dataclass(frozen=True, slots=True)
class Range:
    start: datetime  # inclusive
    end: datetime  # exclusive


@dataclass(slots=True)
class DashboardRows:
    """Raw aggregate rows (plain dicts / lists) as the repository returns them."""

    funnel: Mapping[str, Any]
    orders: list[Mapping[str, Any]] = field(default_factory=list)
    top_zones: list[Mapping[str, Any]] = field(default_factory=list)
    uncovered_hits: list[Mapping[str, Any]] = field(default_factory=list)
    repeat_sessions: Mapping[str, Any] = field(default_factory=dict)
    intent: Mapping[str, Any] = field(default_factory=dict)


class AnalyticsRepository(Protocol):
    async def insert_events(self, rows: list[dict[str, Any]]) -> int: ...

    async def collect(self, rng: Range, *, limit: int, min_sessions: int) -> DashboardRows: ...


# --- SQL repository ------------------------------------------------------------------------------

RANGE = "municipality_id = :m AND occurred_at >= :start_at AND occurred_at < :end_at"
RANGE_E = "e.municipality_id = :m AND e.occurred_at >= :start_at AND e.occurred_at < :end_at"

FUNNEL_SQL = text(f"""
    WITH e AS (
        SELECT name, session_id, properties FROM analytics_events
        WHERE {RANGE}
          AND name IN ('map_loaded', 'parcel_selected', 'panel_viewed', 'order_started',
                       'checkout_completed')
    ),
    paid AS (
        SELECT DISTINCT e.session_id
        FROM e
        JOIN orders o ON o.municipality_id = :m
                     AND o.reference = upper(e.properties ->> 'order_id')
                     AND o.paid_at IS NOT NULL
        WHERE e.name = 'checkout_completed'
    ),
    s AS (
        SELECT session_id,
               bool_or(name = 'map_loaded') AS s1,
               bool_or(name = 'parcel_selected') AS s2,
               bool_or(name = 'panel_viewed') AS s3,
               bool_or(name = 'order_started') AS s4,
               bool_or(name = 'checkout_completed') AS s5
        FROM e GROUP BY session_id
    )
    SELECT
      count(*) FILTER (WHERE s1) AS map_loaded,
      count(*) FILTER (WHERE s1 AND s2) AS parcel_resolved,
      count(*) FILTER (WHERE s1 AND s2 AND s3) AS panel_opened,
      count(*) FILTER (WHERE s1 AND s2 AND s3 AND s4) AS order_started,
      count(*) FILTER (WHERE s1 AND s2 AND s3 AND s4 AND s5) AS order_submitted,
      count(*) FILTER (WHERE s1 AND s2 AND s3 AND s4 AND s5
                       AND session_id IN (SELECT session_id FROM paid)) AS paid
    FROM s
""")

ORDERS_SQL = text("""
    SELECT status, count(*) AS orders, COALESCE(sum(price_eur), 0) AS amount_eur
    FROM orders
    WHERE municipality_id = :m AND placed_at >= :start_at AND placed_at < :end_at
    GROUP BY status
""")

# the zone an event is counted for: its own zone_id, else the smallest zone containing the point
# it carries (a search outside coverage has lat / lng but no zone_id)
_EVENT_ZONE = """COALESCE(e.zone_id, (
        SELECT z.id FROM zones z
        WHERE z.municipality_id = e.municipality_id
          AND jsonb_typeof(e.properties -> 'lat') = 'number'
          AND jsonb_typeof(e.properties -> 'lng') = 'number'
          AND ST_Intersects(z.geom, ST_SetSRID(ST_MakePoint(
                CAST(e.properties ->> 'lng' AS double precision),
                CAST(e.properties ->> 'lat' AS double precision)), 4326))
        ORDER BY ST_Area(z.geom), z.id
        LIMIT 1))"""

TOP_ZONES_SQL = text(f"""
    WITH hits AS (
        SELECT e.name, e.session_id, e.properties ->> 'coverage' AS coverage,
               {_EVENT_ZONE} AS zone_id
        FROM analytics_events e
        WHERE {RANGE_E}
          AND e.name IN ('search_performed', 'parcel_selected')
    )
    SELECT h.zone_id, z.name AS zone_name,
           CASE WHEN z.id IS NULL THEN NULL ELSE {ZONE_COVERED} END AS covered,
           count(*) AS events,
           count(*) FILTER (WHERE h.name = 'search_performed') AS searches,
           count(*) FILTER (WHERE h.name = 'parcel_selected') AS selections,
           count(*) FILTER (WHERE h.name = 'search_performed' AND h.coverage = 'uncovered')
               AS uncovered_searches,
           count(DISTINCT h.session_id) AS sessions
    FROM hits h
    LEFT JOIN zones z ON z.id = h.zone_id
    GROUP BY h.zone_id, z.id, z.name, z.municipality_id
    ORDER BY events DESC, h.zone_id ASC NULLS LAST
    LIMIT :limit
""")

UNCOVERED_HITS_SQL = text(f"""
    SELECT round(CAST(properties ->> 'lat' AS numeric), 3) AS lat,
           round(CAST(properties ->> 'lng' AS numeric), 3) AS lng,
           count(*) AS searches,
           count(DISTINCT session_id) AS sessions
    FROM analytics_events
    WHERE {RANGE}
      AND name = 'search_performed'
      AND properties ->> 'coverage' = 'uncovered'
      AND jsonb_typeof(properties -> 'lat') = 'number'
      AND jsonb_typeof(properties -> 'lng') = 'number'
    GROUP BY 1, 2
    ORDER BY searches DESC, sessions DESC, lat, lng
    LIMIT :limit
""")

REPEAT_SQL = text(f"""
    WITH s AS (
        SELECT session_id, min(client_id) AS client_id
        FROM analytics_events WHERE {RANGE} GROUP BY session_id
    ),
    c AS (
        SELECT client_id, count(*) AS sessions FROM s WHERE client_id IS NOT NULL GROUP BY client_id
    )
    SELECT
      (SELECT count(*) FROM s) AS sessions,
      (SELECT count(*) FROM c) AS visitors,
      (SELECT count(*) FROM c WHERE sessions >= :min_sessions) AS repeat_visitors,
      (SELECT COALESCE(sum(sessions), 0) FROM c WHERE sessions >= :min_sessions) AS repeat_sessions
""")

INTENT_SQL = text(f"""
    SELECT
      count(*) FILTER (WHERE name = 'market_data_interest') AS market_events,
      count(DISTINCT session_id) FILTER (WHERE name = 'market_data_interest') AS market_sessions,
      count(*) FILTER (WHERE name = 'ai_interest') AS ai_events,
      count(DISTINCT session_id) FILTER (WHERE name = 'ai_interest') AS ai_sessions
    FROM analytics_events
    WHERE {RANGE} AND name IN ('market_data_interest', 'ai_interest')
""")


class SqlAnalyticsRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], municipality_id: str):
        self.session_factory = session_factory
        self.municipality_id = municipality_id

    async def insert_events(self, rows: list[dict[str, Any]]) -> int:
        """One multi-row INSERT; retried events (same ``event_id``) are skipped, not errors."""
        statement = (
            pg_insert(AnalyticsEventRecord)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=["municipality_id", "event_id"],
                index_where=text("event_id IS NOT NULL"),
            )
            .returning(AnalyticsEventRecord.id)
        )
        async with self.session_factory() as session:
            inserted = len((await session.execute(statement)).all())
            await session.commit()
        return inserted

    async def collect(self, rng: Range, *, limit: int, min_sessions: int) -> DashboardRows:
        params = {"m": self.municipality_id, "start_at": rng.start, "end_at": rng.end}
        async with self.session_factory() as session:

            async def rows(statement: Any, **extra: Any) -> list[dict[str, Any]]:
                result = await session.execute(statement, {**params, **extra})
                return [dict(row) for row in result.mappings().all()]

            funnel = await rows(FUNNEL_SQL)
            orders = await rows(ORDERS_SQL)
            top_zones = await rows(TOP_ZONES_SQL, limit=limit)
            uncovered = await rows(UNCOVERED_HITS_SQL, limit=UNCOVERED_HITS_LIMIT)
            repeat = await rows(REPEAT_SQL, min_sessions=min_sessions)
            intent = await rows(INTENT_SQL)
        return DashboardRows(
            funnel=funnel[0],
            orders=orders,
            top_zones=top_zones,
            uncovered_hits=uncovered,
            repeat_sessions=repeat[0],
            intent=intent[0],
        )


# --- service -------------------------------------------------------------------------------------


def pct(part: Any, whole: Any) -> float:
    """``part / whole × 100`` with 1 decimal; 0 when there is nothing to divide by."""
    whole = _num(whole)
    return 0.0 if not whole else round(100.0 * _num(part) / whole, 1)


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _int(value: Any) -> int:
    return int(value or 0)


def resolve_range(
    from_: datetime | None,
    to: datetime | None,
    *,
    now: datetime,
    default_days: int = DEFAULT_RANGE_DAYS,
    max_days: int = MAX_RANGE_DAYS,
) -> Range:
    """``[from, to)`` in UTC (naive = UTC); defaults to the last ``default_days`` days. ``from`` =
    ``to`` is an empty range (zeros), ``from`` after ``to`` a 422."""
    end = _utc(to) if to is not None else now
    start = _utc(from_) if from_ is not None else end - timedelta(days=default_days)
    problems = []
    if start > end:
        problems.append({"loc": ["query", "from"], "msg": "'from' must not be after 'to'"})
    elif end - start > timedelta(days=max_days):
        problems.append({"loc": ["query", "to"], "msg": f"range longer than {max_days} days"})
    if problems:
        raise AppError(
            "Request validation failed",
            code="validation_error",
            status_code=422,
            details=problems,
        )
    return Range(start=start, end=end)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class AnalyticsService:
    def __init__(
        self,
        repository: AnalyticsRepository,
        *,
        municipality_id: str,
        top_zones_limit: int = TOP_ZONES_LIMIT,
        repeat_min_sessions: int = REPEAT_MIN_SESSIONS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository = repository
        self.municipality_id = municipality_id
        self.top_zones_limit = int(top_zones_limit)
        self.min_sessions = int(repeat_min_sessions)
        self.clock = clock

    async def ingest(self, batch: EventBatch) -> IngestResult:
        rows = [self._row(event) for event in batch.events]
        inserted = await self.repository.insert_events(rows) if rows else 0
        return IngestResult(
            received=len(rows) + len(batch.rejected),
            accepted=inserted,
            duplicates=len(rows) - inserted,
            rejected=batch.rejected,
        )

    def _row(self, event: Any) -> dict[str, Any]:
        props = dict(event.properties)
        return {
            "municipality_id": self.municipality_id,
            "name": event.name.value,
            "session_id": event.session_id,
            "client_id": event.client_id,
            "event_id": event.event_id,
            "occurred_at": event.occurred_at,
            "properties": props,
            "zone_id": props.get("zone_id"),
            "parcel_id": props.get("parcel_id"),
        }

    async def dashboard(self, from_: datetime | None, to: datetime | None) -> AnalyticsDashboard:
        now = self.clock()
        rng = resolve_range(from_, to, now=now)
        if rng.start == rng.end:
            rows = DashboardRows(funnel={})
        else:
            rows = await self.repository.collect(
                rng, limit=self.top_zones_limit, min_sessions=self.min_sessions
            )
        return self.assemble(rows, rng, generated_at=now)

    def assemble(
        self, rows: DashboardRows, rng: Range, *, generated_at: datetime
    ) -> AnalyticsDashboard:
        return AnalyticsDashboard(
            municipality_id=self.municipality_id,
            range=DateRange(
                from_=rng.start,
                to=rng.end,
                days=round((rng.end - rng.start).total_seconds() / 86400, 2),
            ),
            generated_at=generated_at,
            funnel=_funnel(rows.funnel),
            orders=_orders(rows.orders),
            top_zones=_top_zones(rows.top_zones),
            uncovered_hits=[
                UncoveredHit(
                    lat=_num(r.get("lat")),
                    lng=_num(r.get("lng")),
                    searches=_int(r.get("searches")),
                    sessions=_int(r.get("sessions")),
                )
                for r in rows.uncovered_hits
            ],
            repeat_sessions=_repeat_sessions(rows.repeat_sessions, self.min_sessions),
            intent_counts=_intent(rows.intent),
        )


def _funnel(row: Mapping[str, Any]) -> Funnel:
    steps: list[FunnelStep] = []
    start = _int(row.get(FUNNEL_STEPS[0][0]))
    previous: int | None = None
    for step, names in FUNNEL_STEPS:
        sessions = _int(row.get(step))
        steps.append(
            FunnelStep(
                step=step,
                event_names=list(names),
                sessions=sessions,
                conversion_from_previous_pct=(
                    100.0 if previous is None and sessions else pct(sessions, previous)
                ),
                conversion_from_start_pct=pct(sessions, start),
            )
        )
        previous = sessions
    return Funnel(steps=steps, overall_conversion_pct=pct(steps[-1].sessions, start))


def _orders(rows: list[Mapping[str, Any]]) -> Orders:
    by_status = {str(r.get("status")): r for r in rows}
    counts = [
        OrderStatusCount(
            status=status,
            orders=_int(by_status.get(status, {}).get("orders")),
            amount_eur=round(_num(by_status.get(status, {}).get("amount_eur")), 2),
        )
        for status in ORDER_STATUSES
    ]
    return Orders(placed=sum(c.orders for c in counts), by_status=counts)


def _top_zones(rows: list[Mapping[str, Any]]) -> list[ZoneHits]:
    total = sum(_int(r.get("events")) for r in rows)
    return [
        ZoneHits(
            zone_id=r.get("zone_id"),
            zone_name=r.get("zone_name"),
            covered=r.get("covered"),
            events=_int(r.get("events")),
            searches=_int(r.get("searches")),
            uncovered_searches=_int(r.get("uncovered_searches")),
            selections=_int(r.get("selections")),
            sessions=_int(r.get("sessions")),
            share_pct=pct(r.get("events"), total),
        )
        for r in rows
    ]


def _repeat_sessions(row: Mapping[str, Any], min_sessions: int) -> RepeatSessions:
    return RepeatSessions(
        min_sessions=min_sessions,
        sessions=_int(row.get("sessions")),
        visitors=_int(row.get("visitors")),
        repeat_visitors=_int(row.get("repeat_visitors")),
        repeat_visitors_pct=pct(row.get("repeat_visitors"), row.get("visitors")),
        repeat_sessions=_int(row.get("repeat_sessions")),
    )


def _intent(row: Mapping[str, Any]) -> IntentCounts:
    return IntentCounts(
        market_data_interest=IntentCount(
            events=_int(row.get("market_events")), sessions=_int(row.get("market_sessions"))
        ),
        ai_interest=IntentCount(
            events=_int(row.get("ai_events")), sessions=_int(row.get("ai_sessions"))
        ),
    )
