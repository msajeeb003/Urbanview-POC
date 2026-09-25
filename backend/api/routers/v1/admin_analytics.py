"""Client dashboard aggregates: ``GET /v1/admin/analytics?from=&to=`` (role ``admin``).

Funnel conversion per step, orders and revenue, most searched districts, repeat usage against the
prototype target, paywall / AI interest counts and the share of panel views that reach the
financials, all for one ``[from, to)`` range (default: the last 30 days, at most 366 days).
Definitions live in ``api.services.analytics``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response

from api.deps import AdminPrincipal, AnalyticsServiceDep
from api.schemas.analytics import AnalyticsDashboard

router = APIRouter(prefix="/admin/analytics", tags=["admin"])


@router.get(
    "",
    response_model=AnalyticsDashboard,
    summary="Analytics dashboard aggregates for a date range (admin role)",
    responses={
        401: {"description": "Missing or unknown bearer token (`unauthorized`)"},
        403: {"description": "The token's role may not read analytics (`forbidden`)"},
        422: {"description": "`from` not before `to`, or a range over 366 days"},
        503: {"description": "The events database is unavailable (`service_unavailable`)"},
    },
)
async def analytics_dashboard(
    principal: AdminPrincipal,
    service: AnalyticsServiceDep,
    response: Response,
    from_: Annotated[
        datetime | None,
        Query(alias="from", description="Inclusive start (ISO date or datetime, UTC if naive)"),
    ] = None,
    to: Annotated[
        datetime | None,
        Query(description="Exclusive end (ISO date or datetime, UTC if naive); default now"),
    ] = None,
) -> AnalyticsDashboard:
    response.headers["Cache-Control"] = "no-store"
    return await service.dashboard(from_, to)
