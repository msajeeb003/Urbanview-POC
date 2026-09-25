"""Analytics: batch ingest (``POST /v1/events``) and the client dashboard aggregates
(``GET /v1/admin/analytics``).

The repository does the SQL (one statement per aggregate, all filtered by municipality and the
``[from, to)`` range); the service turns rows into the dashboard payload with the percentages, so
the assembly is unit-tested on canned rows and the SQL on PostGIS. Definitions:

- funnel: a session counts at a step when it emitted one of the step's events inside the range
  (presence, not strict ordering); conversions are session ratios;
- orders and revenue come from ``order_started`` / ``checkout_completed`` events (distinct
  ``order_id``; ``amount_eur`` summed once per order) until an orders table exists;
- districts: ``search_performed`` + ``parcel_selected`` grouped by the ``zone_id`` property;
- repeat usage: ``return_visit`` sessions over all sessions, plus sessions per anonymous
  ``client_id`` against the prototype target (3+), and what ``sessions_per_user`` events report;
- panel views reaching financials: distinct (session, parcel) pairs with ``panel_viewed`` that
  also have ``financials_viewed`` for the same parcel.
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
    AnalyticsEvent,
    DateRange,
    District,
    EventBatch,
    Funnel,
    FunnelStep,
    IngestResult,
    Interest,
    InterestCount,
    Orders,
    PanelToFinancials,
    ProductRevenue,
    RepeatUsage,
    ReportedSessions,
    Totals,
)
from core.errors import AppError
from core.models.analytics import AnalyticsEventRecord

DEFAULT_RANGE_DAYS = 30
MAX_RANGE_DAYS = 366
TARGET_SESSIONS_PER_USER = 3
DISTRICTS_LIMIT = 10

FUNNEL_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("map_loaded", ("map_loaded",)),
    ("searched_or_selected", ("search_performed", "parcel_selected")),
    ("panel_viewed", ("panel_viewed",)),
    ("financials_viewed", ("financials_viewed",)),
    ("order_started", ("order_started",)),
    ("checkout_completed", ("checkout_completed",)),
)


@dataclass(frozen=True, slots=True)
class Range:
    start: datetime  # inclusive
    end: datetime  # exclusive


@dataclass(slots=True)
class DashboardRows:
    """Raw aggregate rows (plain dicts / lists) as the repository returns them."""

    totals: Mapping[str, Any]
    funnel: Mapping[str, Any]
    orders: Mapping[str, Any]
    districts: list[Mapping[str, Any]] = field(default_factory=list)
    repeat_usage: Mapping[str, Any] = field(default_factory=dict)
    interest: Mapping[str, Any] = field(default_factory=dict)
    panel_to_financials: Mapping[str, Any] = field(default_factory=dict)


class AnalyticsRepository(Protocol):
    async def insert_events(self, rows: list[dict[str, Any]]) -> int: ...

    async def collect(self, rng: Range, *, districts_limit: int, target: int) -> DashboardRows: ...


# --- SQL repository ------------------------------------------------------------------------------

RANGE = "municipality_id = :m AND occurred_at >= :start_at AND occurred_at < :end_at"
RANGE_E = "e.municipality_id = :m AND e.occurred_at >= :start_at AND e.occurred_at < :end_at"

TOTALS_SQL = text(f"""
    SELECT count(*) AS events,
           count(DISTINCT session_id) AS sessions,
           count(DISTINCT client_id) AS clients,
           COALESCE((SELECT jsonb_object_agg(name, n)
                     FROM (SELECT name, count(*) AS n FROM analytics_events
                           WHERE {RANGE} GROUP BY name) t), '{{}}'::jsonb) AS by_name
    FROM analytics_events WHERE {RANGE}
""")

FUNNEL_SQL = text(f"""
    SELECT
      count(DISTINCT session_id) FILTER (WHERE name = 'map_loaded') AS map_loaded,
      count(DISTINCT session_id) FILTER (WHERE name IN ('search_performed', 'parcel_selected'))
          AS searched_or_selected,
      count(DISTINCT session_id) FILTER (WHERE name = 'panel_viewed') AS panel_viewed,
      count(DISTINCT session_id) FILTER (WHERE name = 'financials_viewed') AS financials_viewed,
      count(DISTINCT session_id) FILTER (WHERE name = 'order_started') AS order_started,
      count(DISTINCT session_id) FILTER (WHERE name = 'checkout_completed') AS checkout_completed
    FROM analytics_events WHERE {RANGE}
