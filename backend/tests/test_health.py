from __future__ import annotations

import logging


async def test_health_ok(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["municipality"] == "podgorica"
    assert body["env"] == "dev"


async def test_timing_and_request_id_headers(client):
    r = await client.get("/health")
    assert len(r.headers["X-Request-ID"]) == 32  # generated uuid hex
    assert r.headers["Server-Timing"].startswith("app;dur=")
    assert r.headers["X-Response-Time"].endswith("ms")


async def test_incoming_request_id_is_honoured(client):
    r = await client.get("/health", headers={"X-Request-ID": "trace-abc.123:xyz"})
    assert r.headers["X-Request-ID"] == "trace-abc.123:xyz"


async def test_unsafe_incoming_request_id_is_replaced(client):
    r = await client.get("/health", headers={"X-Request-ID": "bad id with spaces <script>"})
    assert r.headers["X-Request-ID"] != "bad id with spaces <script>"
    assert len(r.headers["X-Request-ID"]) == 32


async def test_access_log_has_route_template_status_and_latency(client, caplog):
    with caplog.at_level(logging.INFO, logger="urbanview.http"):
        r = await client.get("/v1/locate", params={"lat": 42.44, "lng": 19.26})
    assert r.status_code == 200
    record = next(rec for rec in caplog.records if rec.name == "urbanview.http")
    assert record.route == "/v1/locate"
    assert record.method == "GET"
    assert record.status == 200
    assert record.duration_ms >= 0
    assert record.slow is False
    assert record.request_id == r.headers["X-Request-ID"]
    assert record.session_id is None


async def test_access_log_carries_a_well_formed_session_id_only(client, caplog):
    with caplog.at_level(logging.INFO, logger="urbanview.http"):
        await client.get("/health", headers={"X-Session-ID": "a1B2c3D4e5F6g7H8"})
        await client.get("/health", headers={"X-Session-ID": "not a session <id>"})
    records = [rec for rec in caplog.records if rec.name == "urbanview.http"]
    assert [rec.session_id for rec in records] == ["a1B2c3D4e5F6g7H8", None]
