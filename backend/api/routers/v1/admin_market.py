"""Market-data imports, the market-input review queue and coverage (``core.market``).

Imports (role admin):
- ``POST /v1/admin/market/imports`` — register an uploaded table (``POST /v1/admin/files`` with
  ``kind = market_data``): official statistics (Monstat) or the client's range sheet. 202 with the
  queued ``import_market_data`` job; 200 when the same content was already normalised;
- ``POST /v1/admin/market/listings`` — asking prices pasted from a portal (scraping is deferred
  to the pilot), one listing per line: location, EUR per m², date;
- ``GET /v1/admin/market/imports[/{id}]`` — imports with their item counts; the detail adds the
  normalisation report (every row or column left out, and why) and the table as read.

Review (roles admin, reviewer, expert) — the review API for items of type ``market_input``:
- ``GET /v1/admin/review/market-inputs`` — the queue (pending first), filters status, zone,
  metric, import, flag;
- ``POST /v1/admin/review/market-inputs/{id}/approve | amend | reject`` — an approved or amended
  input writes the zone's next assumptions version (effective date, provenance); reject keeps it
  out with a reason. Every decision is audited with the state before and after.

``GET /v1/admin/market/coverage`` (admin, reviewer, expert): the sale price per m² of every zone
with its source and date, and what each zone still lacks.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from api.deps import AdminPrincipal, MarketServiceDep, ReviewerPrincipal
from api.schemas.market import (
    ImportKind,
    ImportStatus,
    ListingsImportIn,
    MarketAmendIn,
    MarketApproveIn,
    MarketCoverage,
    MarketImportAccepted,
    MarketImportIn,
    MarketImportList,
    MarketImportOut,
    MarketItemOut,
    MarketItemPage,
    MarketMetric,
    MarketRejectIn,
    MarketReviewStatus,
)


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin", tags=["market data"], dependencies=[Depends(_no_store)])
Id = Annotated[int, Path(gt=0)]
AUTH = {
    401: {"description": "Missing or unknown bearer token (`unauthorized`)"},
    403: {"description": "The principal's role may not do this (`forbidden`)"},
}
DECISION_RESPONSES = {
    **AUTH,
    404: {"description": "No such market input (`not_found`)"},
    409: {
        "description": (
            "`conflict`: the input already wrote an assumptions version (`applied`), its range "
            "is missing or inconsistent (`range_required`, `range_incomplete`, "
            "`bounds_inconsistent`: amend it), or another approved input for the zone and "
            "metric is waiting (`already_approved`)"
        )
    },
}
IMPORT_RESPONSES = {
    **AUTH,
    202: {"description": "Recorded; normalisation queued (the job's status_url)"},
    422: {"description": "Not a readable market table, wrong file kind, or bad fields"},
    503: {"description": "The stored file or the job queue is unavailable"},
}


@router.post(
    "/market/imports",
    response_model=MarketImportAccepted,
    status_code=202,
    summary="Import an uploaded statistics table or range sheet",
    responses={**IMPORT_RESPONSES, 200: {"description": "Already imported and normalised"}},
)
async def create_import(
    principal: AdminPrincipal,
    service: MarketServiceDep,
    payload: MarketImportIn,
    response: Response,
) -> MarketImportAccepted:
    accepted, queued = await service.create_import(principal, payload)
    if not queued:
        response.status_code = 200
    return accepted


@router.post(
    "/market/listings",
    response_model=MarketImportAccepted,
    status_code=202,
    summary="Import asking prices pasted from a listings portal",
    responses={**IMPORT_RESPONSES, 200: {"description": "Already imported and normalised"}},
)
async def create_listings(
    principal: AdminPrincipal,
    service: MarketServiceDep,
    payload: ListingsImportIn,
    response: Response,
) -> MarketImportAccepted:
    accepted, queued = await service.create_listings(principal, payload)
    if not queued:
        response.status_code = 200
    return accepted


@router.get("/market/imports", response_model=MarketImportList, responses=AUTH)
async def list_imports(
    principal: ReviewerPrincipal,
    service: MarketServiceDep,
    kind: Annotated[ImportKind | None, Query()] = None,
    status: Annotated[ImportStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MarketImportList:
    return await service.list_imports(kind=kind, status=status, limit=limit, offset=offset)


@router.get(
    "/market/imports/{import_id}",
    response_model=MarketImportOut,
    responses={**AUTH, 404: {"description": "No such import (`not_found`)"}},
)
async def get_import(
    principal: ReviewerPrincipal, service: MarketServiceDep, import_id: Id
) -> MarketImportOut:
    return await service.get_import(import_id, detail=True)


@router.get(
    "/market/coverage",
    response_model=MarketCoverage,
    summary="Per zone: the sale price per m² with source and date, and what is missing",
    responses=AUTH,
)
async def coverage(principal: ReviewerPrincipal, service: MarketServiceDep) -> MarketCoverage:
    return await service.coverage()


@router.get(
    "/review/market-inputs",
    response_model=MarketItemPage,
    summary="The review queue of market inputs (pending first)",
    tags=["review"],
    responses=AUTH,
)
async def list_market_inputs(
    principal: ReviewerPrincipal,
    service: MarketServiceDep,
    status: Annotated[MarketReviewStatus | None, Query()] = None,
    zone_id: Annotated[int | None, Query(gt=0)] = None,
    metric: Annotated[MarketMetric | None, Query()] = None,
    import_id: Annotated[int | None, Query(gt=0)] = None,
    flag: Annotated[
        str | None,
        Query(max_length=60, pattern=r"^[a-z_]+$", description="e.g. zone_mapped_by_ai"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MarketItemPage:
    return await service.list_items(
        status=status,
        zone_id=zone_id,
        metric=metric,
        import_id=import_id,
        flag=flag,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/review/market-inputs/{item_id}",
    response_model=MarketItemOut,
    tags=["review"],
    responses={**AUTH, 404: {"description": "No such market input (`not_found`)"}},
)
async def get_market_input(
    principal: ReviewerPrincipal, service: MarketServiceDep, item_id: Id
) -> MarketItemOut:
    return await service.get_item(item_id)


@router.post(
    "/review/market-inputs/{item_id}/approve",
    response_model=MarketItemOut,
    summary="Accept the imported figure: writes the zone's next assumptions version",
    tags=["review"],
    responses=DECISION_RESPONSES,
)
async def approve_market_input(
    principal: ReviewerPrincipal,
    service: MarketServiceDep,
    item_id: Id,
    payload: MarketApproveIn | None = None,
) -> MarketItemOut:
    return await service.approve(principal, item_id, payload or MarketApproveIn())


@router.post(
    "/review/market-inputs/{item_id}/amend",
    response_model=MarketItemOut,
    summary="Correct the figure (the imported one stays) and apply the correction",
    tags=["review"],
    responses=DECISION_RESPONSES,
)
async def amend_market_input(
    principal: ReviewerPrincipal, service: MarketServiceDep, item_id: Id, payload: MarketAmendIn
) -> MarketItemOut:
    return await service.amend(principal, item_id, payload)


@router.post(
    "/review/market-inputs/{item_id}/reject",
    response_model=MarketItemOut,
    summary="Reject the figure with a reason; it never reaches the panel",
    tags=["review"],
    responses=DECISION_RESPONSES,
)
async def reject_market_input(
    principal: ReviewerPrincipal, service: MarketServiceDep, item_id: Id, payload: MarketRejectIn
) -> MarketItemOut:
    return await service.reject(principal, item_id, payload.note)