""")

ORDERS_SQL = text(f"""
    WITH e AS (
        SELECT id, name, properties FROM analytics_events
        WHERE {RANGE} AND name IN ('order_started', 'checkout_completed')
    ),
    completed AS (
        SELECT COALESCE(properties->>'order_id', 'event:' || id::text) AS order_key,
               max((properties->>'amount_eur')::numeric) AS amount_eur,
               max(properties->>'product') AS product
        FROM e WHERE name = 'checkout_completed' GROUP BY 1
    ),
    by_product AS (
        SELECT COALESCE(product, 'unknown') AS product, count(*) AS orders,
               COALESCE(sum(amount_eur), 0) AS revenue_eur
        FROM completed GROUP BY 1
    )
    SELECT
      (SELECT count(*) FROM e WHERE name = 'order_started') AS order_started_events,
      (SELECT count(DISTINCT COALESCE(properties->>'order_id', 'event:' || id::text))
       FROM e WHERE name = 'order_started') AS orders_started,
      (SELECT count(*) FROM completed) AS orders_completed,
      (SELECT COALESCE(sum(amount_eur), 0) FROM completed) AS revenue_eur,
      (SELECT COALESCE(jsonb_agg(jsonb_build_object('product', product, 'orders', orders,
                                                    'revenue_eur', revenue_eur)
                                 ORDER BY revenue_eur DESC, product), '[]'::jsonb)
       FROM by_product) AS by_product
""")

DISTRICTS_SQL = text(f"""
    SELECT e.zone_id, z.name AS zone_name, count(*) AS events,
           count(*) FILTER (WHERE e.name = 'search_performed') AS searches,
           count(*) FILTER (WHERE e.name = 'parcel_selected') AS selections,
           count(DISTINCT e.session_id) AS sessions
    FROM analytics_events e
    LEFT JOIN zones z ON z.id = e.zone_id
    WHERE {RANGE_E}
      AND e.name IN ('search_performed', 'parcel_selected')
    GROUP BY e.zone_id, z.name
    ORDER BY events DESC, e.zone_id ASC NULLS LAST
    LIMIT :limit
""")

REPEAT_SQL = text(f"""
    WITH s AS (
        SELECT session_id, min(client_id) AS client_id,
               bool_or(name = 'return_visit') AS is_returning
        FROM analytics_events WHERE {RANGE} GROUP BY session_id
    ),
    c AS (
        SELECT client_id, count(*) AS sessions FROM s WHERE client_id IS NOT NULL GROUP BY client_id
    ),
    reported AS (
        SELECT COALESCE(client_id, session_id) AS who,
               max((properties->>'sessions')::int) AS sessions
        FROM analytics_events
        WHERE {RANGE} AND name = 'sessions_per_user' AND properties ? 'sessions'
        GROUP BY 1
    )
    SELECT
      (SELECT count(*) FROM s) AS sessions,
      (SELECT count(*) FROM s WHERE is_returning) AS returning_sessions,
      (SELECT count(*) FROM c) AS clients,
      (SELECT count(*) FROM c WHERE sessions >= :target) AS clients_at_target,
      (SELECT avg(sessions) FROM c) AS sessions_per_client,
      (SELECT count(*) FROM reported) AS reported_users,
      (SELECT count(*) FROM reported WHERE sessions >= :target) AS reported_users_at_target,
      (SELECT avg(sessions) FROM reported) AS reported_sessions_per_user
""")

INTEREST_SQL = text(f"""
    SELECT
      count(*) FILTER (WHERE name = 'market_data_interest') AS market_events,
      count(DISTINCT session_id) FILTER (WHERE name = 'market_data_interest') AS market_sessions,
      count(*) FILTER (WHERE name = 'ai_interest') AS ai_events,
      count(DISTINCT session_id) FILTER (WHERE name = 'ai_interest') AS ai_sessions
    FROM analytics_events
    WHERE {RANGE} AND name IN ('market_data_interest', 'ai_interest')
