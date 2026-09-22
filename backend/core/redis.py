"""Redis client. Short timeouts and no client-side retries: callers (rate limiter, caches) must
fail fast and fail open rather than hold a public request for seconds."""

from __future__ import annotations

from typing import TYPE_CHECKING

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

if TYPE_CHECKING:
    from core.config import Settings


def create_redis(settings: Settings) -> Redis:
    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
        retry=Retry(NoBackoff(), 0),
    )


async def check_redis(redis: Redis) -> bool:
    return bool(await redis.ping())
