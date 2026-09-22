"""Per-IP rate limiting: limit of 3 requests / 60 s window (see tests.helpers.make_settings)."""

from __future__ import annotations

import asyncio
import time

from api.app import create_app
from tests.helpers import make_app, make_client, make_redis, make_settings

URL = "/v1/municipality"


async def test_blocks_after_limit_with_retry_after_and_envelope(client):
    for expected_remaining in ("2", "1", "0"):
        r = await client.get(URL)
        assert r.status_code == 200
        assert r.headers["X-RateLimit-Limit"] == "3"
        assert r.headers["X-RateLimit-Remaining"] == expected_remaining
        assert r.headers["X-RateLimit-Reset"].isdigit()

    r = await client.get(URL)
    assert r.status_code == 429
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    assert r.headers["X-RateLimit-Remaining"] == "0"
    body = r.json()
    assert body["error"]["code"] == "rate_limited"
    assert body["error"]["details"] == {"limit": 3, "window_seconds": 60}
    assert body["error"]["request_id"] == r.headers["X-Request-ID"]
    # still limited on the next call
    assert (await client.get(URL)).status_code == 429


async def test_limit_is_per_client_ip(app):
    async with app.router.lifespan_context(app):
        async with make_client(app, "10.0.0.1") as a, make_client(app, "10.0.0.2") as b:
            for _ in range(3):
                assert (await a.get(URL)).status_code == 200
            assert (await a.get(URL)).status_code == 429
            assert (await b.get(URL)).status_code == 200


async def test_health_endpoints_are_exempt(client):
    for _ in range(10):
        r = await client.get("/health")
        assert r.status_code == 200
        assert "X-RateLimit-Limit" not in r.headers


async def test_forwarded_for_is_ignored_unless_trusted():
    app = make_app(make_settings(trust_proxy_headers=False))
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            for i in range(3):
                await c.get(URL, headers={"X-Forwarded-For": f"203.0.113.{i}"})
            r = await c.get(URL, headers={"X-Forwarded-For": "203.0.113.9"})
            assert r.status_code == 429


async def test_forwarded_for_is_used_when_trusted():
    app = make_app(make_settings(trust_proxy_headers=True))
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            for i in range(6):
                r = await c.get(URL, headers={"X-Forwarded-For": f"203.0.113.{i}, 10.0.0.1"})
                assert r.status_code == 200
            # same forwarded ip four times -> limited
            for _ in range(3):
                await c.get(URL, headers={"X-Forwarded-For": "198.51.100.7"})
            r = await c.get(URL, headers={"X-Forwarded-For": "198.51.100.7"})
            assert r.status_code == 429


async def test_limiter_can_be_disabled():
    app = make_app(make_settings(rate_limit_enabled=False))
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            for _ in range(10):
                r = await c.get(URL)
                assert r.status_code == 200
                assert "X-RateLimit-Limit" not in r.headers


async def test_fails_open_when_redis_is_unreachable(app, caplog):
    class BrokenRedis:
        def pipeline(self, transaction: bool = True):
            raise ConnectionError("redis down")

    async with app.router.lifespan_context(app):
        app.state.redis = BrokenRedis()
        async with make_client(app) as c:
            for _ in range(5):
                assert (await c.get(URL)).status_code == 200
    assert any("failing open" in rec.getMessage() for rec in caplog.records)


class SlowPipeline:
    """A pipeline whose execute() never finishes in time (Redis hanging)."""

    calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def incr(self, key):
        return self

    def expire(self, key, seconds):
        return self

    async def execute(self):
        SlowPipeline.calls += 1
        await asyncio.sleep(5)
        return [1, True]


class SlowRedis:
    def pipeline(self, transaction: bool = True):
        return SlowPipeline()


async def test_slow_redis_fails_open_within_the_time_budget():
    """A hanging Redis must not hold the request: the limiter gives up after redis_timeout."""
    SlowPipeline.calls = 0
    settings = make_settings(rate_limit_redis_timeout_ms=100, rate_limit_fail_open_seconds=0)
    app = make_app(settings, SlowRedis())
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            started = time.perf_counter()
            r = await c.get(URL)
            elapsed = time.perf_counter() - started
    assert r.status_code == 200
    assert elapsed < 1.0, f"request took {elapsed:.2f}s; the limiter did not fail open in time"
    assert SlowPipeline.calls == 1


async def test_circuit_breaker_skips_redis_during_cooldown():
    """After a failure the limiter stops calling Redis until fail_open_seconds have passed."""

    class CountingBrokenRedis:
        calls = 0

        def pipeline(self, transaction: bool = True):
            CountingBrokenRedis.calls += 1
            raise ConnectionError("redis down")

    clock = {"now": 1_800_000_000.0}
    settings = make_settings(rate_limit_fail_open_seconds=5)
    app = create_app(
        settings, redis_client=CountingBrokenRedis(), rate_limit_clock=lambda: clock["now"]
    )
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            assert (await c.get(URL)).status_code == 200
            assert CountingBrokenRedis.calls == 1
            for _ in range(3):  # within cooldown: no Redis calls at all
                assert (await c.get(URL)).status_code == 200
            assert CountingBrokenRedis.calls == 1
            clock["now"] += 6  # cooldown over: Redis is probed again
            assert (await c.get(URL)).status_code == 200
            assert CountingBrokenRedis.calls == 2


async def test_windows_reset_the_counter():
    """A new window (clock advanced past the window) starts a fresh count."""
    redis = make_redis()
    clock = {"now": 1_800_000_000.0}
    app = create_app(make_settings(), redis_client=redis, rate_limit_clock=lambda: clock["now"])
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            for _ in range(3):
                assert (await c.get(URL)).status_code == 200
            assert (await c.get(URL)).status_code == 429
            clock["now"] += 60
            assert (await c.get(URL)).status_code == 200
