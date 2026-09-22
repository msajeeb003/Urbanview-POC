"""Application factory.

``create_app`` wires settings, infrastructure clients, middleware, error handlers and routers.
Tests call it with injected clients (fake Redis, frozen clock) so no external service is needed.

Middleware order (outermost first): RequestContext -> CORS -> RateLimit -> routes.
Rate-limit responses therefore still carry request ids, timing and CORS headers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import health
from api.routers.v1 import router as v1_router
from api.services.panel import PanelService
from api.services.resolver import NoDataResolver, PostgisResolver
from core.config import Settings, get_settings
from core.db import check_database, create_engine, create_session_factory
from core.errors import register_exception_handlers
from core.logging import configure_logging
from core.middleware import RateLimitMiddleware, RequestContextMiddleware
from core.municipality import load_profile
from core.redis import create_redis
from core.storage import ObjectStorage

log = logging.getLogger("urbanview.app")

EXPOSED_HEADERS = [
    "X-Request-ID",
    "Server-Timing",
    "X-Response-Time",
    "Retry-After",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
]


def create_app(
    settings: Settings | None = None,
    *,
    redis_client: Any | None = None,
    rate_limit_clock: Callable[[], float] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    municipality = load_profile(settings.municipality_id)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.redis = redis_client if redis_client is not None else create_redis(settings)
        app.state.engine = create_engine(settings)
        app.state.session_factory = create_session_factory(app.state.engine)
        app.state.storage = ObjectStorage(settings)
        if settings.location_resolver == "postgis":
            app.state.resolver = PostgisResolver(
                municipality,
                app.state.session_factory,
                min_overlap_m2=settings.locate_min_overlap_m2,
                min_overlap_fraction=settings.locate_min_overlap_fraction,
            )
            # Same links, same thresholds: the panel and locate agree on which planned urban
            # parcel corresponds to a cadastral parcel.
            app.state.panel_service = PanelService(
                municipality,
                app.state.session_factory,
                min_overlap_m2=settings.locate_min_overlap_m2,
                min_overlap_fraction=settings.locate_min_overlap_fraction,
            )
            # Open the first pooled connection now so the first user request does not pay for
            # it (connection + TLS + type introspection is a few hundred ms). Best effort:
            # readiness reports a database that is down; startup must not crash on it.
            try:
                await asyncio.wait_for(check_database(app.state.engine), timeout=5)
            except Exception as exc:  # noqa: BLE001
                log.warning("database warm-up failed: %s: %s", type(exc).__name__, exc)
        log.info(
            "startup",
            extra={
                "env": settings.app_env.value,
                "municipality": municipality.id,
                "version": settings.app_version,
            },
        )
        try:
            yield
        finally:
            if redis_client is None:
                await app.state.redis.aclose()
            await app.state.engine.dispose()
            log.info("shutdown")

    app = FastAPI(
        title="UrbanView API",
        version=settings.app_version,
        description=(
            "Urban feasibility engine: planning parameters and feasibility ranges per parcel. "
            "Uncovered locations are returned as 200 with an explicit uncovered result."
        ),
        lifespan=lifespan,
        docs_url=None if settings.is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_prod else "/openapi.json",
    )

    # State available before lifespan runs (used by dependencies and tests). The PostGIS resolver
    # replaces the no-data one in lifespan once the session factory exists; the panel service
    # exists only with PostGIS (its dependency answers 503 while it is None).
    app.state.settings = settings
    app.state.municipality = municipality
    app.state.resolver = NoDataResolver(municipality)
    app.state.panel_service = None
    if redis_client is not None:
        app.state.redis = redis_client

    register_exception_handlers(app)

    # add_middleware: the last one added is the outermost.
    app.add_middleware(
        RateLimitMiddleware,
        redis_getter=lambda: getattr(app.state, "redis", None),
        limit=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
        exempt_paths=settings.rate_limit_exempt_paths,
        trust_proxy_headers=settings.trust_proxy_headers,
        enabled=settings.rate_limit_enabled,
        redis_timeout=settings.rate_limit_redis_timeout_ms / 1000,
        fail_open_seconds=settings.rate_limit_fail_open_seconds,
        **({"clock": rate_limit_clock} if rate_limit_clock else {}),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=EXPOSED_HEADERS,
    )
    app.add_middleware(RequestContextMiddleware, slow_request_ms=settings.slow_request_ms)

    app.include_router(health.router)
    app.include_router(v1_router)
    return app
