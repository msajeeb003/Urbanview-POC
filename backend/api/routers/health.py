"""Liveness and readiness. Exempt from rate limiting (see RATE_LIMIT_EXEMPT_PATHS)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.deps import get_municipality, get_settings_dep
from core.config import Settings
from core.db import check_database
from core.municipality import MunicipalityProfile
from core.redis import check_redis

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    env: str
    municipality: str


class ReadinessResponse(BaseModel):
    status: Literal["ok", "degraded"]
    checks: dict[str, Literal["ok", "error"]]


@router.get("/health", response_model=HealthResponse, summary="Liveness")
async def health(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    municipality: Annotated[MunicipalityProfile, Depends(get_municipality)],
) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        env=settings.app_env.value,
        municipality=municipality.id,
    )


async def _check(fn: Callable[[Any], Awaitable[bool]], target: Any, timeout: float = 2.0) -> str:
    try:
        ok = await asyncio.wait_for(fn(target), timeout)
        return "ok" if ok else "error"
    except Exception:
        return "error"


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "A dependency is down"}},
    summary="Readiness (database + redis)",
)
async def readiness(request: Request) -> JSONResponse:
    state = request.app.state
    database, redis = await asyncio.gather(
        _check(check_database, state.engine), _check(check_redis, state.redis)
    )
    checks = {"database": database, "redis": redis}
    ok = all(value == "ok" for value in checks.values())
    body = ReadinessResponse(status="ok" if ok else "degraded", checks=checks)
    return JSONResponse(status_code=200 if ok else 503, content=body.model_dump())
