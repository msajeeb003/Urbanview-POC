"""``GET /v1/geocode`` through the app with a fake provider: municipality scope, compact payload,
cache, provider policy (call spacing, failure backoff) and the rule that search never dead-ends."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from core.geocode.base import BoundingBox, GeocodeHit, GeocodeProviderError
from tests.helpers import make_app, make_client, make_redis, make_settings

URL = "/v1/geocode"
PODGORICA_BOX = BoundingBox(19.10, 42.33, 19.45, 42.55)  # municipalities/podgorica.toml

MALL = GeocodeHit("Delta City", "Cetinjski put 5, Podgorica", 42.4310, 19.2380, "poi", "ME")
STREET = GeocodeHit("Bulevar Svetog Petra Cetinjskog", "Podgorica", 42.442, 19.26, "street", "ME")
OUTSIDE_BOX = GeocodeHit("Delta City Beograd", "Jurija Gagarina 16", 44.81, 20.42, "poi", "RS")
FOREIGN_IN_BOX = GeocodeHit("Somewhere", None, 42.44, 19.26, "place", "AL")
NO_COUNTRY = GeocodeHit("Blok 5", "Podgorica", 42.438, 19.245, "place", None)


class FakeProvider:
    name = "fake"

    def __init__(self, hits=(), *, error: Exception | None = None, min_interval_ms=0) -> None:
        self.hits = list(hits)
        self.error = error
        self.min_interval_ms = min_interval_ms
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def search(self, query, *, limit, scope):
        self.calls.append({"query": query, "limit": limit, "scope": scope})
        if self.error is not None:
            raise self.error
        return list(self.hits)

    async def aclose(self) -> None:
        self.closed = True


class BrokenRedis:
    """Every command fails: the cache, the throttle and the rate limiter must all fail open."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise RedisConnectionError("redis is down")

        return fail


def build(provider, redis=None, **overrides):
    settings = {"rate_limit_requests": 100, **overrides}
    redis = redis if redis is not None else make_redis()
    return make_app(make_settings(**settings), redis, geocoder=provider)


