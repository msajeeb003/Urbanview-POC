"""``GET /v1/admin/analytics``: the role gate and the dashboard assembly from canned aggregate rows
(the SQL behind those rows is covered by tests/integration/test_analytics_postgis.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from api.schemas.analytics import AnalyticsEvent
from api.services.analytics import DashboardRows, Range
from core.auth import Principal, Role, TokenAuthenticator, parse_api_tokens
from tests.helpers import make_app, make_client, make_redis, make_settings

URL = "/v1/admin/analytics"
ADMIN = "dash-token-1234"
REVIEWER = "review-token-1"
TOKENS = f"{ADMIN}:admin:client-dashboard, {REVIEWER}:reviewer:ana"

ROWS = DashboardRows(
    totals={
        "events": 40,
        "sessions": 4,
        "clients": 2,
        "by_name": {
            "map_loaded": 4,
            "panel_viewed": 4,
            "financials_viewed": 2,
            "checkout_completed": 1,
        },
    },
    funnel={
        "map_loaded": 4,
        "searched_or_selected": 3,
        "panel_viewed": 3,
        "financials_viewed": 2,
        "order_started": 1,
        "checkout_completed": 1,
    },
    orders={
        "order_started_events": 2,
        "orders_started": 1,
        "orders_completed": 1,
        "revenue_eur": Decimal("49.00"),
        "by_product": [{"product": "expert_report", "orders": 1, "revenue_eur": 49}],
    },
    districts=[
        {
            "zone_id": 1,
            "zone_name": "Centar",
            "events": 3,
            "searches": 2,
            "selections": 1,
            "sessions": 2,
        },
        {
            "zone_id": None,
            "zone_name": None,
            "events": 1,
            "searches": 1,
            "selections": 0,
            "sessions": 1,
        },
    ],
    repeat_usage={
        "sessions": 4,
        "returning_sessions": 2,
        "clients": 2,
        "clients_at_target": 0,
        "sessions_per_client": Decimal("1.5"),
        "reported_users": 1,
        "reported_users_at_target": 0,
        "reported_sessions_per_user": Decimal("2"),
    },
    interest={"market_events": 3, "market_sessions": 2, "ai_events": 1, "ai_sessions": 1},
    panel_to_financials={
        "panel_views": 4,
        "panel_view_pairs": 4,
        "pairs_reaching_financials": 2,
        "panel_sessions": 3,
        "panel_sessions_reaching_financials": 2,
    },
)
EMPTY = DashboardRows(totals={}, funnel={}, orders={})


class FakeAnalyticsRepository:
    def __init__(self, rows: DashboardRows = ROWS) -> None:
        self.rows = rows
        self.calls: list[tuple[Range, int, int]] = []

    async def insert_events(self, rows):
        return len(rows)

    async def collect(self, rng, *, districts_limit, target):
        self.calls.append((rng, districts_limit, target))
        return self.rows


def build(repository=None, tokens=TOKENS, **overrides):
    settings = make_settings(rate_limit_requests=1000, admin_api_tokens=tokens, **overrides)
    repository = repository if repository is not None else FakeAnalyticsRepository()
    return make_app(settings, make_redis(), analytics_repository=repository)


async def get(app, params=None, token=ADMIN, headers=None):
    headers = dict(headers or {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with app.router.lifespan_context(app), make_client(app) as client:
        return await client.get(URL, params=params, headers=headers)


# --- role gate ---------------------------------------------------------------------------------


async def test_no_token_is_401_with_a_challenge():
    r = await get(build(), token=None)
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"
    assert r.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Basic ZGFzaC10b2tlbi0xMjM0"},
        {"Authorization": "Bearer"},
        {"Authorization": f"Token {ADMIN}"},
    ],
)
async def test_wrong_or_malformed_credentials_are_401(headers):
    r = await get(build(), token=None, headers=headers)
    assert r.status_code == 401


async def test_a_reviewer_token_is_403():
    r = await get(build(), token=REVIEWER)
    assert r.status_code == 403
    body = r.json()["error"]
    assert body["code"] == "forbidden"
    assert body["details"] == {"required_roles": ["admin"]}


async def test_no_tokens_configured_means_no_access():
    r = await get(build(tokens=None))
    assert r.status_code == 401


async def test_an_admin_token_opens_the_dashboard_uncached():
    r = await get(build())
    assert r.status_code == 200, r.text
    assert r.headers["Cache-Control"] == "no-store"
    assert r.json()["municipality_id"] == "podgorica"


def test_token_parsing_and_authentication():
    tokens = parse_api_tokens(" a-token:admin , b-token:Reviewer:ana ,c-token:expert:")
    assert tokens == {
        "a-token": Principal(subject="admin", role=Role.admin),
        "b-token": Principal(subject="ana", role=Role.reviewer),
        "c-token": Principal(subject="expert", role=Role.expert),
    }
    assert parse_api_tokens(None) == {} and parse_api_tokens("  ") == {}
    for bad in ("only-token", "t:god", ":admin", "x:admin,x:reviewer"):
        with pytest.raises(ValueError):
            parse_api_tokens(bad)
    auth = TokenAuthenticator(tokens)
    assert auth.configured
    assert auth.authenticate("Bearer b-token") == Principal(subject="ana", role=Role.reviewer)
    assert auth.authenticate("bearer  a-token ") == Principal(subject="admin", role=Role.admin)
    assert auth.authenticate("Bearer nope") is None
    assert auth.authenticate("Basic a-token") is None
    assert auth.authenticate(None) is None and auth.authenticate("") is None
    assert not TokenAuthenticator({}).configured


def test_malformed_token_settings_fail_at_startup():
    with pytest.raises(ValidationError):
        make_settings(admin_api_tokens="tok:god")


# --- assembly ----------------------------------------------------------------------------------


async def test_funnel_conversions_are_session_ratios():
    body = (await get(build())).json()
    funnel = body["funnel"]
    assert funnel["basis"] == "sessions"
    steps = funnel["steps"]
    assert [s["step"] for s in steps] == [
        "map_loaded",
        "searched_or_selected",
        "panel_viewed",
        "financials_viewed",
        "order_started",
        "checkout_completed",
    ]
    assert steps[1]["event_names"] == ["search_performed", "parcel_selected"]
    assert [s["sessions"] for s in steps] == [4, 3, 3, 2, 1, 1]
    assert [s["conversion_from_previous_pct"] for s in steps] == [
        None,
        75.0,
        100.0,
        66.7,
        50.0,
        100.0,
    ]
    assert [s["conversion_from_start_pct"] for s in steps] == [
        100.0,
        75.0,
        75.0,
        50.0,
        25.0,
        25.0,
    ]
    assert funnel["overall_conversion_pct"] == 25.0


async def test_orders_and_revenue():
    orders = (await get(build())).json()["orders"]
    assert orders == {
        "source": "analytics_events",
        "order_started_events": 2,
        "orders_started": 1,
        "orders_completed": 1,
        "revenue_eur": 49.0,
        "average_order_eur": 49.0,
        "completion_pct": 100.0,
        "by_product": [{"product": "expert_report", "orders": 1, "revenue_eur": 49.0}],
    }


async def test_most_searched_districts_with_shares():
    districts = (await get(build())).json()["districts"]
    assert districts == [
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
            "zone_id": None,
            "zone_name": None,
            "events": 1,
            "searches": 1,
            "selections": 0,
            "sessions": 1,
            "share_pct": 25.0,
        },
    ]


async def test_repeat_usage_against_the_prototype_target():
    repeat = (await get(build())).json()["repeat_usage"]
    assert repeat == {
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


async def test_interest_counts_and_panel_views_reaching_financials():
    body = (await get(build())).json()
    assert body["interest"] == {
        "market_data_interest": {"events": 3, "sessions": 2},
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


async def test_totals_list_every_event_name():
    totals = (await get(build())).json()["totals"]
    assert (totals["events"], totals["sessions"], totals["clients"]) == (40, 4, 2)
    assert set(totals["by_name"]) == {name.value for name in AnalyticsEvent}
    assert totals["by_name"]["map_loaded"] == 4
    assert totals["by_name"]["source_reference_opened"] == 0


async def test_an_empty_range_has_no_division_errors():
    body = (await get(build(FakeAnalyticsRepository(EMPTY)))).json()
    assert body["totals"]["events"] == 0
    assert all(s["sessions"] == 0 for s in body["funnel"]["steps"])
    assert body["funnel"]["overall_conversion_pct"] is None
    assert body["funnel"]["steps"][0]["conversion_from_start_pct"] is None
    assert body["orders"]["average_order_eur"] is None
    assert body["orders"]["completion_pct"] is None
    assert body["districts"] == []
    assert body["repeat_usage"]["repeat_usage_rate_pct"] is None
    assert body["repeat_usage"]["sessions_per_client"] is None
    assert body["panel_to_financials"]["reaching_financials_pct"] is None


# --- date range --------------------------------------------------------------------------------


async def test_default_range_is_the_last_30_days():
    repository = FakeAnalyticsRepository()
    before = datetime.now(UTC)
    body = (await get(build(repository))).json()
    (rng, limit, target) = repository.calls[0]
    assert rng.end - rng.start == timedelta(days=30)
    assert before <= rng.end <= datetime.now(UTC)
    assert (limit, target) == (10, 3)
    assert body["range"]["days"] == 30.0
    assert datetime.fromisoformat(body["range"]["to"]) == rng.end
    assert datetime.fromisoformat(body["generated_at"]) == rng.end


async def test_explicit_range_accepts_dates_and_naive_datetimes_as_utc():
    repository = FakeAnalyticsRepository()
    body = (
        await get(build(repository), params={"from": "2026-09-01", "to": "2026-09-22T12:00:00"})
    ).json()
    (rng, _, _) = repository.calls[0]
    assert rng == Range(
        start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 9, 22, 12, tzinfo=UTC)
    )
    assert body["range"] == {
        "from": "2026-09-01T00:00:00Z",
        "to": "2026-09-22T12:00:00Z",
        "days": 21.5,
    }


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-09-22", "to": "2026-09-22"},
        {"from": "2026-09-23", "to": "2026-09-22"},
        {"from": "2025-01-01", "to": "2026-09-22"},
        {"from": "yesterday"},
    ],
    ids=["empty range", "reversed", "over 366 days", "garbage"],
)
async def test_bad_ranges_are_422(params):
    r = await get(build(), params=params)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_error"
