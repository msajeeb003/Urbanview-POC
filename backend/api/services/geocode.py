"""Autocomplete geocoding behind ``GET /v1/geocode``: municipality scope, cache, provider policy.

For each query, in order:

1. normalise (collapse whitespace); shorter than ``min_query_length`` answers empty at once;
2. Redis cache keyed by the case-folded query (``geocode:v1:{provider}:q:{query}``);
3. while the provider's failure backoff key is set, answer empty without calling it (a down
   provider costs one call per ``failure_backoff_seconds`` across all replicas, not one per
   keystroke);
4. keep the provider's minimum call spacing (its usage policy) with a Redis ``SET NX PX`` slot
   shared by all replicas, an in-process fallback when Redis is down, and at most
   ``throttle_wait_ms`` of waiting for a slot;
5. call the provider with the municipality scope (bounding box, country, bias point, language);
6. keep hits inside the box and country, drop duplicates, cap at ``max_results``, cache.

Product rule: search never dead-ends. A failing provider, a missed slot or a Redis outage all
degrade to 200 with an empty list; ``X-Geocode-Status`` says why. Only malformed input is a 422.
Selecting a result is the client's next call (``GET /v1/locate?lat=&lng=``): nothing here
touches the planning database, and no AI is involved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from api.schemas.geocode import GeocodeResponse, GeocodeResult
from core.geocode.base import GeocodeHit, GeocodeProvider, SearchScope

log = logging.getLogger("urbanview.geocode")

CACHE_VERSION = "v1"
COORDINATE_DECIMALS = 6  # ~0.1 m: enough for a map pin, keeps the payload and cache compact


class GeocodeStatus(StrEnum):
    hit = "hit"  # served from the cache
    miss = "miss"  # served by the provider (and cached)
    too_short = "too_short"  # query below the minimum length; nobody was called
    throttled = "throttled"  # no provider slot within throttle_wait_ms
    provider_unavailable = "provider_unavailable"  # provider failed, or is in failure backoff


@dataclass(frozen=True, slots=True)
class GeocodeOutcome:
    response: GeocodeResponse
    status: GeocodeStatus


def normalise_query(raw: str) -> str:
    """Collapse runs of whitespace (including newlines and tabs) and trim; case is kept."""
    return " ".join(raw.split())


class GeocodeService:
    def __init__(
        self,
        provider: GeocodeProvider,
        *,
        redis_getter: Callable[[], Any],
        scope: SearchScope,
        max_results: int = 8,
        min_query_length: int = 2,
        cache_ttl_seconds: int = 600,
        min_interval_ms: int | None = None,
        throttle_wait_ms: int = 400,
        failure_backoff_seconds: int = 5,
        redis_timeout: float = 0.25,
        redis_fail_open_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.provider = provider
        self.redis_getter = redis_getter
        self.scope = scope
        self.max_results = int(max_results)
        self.min_query_length = int(min_query_length)
        self.cache_ttl_seconds = int(cache_ttl_seconds)
        # Explicit configuration wins; otherwise the provider states its own policy.
        self.min_interval_ms = (
            provider.min_interval_ms if min_interval_ms is None else int(min_interval_ms)
        )
        self.throttle_wait_ms = int(throttle_wait_ms)
        self.failure_backoff_seconds = int(failure_backoff_seconds)
        self.redis_timeout = float(redis_timeout)
        self.redis_fail_open_seconds = float(redis_fail_open_seconds)
        self.clock = clock
        self.sleep = sleep
        # Ask for more than we return: the box / country filter and de-duplication thin the list.
        self.fetch_limit = min(2 * self.max_results, 20)

        prefix = f"geocode:{CACHE_VERSION}:{provider.name}"
        self._cache_prefix = prefix + ":q:"
        self._backoff_key = prefix + ":backoff"
        self._throttle_key = prefix + ":throttle"
        self._redis_fail_open_until = 0.0
        self._local_lock = asyncio.Lock()
        self._local_next_slot = 0.0

    # --- public -----------------------------------------------------------------------------

    async def search(self, raw_query: str) -> GeocodeOutcome:
        query = normalise_query(raw_query)
        if len(query) < self.min_query_length:
            return self._outcome(query, [], GeocodeStatus.too_short)

        cache_key = self._cache_prefix + query.casefold()
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return self._outcome(query, cached, GeocodeStatus.hit)

        if await self._in_backoff():
            return self._outcome(query, [], GeocodeStatus.provider_unavailable)
        if not await self._acquire_slot():
            log.info("geocode throttled", extra={"provider": self.provider.name})
            return self._outcome(query, [], GeocodeStatus.throttled)

        try:
            hits = await self.provider.search(query, limit=self.fetch_limit, scope=self.scope)
        except Exception as exc:  # noqa: BLE001 - any provider failure degrades to an empty list
            log.warning(
                "geocode provider failed (%s: %s); answering empty for %ss",
                type(exc).__name__,
                exc,
                self.failure_backoff_seconds,
                extra={"provider": self.provider.name, "error_type": type(exc).__name__},
            )
            await self._start_backoff()
            return self._outcome(query, [], GeocodeStatus.provider_unavailable)

        results = self._select(hits)
        await self._cache_set(cache_key, results)
        return self._outcome(query, results, GeocodeStatus.miss)

    # --- selection ---------------------------------------------------------------------------

    def _select(self, hits: list[GeocodeHit]) -> list[GeocodeResult]:
        """Municipality scope (box + country), de-duplication, cap: whatever the provider did."""
        results: list[GeocodeResult] = []
        seen: set[tuple[str, str, str]] = set()
        for hit in hits:
            if not self.scope.bbox.contains(hit.lng, hit.lat):
                continue
            if hit.country_code and hit.country_code != self.scope.country_code.upper():
                continue
            key = (hit.kind, hit.label.casefold(), (hit.address or "").casefold())
            if key in seen:
                continue
            seen.add(key)
            results.append(
                GeocodeResult(
                    label=hit.label,
                    address=hit.address,
                    lat=round(hit.lat, COORDINATE_DECIMALS),
                    lng=round(hit.lng, COORDINATE_DECIMALS),
                    kind=hit.kind,
                )
            )
            if len(results) >= self.max_results:
                break
        return results

    @staticmethod
    def _outcome(query: str, results: list[GeocodeResult], status: GeocodeStatus) -> GeocodeOutcome:
        return GeocodeOutcome(GeocodeResponse(query=query, results=results), status)

    # --- redis: cache, backoff, throttle (all fail open) ---------------------------------------

    async def _redis_call(self, operation: Callable[[Any], Awaitable[Any]]) -> Any | None:
        """Run one Redis operation under a hard time budget; ``None`` means "Redis unavailable".

        After a failure Redis is skipped for ``redis_fail_open_seconds`` (the same small circuit
        breaker as the rate limiter) so an outage costs one warning per cooldown.
        """
        now = self.clock()
        if now < self._redis_fail_open_until:
            return None
        redis = self.redis_getter()
        if redis is None:
            return None
        try:
            return await asyncio.wait_for(operation(redis), timeout=self.redis_timeout)
        except Exception as exc:  # noqa: BLE001 - connection errors, timeouts, protocol errors
            self._redis_fail_open_until = now + self.redis_fail_open_seconds
            log.warning(
                "geocode redis unavailable (%s: %s); skipping cache and throttle for %.0fs",
                type(exc).__name__,
                exc,
                self.redis_fail_open_seconds,
            )
            return None

    async def _cache_get(self, key: str) -> list[GeocodeResult] | None:
        if self.cache_ttl_seconds <= 0:
            return None
        raw = await self._redis_call(lambda r: r.get(key))
        if not raw:
            return None
        try:
            return [GeocodeResult.model_validate(item) for item in json.loads(raw)]
        except (ValueError, TypeError):
            return None  # a corrupt entry is a miss; it is overwritten below

    async def _cache_set(self, key: str, results: list[GeocodeResult]) -> None:
        if self.cache_ttl_seconds <= 0:
            return
        payload = json.dumps([r.model_dump() for r in results], separators=(",", ":"))
        await self._redis_call(lambda r: r.set(key, payload, ex=self.cache_ttl_seconds))

    async def _in_backoff(self) -> bool:
        if self.failure_backoff_seconds <= 0:
            return False
        return bool(await self._redis_call(lambda r: r.exists(self._backoff_key)))

    async def _start_backoff(self) -> None:
        if self.failure_backoff_seconds <= 0:
            return
        await self._redis_call(
            lambda r: r.set(self._backoff_key, "1", ex=self.failure_backoff_seconds)
        )

    async def _acquire_slot(self) -> bool:
        """Keep the provider's minimum call spacing; wait at most ``throttle_wait_ms``."""
        interval_ms = self.min_interval_ms
        if interval_ms <= 0:
            return True
        deadline = self.clock() + self.throttle_wait_ms / 1000

        async def try_redis(redis: Any) -> bool:
            return bool(await redis.set(self._throttle_key, "1", nx=True, px=interval_ms))

        while True:
            acquired = await self._redis_call(try_redis)
            if acquired is None:  # Redis unavailable: keep the policy per process at least
                return await self._acquire_local_slot(interval_ms, deadline)
            if acquired:
                return True
            now = self.clock()
            if now >= deadline:
                return False
            ttl_ms = await self._redis_call(lambda r: r.pttl(self._throttle_key))
            wait = (ttl_ms if isinstance(ttl_ms, int) and ttl_ms > 0 else 50) / 1000
            await self.sleep(max(0.0, min(wait, deadline - now)))

    async def _acquire_local_slot(self, interval_ms: int, deadline: float) -> bool:
        async with self._local_lock:
            now = self.clock()
            wait = self._local_next_slot - now
            if wait > 0:
                if now + wait > deadline:
                    return False
                await self.sleep(wait)
            self._local_next_slot = self.clock() + interval_ms / 1000
            return True
