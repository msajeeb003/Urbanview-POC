"""Per-client-IP rate limiting, Redis-backed, fixed window (pure ASGI).

Each client IP gets ``limit`` requests per ``window_seconds``. Counters live in Redis under
``ratelimit:{ip}:{window_start}`` and expire with the window, so the limiter is shared by all API
replicas and needs no cleanup. Over the limit, the client receives HTTP 429 in the standard error
envelope with ``Retry-After`` (seconds until the window resets) and ``X-RateLimit-*`` headers.

If Redis is slow or unreachable the limiter *fails open*: each Redis call has a hard time budget
(``redis_timeout``), and after a failure the limiter skips Redis entirely for ``fail_open_seconds``
(a small circuit breaker) so an outage costs one warning per cooldown, not seconds per request.
Availability of the public map (under two seconds to a populated panel) matters more than strict
throttling.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.errors import error_response

log = logging.getLogger("urbanview.ratelimit")

KEY_PREFIX = "ratelimit"


class RateLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        redis_getter: Callable[[], Any],
        limit: int,
        window_seconds: int,
        exempt_paths: Iterable[str] = (),
        trust_proxy_headers: bool = False,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
        redis_timeout: float = 0.25,
        fail_open_seconds: float = 5.0,
    ) -> None:
        self.app = app
        self.redis_getter = redis_getter
        self.limit = int(limit)
        self.window_seconds = int(window_seconds)
        self.exempt_paths = tuple(p.rstrip("/") or "/" for p in exempt_paths)
        self.trust_proxy_headers = trust_proxy_headers
        self.enabled = enabled
        self.clock = clock
        self.redis_timeout = float(redis_timeout)
        self.fail_open_seconds = float(fail_open_seconds)
        self._fail_open_until: float = 0.0

    def is_exempt(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in self.exempt_paths)

    def client_ip(self, scope: Scope) -> str:
        if self.trust_proxy_headers:
            forwarded = Headers(scope=scope).get("x-forwarded-for")
            if forwarded:
                return forwarded.split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    async def _increment(self, redis: Any, key: str) -> int:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, self.window_seconds + 1)
            count, _ = await pipe.execute()
        return int(count)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled or self.is_exempt(scope["path"]):
            await self.app(scope, receive, send)
            return

        now_exact = self.clock()
        if now_exact < self._fail_open_until:
            # Circuit open after a recent Redis failure: skip the limiter until the cooldown ends.
            await self.app(scope, receive, send)
            return

        redis = self.redis_getter()
        if redis is None:
            log.warning("rate limiter has no redis client; failing open")
            await self.app(scope, receive, send)
            return

        ip = self.client_ip(scope)
        now = int(now_exact)
        window_start = now - (now % self.window_seconds)
        reset_at = window_start + self.window_seconds
        key = f"{KEY_PREFIX}:{ip}:{window_start}"

        try:
            count = await asyncio.wait_for(self._increment(redis, key), timeout=self.redis_timeout)
        except Exception as exc:  # connection errors, timeouts, protocol errors: fail open
            self._fail_open_until = now_exact + self.fail_open_seconds
            log.warning(
                "rate limiter unavailable (%s: %s); failing open for %.0fs",
                type(exc).__name__,
                exc,
                self.fail_open_seconds,
                extra={"fail_open_seconds": self.fail_open_seconds},
            )
            await self.app(scope, receive, send)
            return

        remaining = max(0, self.limit - count)
        limit_headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_at),
        }

        if count > self.limit:
            retry_after = max(1, reset_at - now)
            log.info(
                "rate limit exceeded",
                extra={"client_ip": ip, "count": count, "limit": self.limit},
            )
            response = error_response(
                429,
                "rate_limited",
                f"Too many requests. Retry in {retry_after} seconds.",
                details={"limit": self.limit, "window_seconds": self.window_seconds},
                headers={"Retry-After": str(retry_after), **limit_headers},
            )
            await response(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in limit_headers.items():
                    headers.append(name, value)
            await send(message)

        await self.app(scope, receive, send_wrapper)
