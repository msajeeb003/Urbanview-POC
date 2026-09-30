"""``POST /v1/events`` with a fake repository: the 13 names, row-by-row validation (ids,
properties, personal data, timestamps), the batch envelope, what gets stored, and de-duplication
of retried events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.models.analytics import EVENT_NAMES, AnalyticsEvent
from tests.helpers import make_app, make_client, make_redis, make_settings

URL = "/v1/events"


class FakeAnalyticsRepository:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.seen_event_ids: set[str] = set()

    async def insert_events(self, rows):
        inserted = 0
        for row in rows:
            event_id = row.get("event_id")
            if event_id is not None:
                if event_id in self.seen_event_ids:
                    continue
                self.seen_event_ids.add(event_id)
            self.rows.append(row)
            inserted += 1
        return inserted

    async def collect(self, rng, *, limit, min_sessions):  # pragma: no cover - not used here
        raise AssertionError("the ingest tests never build the dashboard")


def build(repository=None, **overrides):
    settings = make_settings(rate_limit_requests=1000, **overrides)
    repository = repository if repository is not None else FakeAnalyticsRepository()
    return make_app(settings, make_redis(), analytics_repository=repository)


def event(name="map_loaded", **overrides):
    base = {
        "name": name,
        "session_id": "session-0001-abcd",
        "occurred_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "properties": {},
    }
    base.update(overrides)
    return base


async def post(app, body):
    async with app.router.lifespan_context(app), make_client(app) as client:
        return await client.post(URL, json=body)


def test_the_thirteen_events_are_the_brd_eleven_and_the_two_intent_buttons():
    assert list(EVENT_NAMES) == [
        "map_loaded",
        "search_performed",
        "parcel_selected",
        "layer_toggled",
        "panel_viewed",
        "financials_viewed",
        "source_reference_opened",
        "order_started",
        "checkout_completed",
        "return_visit",
        "sessions_per_user",
        "market_data_interest",
        "ai_interest",
    ]
    # one list: the database check (ck_analytics_events_name) is built from the same enum
    assert EVENT_NAMES == tuple(e.value for e in AnalyticsEvent)


async def test_a_valid_batch_is_stored_with_the_grouping_columns_extracted():
    repository = FakeAnalyticsRepository()
    batch = {
        "events": [
            event("map_loaded", client_id="client-0001-xyz"),
            event(
                "search_performed",
                properties={"search_kind": "address", "zone_id": 1},
                event_id="evt-0000-0001",
            ),
            event(
                "parcel_selected",
                client_id="client-0001-xyz",
                properties={"parcel_id": 1001, "zone_id": 1, "via": "map_click"},
            ),
            event(
                "checkout_completed",
                properties={
                    "order_id": "order-0001",
                    "amount_eur": 49.0,
                    "product": "expert_report",
                },
            ),
        ]
    }
    r = await post(build(repository), batch)
    assert r.status_code == 202, r.text
    assert r.json() == {"received": 4, "accepted": 4, "duplicates": 0, "rejected": []}
    assert [row["name"] for row in repository.rows] == [
        "map_loaded",
        "search_performed",
        "parcel_selected",
        "checkout_completed",
    ]
    selected = repository.rows[2]
    assert selected["municipality_id"] == "podgorica"
    assert selected["session_id"] == "session-0001-abcd"
    assert selected["client_id"] == "client-0001-xyz"
    assert (selected["zone_id"], selected["parcel_id"]) == (1, 1001)
    assert selected["properties"] == {"parcel_id": 1001, "zone_id": 1, "via": "map_click"}
    assert selected["occurred_at"].tzinfo is not None
    assert selected["occurred_at"].utcoffset() == timedelta(0)
    assert repository.rows[1]["event_id"] == "evt-0000-0001"
    assert repository.rows[0]["event_id"] is None
    assert repository.rows[0]["zone_id"] is None and repository.rows[0]["parcel_id"] is None
    # nothing personal, nothing about the request, is stored
    assert not {"ip", "user_agent", "email"} & set(selected)


async def test_a_malformed_row_is_rejected_without_dropping_the_rest():
    repository = FakeAnalyticsRepository()
    batch = {
        "events": [
            event("map_loaded"),
            event("page_viewed"),  # not one of the 13
            "not an object",
            event("panel_viewed", properties={"note": "contact ana@example.com please"}),
            event("layer_toggled", properties={"layer_id": "zones", "on": True}),
        ]
    }
    r = await post(build(repository), batch)
    assert r.status_code == 202, r.text
    body = r.json()
    assert (body["received"], body["accepted"], body["duplicates"]) == (5, 2, 0)
    assert [row["name"] for row in repository.rows] == ["map_loaded", "layer_toggled"]
    rejected = {row["index"]: row["problems"] for row in body["rejected"]}
    assert set(rejected) == {1, 2, 3}
    assert rejected[1][0]["loc"] == ["name"] and rejected[1][0]["type"] == "enum"
    assert rejected[2][0]["type"] == "model_type"
    assert rejected[3][0]["loc"] == ["properties"]
    assert "personal data" in rejected[3][0]["msg"]
    # a rejection never echoes what was sent
    assert "ana@example.com" not in r.text


async def test_a_batch_whose_every_row_is_malformed_is_422_per_row():
    repository = FakeAnalyticsRepository()
    r = await post(
        build(repository),
        {"events": [event("page_viewed"), event(session_id="short")]},
    )
    assert r.status_code == 422, r.text
    body = r.json()["error"]
    assert body["code"] == "validation_error"
    locs = [d["loc"] for d in body["details"]]
    assert ["body", "events", 0, "name"] in locs
    assert ["body", "events", 1, "session_id"] in locs
    assert repository.rows == []


@pytest.mark.parametrize(
    "session_id",
    ["short", "has space here", "user@example.com", "", "x" * 65, "sess/0001"],
)
async def test_session_ids_must_be_anonymous_opaque_tokens(session_id):
    r = await post(build(), {"events": [event(session_id=session_id)]})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "properties",
    [
        {"email": "x"},
        {"name": "Ana"},
        {"first_name": "Ana"},
        {"phone": "+382 67 000 000"},
        {"ip": "1.1.1.1"},
        {"user_agent": "Mozilla"},
        {"address": "Ulica Slobode 12"},
        {"note": "contact ana@example.com please"},
        {"origin": "seen from 192.168.1.10 today"},
        {"origin": "2001:db8::1"},
    ],
)
async def test_personal_data_is_rejected(properties):
    repository = FakeAnalyticsRepository()
    r = await post(build(repository), {"events": [event(properties=properties)]})
    assert r.status_code == 422, r.text
    assert "personal" in r.text
    assert repository.rows == []


@pytest.mark.parametrize(
    "properties",
    [
        {"nested": {"a": 1}},
        {"items": [1, 2]},
        {f"k{i}": i for i in range(21)},
        {"label": "x" * 201},
        {"Bad-Key": 1},
        {"_private": 1},
    ],
    ids=["nested object", "list", "21 keys", "long string", "bad key", "underscore key"],
)
async def test_properties_must_be_small_and_flat(properties):
    r = await post(build(), {"events": [event(properties=properties)]})
    assert r.status_code == 422, r.text


@pytest.mark.parametrize(
    ("value", "status"),
    [
        ("0.1.0", 202),
        ("999.1.1.1", 202),  # not a valid IPv4 address
        ("10:30:00", 202),  # a time, not an IPv6 address
        ("build 1.2.3.4-beta", 422),  # a valid IPv4 address inside the text is still an IP
        ("fe80::1%25eth0", 422),
    ],
)
async def test_ip_detection_is_exact_not_pattern_happy(value, status):
    r = await post(build(), {"events": [event(properties={"app_version": value})]})
    assert r.status_code == status, r.text


@pytest.mark.parametrize(
    ("name", "properties"),
    [
        ("search_performed", {}),
        ("search_performed", {"search_kind": "voice"}),
        ("search_performed", {"search_kind": "address", "matched": "no"}),
        ("search_performed", {"search_kind": "address", "result": "street"}),
        ("search_performed", {"search_kind": "address", "recent": 1}),
        ("search_performed", {"search_kind": "click", "lat": 95.0, "lng": 19.26}),
        ("search_performed", {"search_kind": "click", "lat": 42.44, "lng": -181}),
        ("search_performed", {"search_kind": "click", "lat": "42.44", "lng": 19.26}),
        ("search_performed", {"search_kind": "click", "lat": True, "lng": 19.26}),
        ("search_performed", {"search_kind": "click", "coverage": "outside"}),
        ("search_performed", {"search_kind": "click", "coverage": True}),
        ("layer_toggled", {}),
        ("layer_toggled", {"layer_id": "zones", "visible": "yes"}),
        ("source_reference_opened", {"document_id": 2}),
        ("source_reference_opened", {"document_id": 2, "page": 0}),
        ("checkout_completed", {"order_id": "order-1"}),
        ("checkout_completed", {"amount_eur": -1}),
        ("checkout_completed", {"amount_eur": "49"}),
        ("parcel_selected", {"parcel_id": "1001"}),
        ("parcel_selected", {"parcel_id": 0}),
        ("parcel_selected", {"parcel_id": True}),
        ("panel_viewed", {"panel_type": "map"}),
        ("sessions_per_user", {"sessions": 0}),
        ("checkout_completed", {"amount_eur": 10, "currency": "USD"}),
        # not one of the 13 (the Group 2 edit event is not in the plan's list)
        ("assumption_edited", {"assumption": "saleable_share", "assumption_value": 0.7}),
    ],
)
async def test_known_properties_are_typed_and_some_are_required(name, properties):
    r = await post(build(), {"events": [event(name, properties=properties)]})
    assert r.status_code == 422, r.text


@pytest.mark.parametrize(
    ("name", "properties"),
    [
        ("search_performed", {"search_kind": "address"}),
        ("search_performed", {"search_kind": "click", "zone_id": 2}),
        (
            "search_performed",
            {
                "search_kind": "click",
                "matched": True,
                "lat": 42.4425,
                "lng": 19.253,
                "urban_parcel_id": 7,
                "zone_id": 2,
            },
        ),
        (
            "search_performed",
            {
                "search_kind": "parcel_number",
                "result": "parcel",
                "lat": -90,
                "lng": 180,
                "parcel_id": 1001,
            },
        ),
        ("search_performed", {"search_kind": "parcel_number"}),
        (
            "search_performed",
            {"search_kind": "click", "matched": False, "coverage": "uncovered", "lat": 42.4415},
        ),
        ("search_performed", {"search_kind": "click", "matched": False, "coverage": "no_parcel"}),
        ("search_performed", {"search_kind": "click", "matched": False, "coverage": "failed"}),
        ("search_performed", {"search_kind": "address", "result": "zone", "coverage": "covered"}),
        (
            "search_performed",
            {"search_kind": "parcel_number", "matched": False, "result": "parcel"},
        ),
        (
            "search_performed",
            {"search_kind": "address", "matched": True, "result": "zone", "recent": True},
        ),
        ("layer_toggled", {"layer_id": "zones", "visible": False}),
        ("source_reference_opened", {"document_id": 2, "page": 12, "value_id": 1}),
        ("checkout_completed", {"amount_eur": 49, "currency": "EUR", "order_id": "o-1"}),
        ("panel_viewed", {"panel_type": "urban", "parcel_id": 1001, "custom_flag": None}),
        ("sessions_per_user", {"sessions": 3}),
        ("return_visit", {"days_since_last": 0}),
        ("ai_interest", {"trigger": "parcel_panel", "extra_scalar": 1.5}),
        ("market_data_interest", {"trigger": "parcel_panel", "panel_type": "urban"}),
    ],
)
async def test_well_formed_events_are_accepted(name, properties):
    r = await post(build(), {"events": [event(name, properties=properties)]})
    assert r.status_code == 202, r.text


@pytest.mark.parametrize(
    ("offset", "status"),
    [
        (None, 422),  # naive: no timezone
        (timedelta(minutes=10), 422),
        (timedelta(minutes=2), 202),
        (timedelta(days=-365), 202),
        ("garbage", 422),
    ],
    ids=["naive", "10 min in the future", "2 min in the future", "a year ago", "garbage"],
)
async def test_timestamps_need_a_timezone_and_may_not_be_in_the_future(offset, status):
    # computed when the test runs, not when it is collected (a slow run moved the boundary)
    if offset is None:
        occurred_at = "2026-09-24T10:00:00"
    elif isinstance(offset, str):
        occurred_at = "not a date"
    else:
        occurred_at = (datetime.now(UTC) + offset).isoformat()
    r = await post(build(), {"events": [event(occurred_at=occurred_at)]})
    assert r.status_code == status, r.text


@pytest.mark.parametrize(
    "body",
    [
        {"events": []},
        {"events": [event() for _ in range(101)]},
        {},
        {"events": "map_loaded"},
        {"events": [event()], "source": "web"},
        {"events": [{**event(), "ip": "1.2.3.4"}]},
    ],
    ids=[
        "empty",
        "101 events",
        "no events key",
        "events not a list",
        "unknown top-level key",
        "unknown event key",
    ],
)
async def test_batch_shape_is_enforced(body):
    repository = FakeAnalyticsRepository()
    r = await post(build(repository), body)
    assert r.status_code == 422, r.text
    assert repository.rows == []


async def test_a_full_batch_of_100_is_accepted():
    r = await post(build(), {"events": [event(event_id=f"evt-{i:08d}") for i in range(100)]})
    assert r.status_code == 202
    assert r.json() == {"received": 100, "accepted": 100, "duplicates": 0, "rejected": []}


async def test_retried_events_are_counted_as_duplicates_not_stored_twice():
    repository = FakeAnalyticsRepository()
    app = build(repository)
    batch = {
        "events": [
            event(event_id="evt-0000-0001"),
            event(event_id="evt-0000-0001"),
            event(event_id="evt-0000-0002"),
        ]
    }
    async with app.router.lifespan_context(app), make_client(app) as client:
        first = await client.post(URL, json=batch)
        second = await client.post(URL, json=batch)
    assert first.json() == {"received": 3, "accepted": 2, "duplicates": 1, "rejected": []}
    assert second.json() == {"received": 3, "accepted": 0, "duplicates": 3, "rejected": []}
    assert len(repository.rows) == 2


async def test_without_the_events_database_ingest_is_503():
    app = make_app(make_settings(), make_redis())  # nodata resolver, no repository injected
    r = await post(app, {"events": [event()]})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "service_unavailable"
