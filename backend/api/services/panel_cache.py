"""Response cache of the display-shaped panels: one entry per entity per data state.

Key ``panel:{namespace}:{municipality}:{kind}:{id}:{version_id}:{token}``: ``version_id`` is the
current publish version, ``token`` a short hash of its creation time and of everything that changes
a panel outside a publish (documents' status / coverage switch / versions / files, current market
assumptions, current zone parameter sets), both read by one cheap statement per request
(``STAMP_SQL``). A publish, a rollback or an admin change therefore produces new keys: nothing is
ever invalidated by hand and nothing stale is served; old entries age out
(``PANEL_CACHE_TTL_SECONDS``).
The stamp is read before the data, so an entry can only be newer than its key, never older.

The cached value is the exact JSON body, so a hit costs the stamp query and one Redis GET. The
same key gives a strong ``ETag``; ``If-None-Match`` answers 304 without touching Redis. Redis
trouble never fails a request: the cache is skipped for a few seconds and the panel is computed
(``X-Panel-Cache: bypass``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.services.parcel_panel_sql import STAMP_SQL

log = logging.getLogger("urbanview.panel.cache")

CacheStatus = str  # hit | miss | bypass | revalidated


@dataclass(frozen=True, slots=True)
class Stamp:
    version_id: int | None
    token: str


@dataclass(frozen=True, slots=True)
class PanelView:
    """What the router sends: the JSON body (``None`` for a 304), its ETag and the cache path."""

    body: bytes | None
    etag: str
    cache: CacheStatus


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        value = candidate.strip()
        if value == "*":
            return True
        if value.startswith("W/"):
            value = value[2:]
        if value == etag:
            return True
    return False


class PanelCache:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_getter: Callable[[], Any],
        *,
        municipality_id: str,
        ttl_seconds: int,
        namespace: str,
        redis_timeout: float = 0.25,
        fail_open_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_factory = session_factory
        self.redis_getter = redis_getter
        self.municipality_id = municipality_id
        self.ttl_seconds = int(ttl_seconds)
        self.namespace = namespace
        self.redis_timeout = float(redis_timeout)
        self.fail_open_seconds = float(fail_open_seconds)
        self.clock = clock
        self._skip_until = 0.0

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    async def stamp(self) -> Stamp:
        async with self.session_factory() as session:
            row = (
                (await session.execute(text(STAMP_SQL), {"municipality_id": self.municipality_id}))
                .mappings()
                .one()
            )
        parts = "|".join(str(row[k]) for k in ("version_created", "documents", "market", "typical"))
        token = hashlib.sha1(parts.encode("utf-8")).hexdigest()[:16]
        version_id = row["version_id"]
        return Stamp(version_id=int(version_id) if version_id is not None else None, token=token)

    def key(self, kind: str, entity_id: int, stamp: Stamp) -> str:
        version = stamp.version_id if stamp.version_id is not None else "none"
        return (
            f"panel:{self.namespace}:{self.municipality_id}:{kind}:{entity_id}:{version}:"
            f"{stamp.token}"
        )

    @staticmethod
    def etag(key: str) -> str:
        return '"' + hashlib.sha1(key.encode("utf-8")).hexdigest()[:24] + '"'

    async def _redis(self, operation: Callable[[Any], Awaitable[Any]]) -> tuple[bool, Any]:
        """(ok, value); ok is False when Redis is unavailable (then skipped for a while)."""
        now = self.clock()
        if now < self._skip_until:
            return False, None
        redis = self.redis_getter()
        if redis is None:
            return False, None
        try:
            return True, await asyncio.wait_for(operation(redis), timeout=self.redis_timeout)
        except Exception as exc:  # noqa: BLE001 - connection errors, timeouts, protocol errors
            self._skip_until = now + self.fail_open_seconds
            log.warning(
                "panel cache unavailable (%s: %s); computing panels for %.0fs",
                type(exc).__name__,
                exc,
                self.fail_open_seconds,
            )
            return False, None

    async def get(self, key: str) -> bytes | None:
        if not self.enabled:
            return None
        ok, value = await self._redis(lambda r: r.get(key))
        if not ok or value is None:
            return None
        return value if isinstance(value, bytes) else str(value).encode("utf-8")

    async def set(self, key: str, body: bytes) -> bool:
        if not self.enabled:
            return False
        payload = body.decode("utf-8")
        ok, _ = await self._redis(lambda r: r.set(key, payload, ex=self.ttl_seconds))
        return ok

    async def serve(
        self,
        kind: str,
        entity_id: int,
        build: Callable[[], Awaitable[BaseModel]],
        if_none_match: str | None = None,
    ) -> PanelView:
        """304 when the client already holds this state, else the cached body, else build it."""
        stamp = await self.stamp()
        key = self.key(kind, entity_id, stamp)
        etag = self.etag(key)
        if etag_matches(if_none_match, etag):
            return PanelView(body=None, etag=etag, cache="revalidated")
        cached = await self.get(key)
        if cached is not None:
            return PanelView(body=cached, etag=etag, cache="hit")
        model = await build()  # raises for a missing entity: nothing is cached then
        body = model.model_dump_json().encode("utf-8")
        stored = await self.set(key, body)
        return PanelView(body=body, etag=etag, cache="miss" if stored else "bypass")