""")

PANEL_FINANCIALS_SQL = text(f"""
    WITH p AS (
        SELECT session_id, parcel_id FROM analytics_events
        WHERE {RANGE} AND name = 'panel_viewed'
    ),
    f AS (
        SELECT DISTINCT session_id, parcel_id FROM analytics_events
        WHERE {RANGE} AND name = 'financials_viewed'
    ),
    pairs AS (SELECT DISTINCT session_id, parcel_id FROM p)
    SELECT
      (SELECT count(*) FROM p) AS panel_views,
      (SELECT count(*) FROM pairs) AS panel_view_pairs,
      (SELECT count(*) FROM pairs pr
       WHERE EXISTS (SELECT 1 FROM f WHERE f.session_id = pr.session_id
                     AND f.parcel_id IS NOT DISTINCT FROM pr.parcel_id))
          AS pairs_reaching_financials,
      (SELECT count(DISTINCT session_id) FROM p) AS panel_sessions,
      (SELECT count(DISTINCT p.session_id) FROM p
       WHERE EXISTS (SELECT 1 FROM f WHERE f.session_id = p.session_id))
          AS panel_sessions_reaching_financials
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

    async def collect(self, rng: Range, *, districts_limit: int, target: int) -> DashboardRows:
        params = {"m": self.municipality_id, "start_at": rng.start, "end_at": rng.end}
        async with self.session_factory() as session:
            totals = (await session.execute(TOTALS_SQL, params)).mappings().one()
            funnel = (await session.execute(FUNNEL_SQL, params)).mappings().one()
            orders = (await session.execute(ORDERS_SQL, params)).mappings().one()
            districts = (
                (await session.execute(DISTRICTS_SQL, {**params, "limit": districts_limit}))
                .mappings()
                .all()
            )
            repeat = (
                (await session.execute(REPEAT_SQL, {**params, "target": target})).mappings().one()
            )
            interest = (await session.execute(INTEREST_SQL, params)).mappings().one()
            panel = (await session.execute(PANEL_FINANCIALS_SQL, params)).mappings().one()
        return DashboardRows(
            totals=dict(totals),
            funnel=dict(funnel),
            orders=dict(orders),
            districts=[dict(row) for row in districts],
            repeat_usage=dict(repeat),
            interest=dict(interest),
            panel_to_financials=dict(panel),
        )


# --- service -------------------------------------------------------------------------------------


def pct(part: Any, whole: Any) -> float | None:
    whole = _num(whole)
    return None if not whole else round(100.0 * _num(part) / whole, 1)


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _int(value: Any) -> int:
    return int(value or 0)


def _avg(value: Any) -> float | None:
    return None if value is None else round(_num(value), 2)


def resolve_range(
    from_: datetime | None,
    to: datetime | None,
    *,
    now: datetime,
    default_days: int = DEFAULT_RANGE_DAYS,
    max_days: int = MAX_RANGE_DAYS,
) -> Range:
    """``[from, to)`` in UTC (naive = UTC); defaults to the last ``default_days`` days."""
    end = _utc(to) if to is not None else now
    start = _utc(from_) if from_ is not None else end - timedelta(days=default_days)
    problems = []
    if start >= end:
        problems.append({"loc": ["query", "from"], "msg": "'from' must be before 'to'"})
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
        districts_limit: int = DISTRICTS_LIMIT,
        target_sessions_per_user: int = TARGET_SESSIONS_PER_USER,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository = repository
        self.municipality_id = municipality_id
        self.districts_limit = int(districts_limit)
        self.target = int(target_sessions_per_user)
        self.clock = clock

    async def ingest(self, batch: EventBatch) -> IngestResult:
        rows = [self._row(event) for event in batch.events]
        inserted = await self.repository.insert_events(rows)
        return IngestResult(received=len(rows), accepted=inserted, duplicates=len(rows) - inserted)

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
        rows = await self.repository.collect(
            rng, districts_limit=self.districts_limit, target=self.target
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
            totals=_totals(rows.totals),
            funnel=_funnel(rows.funnel),
            orders=_orders(rows.orders),
            districts=_districts(rows.districts),
            repeat_usage=_repeat_usage(rows.repeat_usage, self.target),
            interest=_interest(rows.interest),
            panel_to_financials=_panel_to_financials(rows.panel_to_financials),
        )


