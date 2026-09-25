"""Analytics on PostGIS: ingest into ``analytics_events`` (de-duplication, extracted columns,
the name + time index) and every dashboard aggregate on a crafted event set with known answers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tests.helpers import make_app, make_client, make_settings

pytestmark = pytest.mark.integration

URL = "/v1/events"
DASHBOARD = "/v1/admin/analytics"
TOKEN = "dash-token-1234"
T0 = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
RANGE = {"from": "2026-09-20", "to": "2026-09-21"}


@pytest.fixture
def analytics_app(postgis_url):
    settings = make_settings(
        location_resolver="postgis",
        database_url=postgis_url,
        rate_limit_requests=100_000,
        admin_api_tokens=f"{TOKEN}:admin:dashboard",
    )
    return make_app(settings)


def ev(name: str, session: str, minute: int, client: str | None = None, **props: Any) -> dict:
    event = {
        "name": name,
        "session_id": session,
        "event_id": f"{session}-{name}-{minute + 100_000:06d}",  # id charset: no sign
        "occurred_at": (T0 + timedelta(minutes=minute)).isoformat(),
        "properties": props,
    }
    if client:
        event["client_id"] = client
    return event


C1, C2 = "client-000001", "client-000002"
S1, S2, S3, S4 = "session-0001", "session-0002", "session-0003", "session-0004"
EVENTS = [
    # session 1 (client 1): the whole funnel, a purchase, a return visit
    ev("map_loaded", S1, 0, C1),
    ev("return_visit", S1, 0, C1, days_since_last=3),
    ev("search_performed", S1, 1, C1, search_kind="address", zone_id=1),
    ev("parcel_selected", S1, 2, C1, parcel_id=1001, zone_id=1),
    ev("panel_viewed", S1, 3, C1, parcel_id=1001, zone_id=1, panel_type="cadastral"),
    ev("financials_viewed", S1, 4, C1, parcel_id=1001, zone_id=1),
    ev("market_data_interest", S1, 5, C1, parcel_id=1001, trigger="paywall"),
    ev("order_started", S1, 6, C1, order_id="order-0001", product="expert_report"),
    ev(
        "checkout_completed",
        S1,
        8,
        C1,
        order_id="order-0001",
        product="expert_report",
        amount_eur=49,
    ),
    # session 2 (client 1 again): a second zone, no financials, reports 2 sessions
    ev("map_loaded", S2, 60, C1),
    ev("return_visit", S2, 60, C1, days_since_last=0),
    ev("sessions_per_user", S2, 60, C1, sessions=2),
    ev("parcel_selected", S2, 61, C1, parcel_id=1007, zone_id=2),
    ev("panel_viewed", S2, 62, C1, parcel_id=1007, zone_id=2, panel_type="cadastral"),
    ev("ai_interest", S2, 63, C1, trigger="ai_quota"),
    # session 3 (client 2): two panels, financials for one of them
    ev("map_loaded", S3, 120, C2),
    ev("search_performed", S3, 121, C2, search_kind="parcel_number", zone_id=1),
    ev("panel_viewed", S3, 122, C2, parcel_id=1001, zone_id=1, panel_type="cadastral"),
    ev("panel_viewed", S3, 123, C2, parcel_id=1002, zone_id=1, panel_type="cadastral"),
    ev("financials_viewed", S3, 124, C2, parcel_id=1002, zone_id=1),
    ev("layer_toggled", S3, 125, C2, layer_id="zones", visible=True),
    # session 4: anonymous, map only
    ev("map_loaded", S4, 180),
]
A_MONTH_EARLIER = -30 * 24 * 60
OUT_OF_RANGE = [
    ev("map_loaded", "session-0009", A_MONTH_EARLIER),
    ev(
        "checkout_completed",
        "session-0009",
        A_MONTH_EARLIER + 1,
        order_id="order-old",
        amount_eur=99,
    ),
]


async def seed(client) -> dict:
    """Idempotent: every event carries an event_id, so re-runs are counted as duplicates."""
    r = await client.post(URL, json={"events": EVENTS + OUT_OF_RANGE})
    assert r.status_code == 202, r.text
    return r.json()


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


async def test_ingest_stores_rows_once_with_the_grouping_columns(analytics_app):
    app = analytics_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        first = await seed(client)
        second = await seed(client)
        async with app.state.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT name, session_id, client_id, zone_id, parcel_id, properties "
                            "FROM analytics_events WHERE municipality_id = 'podgorica' "
                            "AND session_id = :s ORDER BY occurred_at, name"
                        ),
                        {"s": S1},
                    )
                )
                .mappings()
                .all()
            )
    total = len(EVENTS) + len(OUT_OF_RANGE)
    assert first["received"] == total and first["accepted"] + first["duplicates"] == total
    assert second == {"received": total, "accepted": 0, "duplicates": total}
    selected = next(r for r in rows if r["name"] == "parcel_selected")
    assert (selected["client_id"], selected["zone_id"], selected["parcel_id"]) == (C1, 1, 1001)
    assert selected["properties"] == {"parcel_id": 1001, "zone_id": 1}
    assert len(rows) == 9


async def test_every_dashboard_aggregate(analytics_app):
    app = analytics_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        await seed(client)
        r = await client.get(DASHBOARD, params=RANGE, headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["municipality_id"] == "podgorica"
    assert body["range"] == {
        "from": "2026-09-20T00:00:00Z",
        "to": "2026-09-21T00:00:00Z",
        "days": 1.0,
    }

    totals = body["totals"]
    assert (totals["events"], totals["sessions"], totals["clients"]) == (len(EVENTS), 4, 2)
    assert totals["by_name"] == {
        "map_loaded": 4,
        "search_performed": 2,
        "parcel_selected": 2,
        "layer_toggled": 1,
        "panel_viewed": 4,
        "financials_viewed": 2,
        "source_reference_opened": 0,
        "order_started": 1,
        "checkout_completed": 1,
        "return_visit": 2,
        "sessions_per_user": 1,
        "market_data_interest": 1,
        "ai_interest": 1,
    }

    steps = body["funnel"]["steps"]
    assert [s["sessions"] for s in steps] == [4, 3, 3, 2, 1, 1]
    assert [s["conversion_from_previous_pct"] for s in steps] == [
        None,
        75.0,
        100.0,
        66.7,
        50.0,
        100.0,
    ]
    assert body["funnel"]["overall_conversion_pct"] == 25.0

    assert body["orders"] == {
        "source": "analytics_events",
        "order_started_events": 1,
        "orders_started": 1,
        "orders_completed": 1,
        "revenue_eur": 49.0,
        "average_order_eur": 49.0,
        "completion_pct": 100.0,
        "by_product": [{"product": "expert_report", "orders": 1, "revenue_eur": 49.0}],
    }

    assert body["districts"] == [
        {
            "zone_id": 1,
            "zone_name": "Centar",
            "events": 3,
            "searches": 2,
            "selections": 1,
            "sessions": 2,
            "share_pct": 75.0,
        },
        {
            "zone_id": 2,
            "zone_name": "Stari Aerodrom",
            "events": 1,
            "searches": 0,
            "selections": 1,
            "sessions": 1,
            "share_pct": 25.0,
        },
    ]

    assert body["repeat_usage"] == {
        "target_sessions_per_user": 3,
        "sessions": 4,
        "returning_sessions": 2,
        "repeat_usage_rate_pct": 50.0,
        "clients": 2,
        "sessions_per_client": 1.5,
        "clients_at_target": 0,
        "clients_at_target_pct": 0.0,
        "reported": {
            "users": 1,
            "users_at_target": 0,
            "users_at_target_pct": 0.0,
            "sessions_per_user": 2.0,
        },
    }

    assert body["interest"] == {
        "market_data_interest": {"events": 1, "sessions": 1},
        "ai_interest": {"events": 1, "sessions": 1},
    }

    assert body["panel_to_financials"] == {
        "panel_views": 4,
        "panel_view_pairs": 4,
        "pairs_reaching_financials": 2,
        "reaching_financials_pct": 50.0,
        "panel_sessions": 3,
        "panel_sessions_reaching_financials": 2,
        "sessions_reaching_financials_pct": 66.7,
    }


async def test_the_range_filter_and_the_role_gate(analytics_app):
    app = analytics_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        await seed(client)
        earlier = await client.get(
            DASHBOARD, params={"from": "2026-08-20", "to": "2026-08-22"}, headers=auth()
        )
        anonymous = await client.get(DASHBOARD, params=RANGE)
    assert earlier.status_code == 200
    body = earlier.json()
    assert body["totals"]["events"] == 2
    assert body["orders"]["revenue_eur"] == 99.0
    assert body["districts"] == []
    assert anonymous.status_code == 401


async def test_name_and_time_queries_use_the_index(analytics_app):
    app = analytics_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        await seed(client)
        async with app.state.session_factory() as session:
            await session.execute(text("ANALYZE analytics_events"))
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            plan = (
                await session.execute(
                    text(
                        "EXPLAIN (FORMAT JSON) SELECT count(DISTINCT session_id) "
                        "FROM analytics_events WHERE municipality_id = 'podgorica' "
                        "AND name = 'panel_viewed' AND occurred_at >= :a AND occurred_at < :b"
                    ),
                    {"a": T0, "b": T0 + timedelta(days=1)},
                )
            ).scalar_one()
    plan = json.loads(plan) if isinstance(plan, str) else plan
    assert "ix_analytics_events_name_time" in json.dumps(plan)
