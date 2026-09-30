"""``GET /v1/admin/analytics``: the role gate and the dashboard assembly from canned aggregate rows
(the SQL behind those rows is covered by tests/integration/test_analytics_postgis.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from api.services.analytics import DashboardRows, Range
from core.auth import Principal, Role, TokenAuthenticator, parse_api_tokens
from tests.helpers import make_app, make_client, make_redis, make_settings

URL = "/v1/admin/analytics"
ADMIN = "dash-token-1234"
REVIEWER = "review-token-1"
TOKENS = f"{ADMIN}:admin:client-dashboard, {REVIEWER}:reviewer:ana"

ROWS = DashboardRows(
    funnel={
        "map_loaded": 4,
        "parcel_resolved": 3,
        "panel_opened": 3,
        "order_started": 2,
        "order_submitted": 1,
        "paid": 1,
    },
    orders=[
        {"status": "pending_payment", "orders": 2, "amount_eur": Decimal("300.00")},
        {"status": "paid", "orders": 1, "amount_eur": Decimal("100.00")},
    ],
    top_zones=[
        {
            "zone_id": 1,
            "zone_name": "Centar",
            "covered": True,
            "events": 3,
            "searches": 2,
            "uncovered_searches": 0,
            "selections": 1,
            "sessions": 2,
        },
        {
            "zone_id": 5,
            "zone_name": "Konik",
            "covered": False,
            "events": 2,
            "searches": 2,
            "uncovered_searches": 2,
            "selections": 0,
            "sessions": 1,
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
    uncovered_hits=[
        {"lat": Decimal("42.460"), "lng": Decimal("19.281"), "searches": 2, "sessions": 1},
        {"lat": Decimal("42.401"), "lng": Decimal("19.230"), "searches": 1, "sessions": 1},
    ],
    repeat_sessions={"sessions": 5, "visitors": 2, "repeat_visitors": 1, "repeat_sessions": 3},
    intent={"market_events": 3, "market_sessions": 2, "ai_events": 1, "ai_sessions": 1},
)
EMPTY = DashboardRows(funnel={})


class FakeAnalyticsRepository:
    def __init__(self, rows: DashboardRows = ROWS) -> None:
        self.rows = rows
        self.calls: list[tuple[Range, int, int]] = []

    async def insert_events(self, rows):
        return len(rows)

    async def collect(self, rng, *, limit, min_sessions):
        self.calls.append((rng, limit, min_sessions))
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


async def test_the_payload_holds_exactly_the_plan_aggregates_and_no_customer_data():
    body = (await get(build())).json()
    assert set(body) == {
        "municipality_id",
        "range",
        "generated_at",
        "funnel",
        "orders",
        "top_zones",
        "uncovered_hits",
        "repeat_sessions",
        "intent_counts",
    }
    text = json.dumps(body)
    assert "@" not in text and "email" not in text and "first_name" not in text


async def test_funnel_from_map_to_paid_with_conversions():
    funnel = (await get(build())).json()["funnel"]
    assert funnel["basis"] == "sessions"
    steps = funnel["steps"]
    assert [s["step"] for s in steps] == [
        "map_loaded",
        "parcel_resolved",
        "panel_opened",
        "order_started",
        "order_submitted",
        "paid",
    ]
    assert [s["event_names"] for s in steps] == [
        ["map_loaded"],
        ["parcel_selected"],
        ["panel_viewed"],
        ["order_started"],
        ["checkout_completed"],
        [],  # the orders table: the submitted order has been paid
    ]
    assert [s["sessions"] for s in steps] == [4, 3, 3, 2, 1, 1]
    assert [s["conversion_from_previous_pct"] for s in steps] == [
        100.0,
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


async def test_orders_by_status_list_every_status_in_flow_order():
    orders = (await get(build())).json()["orders"]
    assert orders == {
        "placed": 3,
        "by_status": [
            {"status": "pending_payment", "orders": 2, "amount_eur": 300.0},
            {"status": "paid", "orders": 1, "amount_eur": 100.0},
            {"status": "payment_failed", "orders": 0, "amount_eur": 0.0},
            {"status": "in_progress", "orders": 0, "amount_eur": 0.0},
            {"status": "delivered", "orders": 0, "amount_eur": 0.0},
            {"status": "refunded", "orders": 0, "amount_eur": 0.0},
        ],
    }


async def test_top_zones_with_shares_and_uncovered_hits_by_position():
    body = (await get(build())).json()
    assert body["top_zones"] == [
        {
            "zone_id": 1,
            "zone_name": "Centar",
            "covered": True,
            "events": 3,
            "searches": 2,
            "uncovered_searches": 0,
            "selections": 1,
            "sessions": 2,
            "share_pct": 50.0,
        },
        # searches outside coverage count for the district they were made in (S6 demand)
        {
            "zone_id": 5,
            "zone_name": "Konik",
            "covered": False,
            "events": 2,
            "searches": 2,
            "uncovered_searches": 2,
            "selections": 0,
            "sessions": 1,
            "share_pct": 33.3,
        },
        {
            "zone_id": None,
            "zone_name": None,
            "covered": None,
            "events": 1,
            "searches": 1,
            "uncovered_searches": 0,
            "selections": 0,
            "sessions": 1,
            "share_pct": 16.7,
        },
    ]
    assert body["uncovered_hits"] == [
        {"lat": 42.46, "lng": 19.281, "searches": 2, "sessions": 1},
        {"lat": 42.401, "lng": 19.23, "searches": 1, "sessions": 1},
    ]


async def test_repeat_sessions_are_visitors_with_three_or_more_and_intent_counts():
    body = (await get(build())).json()
    assert body["repeat_sessions"] == {
        "min_sessions": 3,
        "sessions": 5,
        "visitors": 2,
        "repeat_visitors": 1,
        "repeat_visitors_pct": 50.0,
        "repeat_sessions": 3,
    }
    assert body["intent_counts"] == {
        "market_data_interest": {"events": 3, "sessions": 2},
        "ai_interest": {"events": 1, "sessions": 1},
    }


async def test_an_empty_range_answers_zeros():
    body = (await get(build(FakeAnalyticsRepository(EMPTY)))).json()
    steps = body["funnel"]["steps"]
    assert all(s["sessions"] == 0 for s in steps)
    assert all(s["conversion_from_previous_pct"] == 0.0 for s in steps)
    assert all(s["conversion_from_start_pct"] == 0.0 for s in steps)
    assert body["funnel"]["overall_conversion_pct"] == 0.0
    assert body["orders"]["placed"] == 0
    assert [s["orders"] for s in body["orders"]["by_status"]] == [0] * 6
    assert body["top_zones"] == [] and body["uncovered_hits"] == []
    assert body["repeat_sessions"] == {
        "min_sessions": 3,
        "sessions": 0,
        "visitors": 0,
        "repeat_visitors": 0,
        "repeat_visitors_pct": 0.0,
        "repeat_sessions": 0,
    }
    assert body["intent_counts"]["ai_interest"] == {"events": 0, "sessions": 0}


async def test_a_zero_length_range_is_zeros_without_a_query():
    repository = FakeAnalyticsRepository()
    r = await get(build(repository), params={"from": "2026-09-22", "to": "2026-09-22"})
    assert r.status_code == 200, r.text
    assert repository.calls == []
    assert r.json()["funnel"]["overall_conversion_pct"] == 0.0
    assert r.json()["range"]["days"] == 0.0


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
        {"from": "2026-09-23", "to": "2026-09-22"},
        {"from": "2025-01-01", "to": "2026-09-22"},
        {"from": "yesterday"},
    ],
    ids=["reversed", "over 366 days", "garbage"],
)
async def test_bad_ranges_are_422(params):
    r = await get(build(), params=params)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_error"
