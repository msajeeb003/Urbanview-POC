"""Request id + timing middleware (pure ASGI, outermost in the stack).

- Honours a safe incoming ``X-Request-ID`` so ids trace across services; otherwise mints one.
- Adds ``X-Request-ID``, ``Server-Timing`` and ``X-Response-Time`` to every response.
- Logs one access line per request with method, route template, status and latency; WARNING when
  slower than ``slow_request_ms`` (product target: under two seconds from query to populated panel).
- The public map sends its anonymous analytics session id (``X-Session-ID``, the id its events
  carry; random, never personal) on every call: a well-formed one goes into the access line and
  ``scope["state"]["session_id"]``, anything else is ignored.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.context import request_id_var

log = logging.getLogger("urbanview.http")

REQUEST_ID_HEADER = "X-Request-ID"
SESSION_ID_HEADER = "X-Session-ID"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")  # the analytics id format (``POST /v1/events``)


def route_template(scope: Scope) -> str:
    """Full route template for the matched route (``/v1/locations/resolve``), else the raw path.

    FastAPI keeps included routers as a tree, so the matched route only knows its own sub-path
    (``/locations/resolve``) and not the ``/v1`` prefix. The prefix is recovered by matching the
    route's own regex against the tail of the request path, which also keeps path parameters
    in the template form (``/parcels/{parcel_id}``) for the matched part.
    """
    path: str = scope.get("path", "")
    route = scope.get("route")
    regex = getattr(route, "path_regex", None)
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if regex is None or not template:
        return path
    for i in range(len(path) + 1):
        if (i == 0 or path[i] == "/") and regex.match(path[i:]):
            return path[:i].rstrip("/") + template
    return path


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, slow_request_ms: int = 2000) -> None:
        self.app = app
        self.slow_request_ms = slow_request_ms

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers_in = Headers(scope=scope)
        incoming = headers_in.get(REQUEST_ID_HEADER)
        request_id = incoming if incoming and _SAFE_ID.match(incoming) else uuid.uuid4().hex
        session = headers_in.get(SESSION_ID_HEADER)
        session_id = session if session and _SESSION_ID.match(session) else None
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["session_id"] = session_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code: int | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                elapsed_ms = (time.perf_counter() - started) * 1000
                headers = MutableHeaders(scope=message)
                headers.append(REQUEST_ID_HEADER, request_id)
                headers.append("Server-Timing", f"app;dur={elapsed_ms:.1f}")
                headers.append("X-Response-Time", f"{elapsed_ms:.1f}ms")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status_code = 500
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            route_path = route_template(scope)
            slow = elapsed_ms > self.slow_request_ms
            log.log(
                logging.WARNING if slow else logging.INFO,
                "%s %s -> %s in %.1fms",
                scope.get("method"),
                route_path,
                status_code,
                elapsed_ms,
                extra={
                    "method": scope.get("method"),
                    "route": route_path,
                    "path": scope.get("path"),
                    "status": status_code,
                    "duration_ms": round(elapsed_ms, 1),
                    "slow": slow,
                    "session_id": session_id,
                },
            )
            request_id_var.reset(token)