async def test_results_are_compact_and_scoped_to_the_municipality():
    provider = FakeProvider([MALL, OUTSIDE_BOX, FOREIGN_IN_BOX, NO_COUNTRY])
    app = build(provider)
    async with app.router.lifespan_context(app), make_client(app) as client:
        r = await client.get(URL, params={"q": "Delta"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert r.headers["X-Geocode-Status"] == "miss"
    assert "x-geocode-status" in r.headers["access-control-expose-headers"].lower()
    assert r.json() == {
        "query": "Delta",
        "results": [
            {
                "label": "Delta City",
                "address": "Cetinjski put 5, Podgorica",
                "lat": 42.431,
                "lng": 19.238,
                "kind": "poi",
            },
            {
                "label": "Blok 5",
                "address": "Podgorica",
                "lat": 42.438,
                "lng": 19.245,
                "kind": "place",
            },
        ],
    }
    # the provider was asked for the municipality, not the world
    (call,) = provider.calls
    assert call["query"] == "Delta"
    assert call["limit"] == 16  # 2 x max_results: room for the post-filter
    assert call["scope"].bbox == PODGORICA_BOX
    assert call["scope"].center == (19.2636, 42.4411)
    assert call["scope"].country_code == "ME"
    assert call["scope"].language == "sr-Latn-ME"
    assert provider.closed  # the app owns the provider's HTTP client and closes it at shutdown


async def test_cache_is_keyed_by_the_normalised_query():
    provider = FakeProvider([MALL])
    redis = make_redis()
    app = build(provider, redis)
    async with app.router.lifespan_context(app), make_client(app) as client:
        first = await client.get(URL, params={"q": "  Delta   City "})
        second = await client.get(URL, params={"q": "delta city"})
        third = await client.get(URL, params={"q": "DELTA\tCITY"})
        other = await client.get(URL, params={"q": "Delta City 2"})
        ttl = await redis.ttl("geocode:v1:fake:q:delta city")
    statuses = [r.headers["X-Geocode-Status"] for r in (first, second, third, other)]
    assert statuses == ["miss", "hit", "hit", "miss"]
    assert first.json()["query"] == "Delta City"  # whitespace collapsed, case kept
    assert first.json()["results"] == second.json()["results"] == third.json()["results"]
    assert [c["query"] for c in provider.calls] == ["Delta City", "Delta City 2"]
    assert 0 < ttl <= 600


async def test_cache_can_be_disabled():
    provider = FakeProvider([MALL])
    app = build(provider, geocoder_cache_ttl_seconds=0)
    async with app.router.lifespan_context(app), make_client(app) as client:
        statuses = [
            (await client.get(URL, params={"q": "Delta"})).headers["X-Geocode-Status"]
            for _ in range(2)
        ]
    assert statuses == ["miss", "miss"]
    assert len(provider.calls) == 2


@pytest.mark.parametrize("q", ["   ", "a", " a "])
async def test_short_queries_answer_empty_without_calling_anyone(q):
    provider = FakeProvider([MALL])
    app = build(provider)
    async with app.router.lifespan_context(app), make_client(app) as client:
        r = await client.get(URL, params={"q": q})
    assert r.status_code == 200
    assert r.json() == {"query": q.strip(), "results": []}
    assert r.headers["X-Geocode-Status"] == "too_short"
    assert provider.calls == []


@pytest.mark.parametrize(
    "params", [{}, {"q": ""}, {"q": "x" * 201}], ids=["missing", "empty", "too long"]
)
async def test_malformed_queries_are_422(params):
    provider = FakeProvider([MALL])
    app = build(provider)
    async with app.router.lifespan_context(app), make_client(app) as client:
        r = await client.get(URL, params=params)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert provider.calls == []


@pytest.mark.parametrize(
    "error",
    [
        GeocodeProviderError("nominatim: HTTP 503"),
        TimeoutError("read timed out"),
        RuntimeError("?"),
    ],
    ids=["provider error", "timeout", "unexpected"],
)
async def test_provider_failure_is_an_empty_list_not_an_error(error):
    provider = FakeProvider(error=error)
    app = build(provider)
    async with app.router.lifespan_context(app), make_client(app) as client:
        first = await client.get(URL, params={"q": "Delta"})
        second = await client.get(URL, params={"q": "Delta City"})
    for r in (first, second):
        assert r.status_code == 200, r.text
        assert r.json()["results"] == []
        assert r.headers["X-Geocode-Status"] == "provider_unavailable"
    assert first.json()["query"] == "Delta"
    # the failure is not cached as a result, but the provider is left alone during the backoff
    assert len(provider.calls) == 1


async def test_provider_is_retried_once_the_backoff_ends():
    provider = FakeProvider(error=RuntimeError("boom"))
    app = build(provider, geocoder_failure_backoff_seconds=0)
    async with app.router.lifespan_context(app), make_client(app) as client:
        for _ in range(2):
            r = await client.get(URL, params={"q": "Delta"})
            assert r.headers["X-Geocode-Status"] == "provider_unavailable"
        assert len(provider.calls) == 2
        provider.error, provider.hits = None, [MALL]
        r = await client.get(URL, params={"q": "Delta"})
    assert r.headers["X-Geocode-Status"] == "miss"
    assert [x["label"] for x in r.json()["results"]] == ["Delta City"]


async def test_duplicates_are_collapsed_and_the_list_is_capped():
    distinct = [
        GeocodeHit(f"Ulica {i}", "Podgorica", 42.40 + i / 1000, 19.20 + i / 1000, "street", "ME")
        for i in range(12)
    ]
    provider = FakeProvider([STREET, STREET, MALL, STREET, *distinct])
    app = build(provider)
    async with app.router.lifespan_context(app), make_client(app) as client:
        results = (await client.get(URL, params={"q": "Ulica"})).json()["results"]
    labels = [x["label"] for x in results]
    assert len(labels) == 8 == len(set(labels))
    assert labels[:2] == ["Bulevar Svetog Petra Cetinjskog", "Delta City"]


async def test_max_results_is_configuration():
    provider = FakeProvider([MALL, STREET, NO_COUNTRY, GeocodeHit("X", None, 42.4, 19.3, "other")])
    app = build(provider, geocoder_max_results=3)
    async with app.router.lifespan_context(app), make_client(app) as client:
        results = (await client.get(URL, params={"q": "Delta"})).json()["results"]
    assert len(results) == 3
    assert provider.calls[0]["limit"] == 6


async def test_per_ip_rate_limit_applies():
    provider = FakeProvider([MALL])
    app = make_app(make_settings(), make_redis(), geocoder=provider)  # 3 requests / 60 s
    async with app.router.lifespan_context(app), make_client(app) as client:
        for _ in range(3):
            assert (await client.get(URL, params={"q": "Delta"})).status_code == 200
        r = await client.get(URL, params={"q": "Delta"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert len(provider.calls) == 1  # the cache answered the repeats


async def test_provider_spacing_is_kept_when_no_waiting_is_allowed():
    provider = FakeProvider([MALL], min_interval_ms=300)
    app = build(provider, geocoder_throttle_wait_ms=0)
    async with app.router.lifespan_context(app), make_client(app) as client:
        first = await client.get(URL, params={"q": "Delta"})
        second = await client.get(URL, params={"q": "Blok"})
        await asyncio.sleep(0.35)
        third = await client.get(URL, params={"q": "Blok"})
    assert first.headers["X-Geocode-Status"] == "miss"
    assert second.status_code == 200 and second.json()["results"] == []
    assert second.headers["X-Geocode-Status"] == "throttled"  # and not cached as empty
    assert third.headers["X-Geocode-Status"] == "miss"
    assert [c["query"] for c in provider.calls] == ["Delta", "Blok"]


async def test_a_request_waits_briefly_for_a_provider_slot():
    provider = FakeProvider([MALL], min_interval_ms=200)
    app = build(provider, geocoder_throttle_wait_ms=1000)
    async with app.router.lifespan_context(app), make_client(app) as client:
        await client.get(URL, params={"q": "Delta"})
        started = time.monotonic()
        second = await client.get(URL, params={"q": "Blok"})
        elapsed = time.monotonic() - started
    assert second.headers["X-Geocode-Status"] == "miss"
    assert elapsed >= 0.15
    assert len(provider.calls) == 2


async def test_settings_override_the_provider_spacing():
    provider = FakeProvider([MALL], min_interval_ms=300)
    app = build(provider, geocoder_min_interval_ms=0, geocoder_throttle_wait_ms=0)
    async with app.router.lifespan_context(app), make_client(app) as client:
        statuses = [
            (await client.get(URL, params={"q": q})).headers["X-Geocode-Status"]
            for q in ("Delta", "Blok")
        ]
    assert statuses == ["miss", "miss"]


async def test_redis_outage_degrades_to_uncached_but_working_search():
    provider = FakeProvider([MALL], min_interval_ms=200)
    app = build(provider, BrokenRedis(), geocoder_throttle_wait_ms=1000)
    async with app.router.lifespan_context(app), make_client(app) as client:
        started = time.monotonic()
        first = await client.get(URL, params={"q": "Delta"})
        second = await client.get(URL, params={"q": "Delta"})
        elapsed = time.monotonic() - started
    for r in (first, second):
        assert r.status_code == 200
        assert r.headers["X-Geocode-Status"] == "miss"  # no cache, but an answer
        assert [x["label"] for x in r.json()["results"]] == ["Delta City"]
    assert len(provider.calls) == 2
    assert elapsed >= 0.15  # the in-process fallback still kept the provider's spacing