def _totals(row: Mapping[str, Any]) -> Totals:
    by_name = {name.value: 0 for name in AnalyticsEvent}
    by_name.update({k: _int(v) for k, v in (row.get("by_name") or {}).items()})
    return Totals(
        events=_int(row.get("events")),
        sessions=_int(row.get("sessions")),
        clients=_int(row.get("clients")),
        by_name=by_name,
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
                    None if previous is None else pct(sessions, previous)
                ),
                conversion_from_start_pct=pct(sessions, start),
            )
        )
        previous = sessions
    return Funnel(steps=steps, overall_conversion_pct=pct(steps[-1].sessions, start))


def _orders(row: Mapping[str, Any]) -> Orders:
    completed = _int(row.get("orders_completed"))
    revenue = round(_num(row.get("revenue_eur")), 2)
    return Orders(
        order_started_events=_int(row.get("order_started_events")),
        orders_started=_int(row.get("orders_started")),
        orders_completed=completed,
        revenue_eur=revenue,
        average_order_eur=round(revenue / completed, 2) if completed else None,
        completion_pct=pct(completed, row.get("orders_started")),
        by_product=[
            ProductRevenue(
                product=str(item.get("product") or "unknown"),
                orders=_int(item.get("orders")),
                revenue_eur=round(_num(item.get("revenue_eur")), 2),
            )
            for item in (row.get("by_product") or [])
        ],
    )


def _districts(rows: list[Mapping[str, Any]]) -> list[District]:
    total = sum(_int(r.get("events")) for r in rows)
    return [
        District(
            zone_id=r.get("zone_id"),
            zone_name=r.get("zone_name"),
            events=_int(r.get("events")),
            searches=_int(r.get("searches")),
            selections=_int(r.get("selections")),
            sessions=_int(r.get("sessions")),
            share_pct=pct(r.get("events"), total) or 0.0,
        )
        for r in rows
    ]


def _repeat_usage(row: Mapping[str, Any], target: int) -> RepeatUsage:
    return RepeatUsage(
        target_sessions_per_user=target,
        sessions=_int(row.get("sessions")),
        returning_sessions=_int(row.get("returning_sessions")),
        repeat_usage_rate_pct=pct(row.get("returning_sessions"), row.get("sessions")),
        clients=_int(row.get("clients")),
        sessions_per_client=_avg(row.get("sessions_per_client")),
        clients_at_target=_int(row.get("clients_at_target")),
        clients_at_target_pct=pct(row.get("clients_at_target"), row.get("clients")),
        reported=ReportedSessions(
            users=_int(row.get("reported_users")),
            users_at_target=_int(row.get("reported_users_at_target")),
            users_at_target_pct=pct(row.get("reported_users_at_target"), row.get("reported_users")),
            sessions_per_user=_avg(row.get("reported_sessions_per_user")),
        ),
    )


def _interest(row: Mapping[str, Any]) -> Interest:
    return Interest(
        market_data_interest=InterestCount(
            events=_int(row.get("market_events")), sessions=_int(row.get("market_sessions"))
        ),
        ai_interest=InterestCount(
            events=_int(row.get("ai_events")), sessions=_int(row.get("ai_sessions"))
        ),
    )


def _panel_to_financials(row: Mapping[str, Any]) -> PanelToFinancials:
    return PanelToFinancials(
        panel_views=_int(row.get("panel_views")),
        panel_view_pairs=_int(row.get("panel_view_pairs")),
        pairs_reaching_financials=_int(row.get("pairs_reaching_financials")),
        reaching_financials_pct=pct(
            row.get("pairs_reaching_financials"), row.get("panel_view_pairs")
        ),
        panel_sessions=_int(row.get("panel_sessions")),
        panel_sessions_reaching_financials=_int(row.get("panel_sessions_reaching_financials")),
        sessions_reaching_financials_pct=pct(
            row.get("panel_sessions_reaching_financials"), row.get("panel_sessions")
        ),
    )
