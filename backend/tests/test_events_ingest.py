"""``POST /v1/events`` with a fake repository: strict validation (names, ids, properties, personal
data, timestamps, batch size), what gets stored, and de-duplication of retried events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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

    async def collect(self, rng, *, districts_limit, target):  # pragma: no cover - not used here
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
    assert r.json() == {"received": 4, "accepted": 4, "duplicates": 0}
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


async def test_unknown_event_names_reject_the_whole_batch():
    repository = FakeAnalyticsRepository()
    r = await post(build(repository), {"events": [event("map_loaded"), event("page_viewed")]})
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "validation_error"
    assert any(d["loc"] == ["body", "events", 1, "name"] for d in body["details"])
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
        ("search_performed", {"search_kind": "parcel_number"}),
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
        ("ai_interest", {"trigger": "quota", "extra_scalar": 1.5}),
    ],
)
async def test_well_formed_events_are_accepted(name, properties):
    r = await post(build(), {"events": [event(name, properties=properties)]})
    assert r.status_code == 202, r.text


@pytest.mark.parametrize(
    ("occurred_at", "status"),
    [
        ("2026-09-24T10:00:00", 422),  # naive: no timezone
        ((datetime.now(UTC) + timedelta(minutes=10)).isoformat(), 422),
        ((datetime.now(UTC) + timedelta(minutes=2)).isoformat(), 202),
        ((datetime.now(UTC) - timedelta(days=365)).isoformat(), 202),
        ("not a date", 422),
    ],
    ids=["naive", "10 min in the future", "2 min in the future", "a year ago", "garbage"],
)
async def test_timestamps_need_a_timezone_and_may_not_be_in_the_future(occurred_at, status):
    r = await post(build(), {"events": [event(occurred_at=occurred_at)]})
    assert r.status_code == status, r.text


@pytest.mark.parametrize(
    "body",
    [
        {"events": []},
        {"events": [event() for _ in range(101)]},
        {},
        {"events": [event()], "source": "web"},
        {"events": [{**event(), "ip": "1.2.3.4"}]},
    ],
    ids=["empty", "101 events", "no events key", "unknown top-level key", "unknown event key"],
)
async def test_batch_shape_is_enforced(body):
    r = await post(build(), body)
    assert r.status_code == 422, r.text


async def test_a_full_batch_of_100_is_accepted():
    r = await post(build(), {"events": [event(event_id=f"evt-{i:08d}") for i in range(100)]})
    assert r.status_code == 202
    assert r.json() == {"received": 100, "accepted": 100, "duplicates": 0}


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
    assert first.json() == {"received": 3, "accepted": 2, "duplicates": 1}
    assert second.json() == {"received": 3, "accepted": 0, "duplicates": 3}
    assert len(repository.rows) == 2


async def test_without_the_events_database_ingest_is_503():
    app = make_app(make_settings(), make_redis())  # nodata resolver, no repository injected
    r = await post(app, {"events": [event()]})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "service_unavailable"
