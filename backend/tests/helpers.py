"""Shared test helpers: settings factory, app factory with fakes, client factory."""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from core.config import AppEnv, Settings

# Frozen clock for the rate limiter so window boundaries never move during a test.
FIXED_NOW = 1_800_000_000.0


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        app_env=AppEnv.dev,
        rate_limit_requests=3,
        rate_limit_window_seconds=60,
        cors_origins=["http://testserver"],
        log_level="INFO",
        location_resolver="nodata",  # unit tests run without a database
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def make_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def make_app(
    settings: Settings | None = None,
    redis: Any | None = None,
    *,
    geocoder: Any | None = None,
    storage: Any | None = None,
    source_repository: Any | None = None,
    analytics_repository: Any | None = None,
    admin_dispatcher: Any | None = None,
    staff_authenticator: Any | None = None,
) -> FastAPI:
    return create_app(
        settings or make_settings(),
        redis_client=redis if redis is not None else make_redis(),
        rate_limit_clock=lambda: FIXED_NOW,
        geocoder=geocoder,
        storage=storage,
        source_repository=source_repository,
        analytics_repository=analytics_repository,
        admin_dispatcher=admin_dispatcher,
        staff_authenticator=staff_authenticator,
    )


def make_client(app: FastAPI, client_ip: str = "127.0.0.1") -> AsyncClient:
    transport = ASGITransport(app=app, raise_app_exceptions=False, client=(client_ip, 12345))
    return AsyncClient(transport=transport, base_url="http://testserver")
