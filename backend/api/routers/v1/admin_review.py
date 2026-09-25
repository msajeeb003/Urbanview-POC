"""Expert review queue (roles admin, reviewer, expert) and the audit trail (admin, reviewer).

- ``GET /v1/admin/review`` — paged queue of staged extracted items with everything needed to open
  the cited page and check the value; filters: document, zone, status, entity, urban parcel, page;
- ``GET /v1/admin/review/summary`` — pending / approved / amended / rejected per document and
  ``can_publish`` (no pending items and something approved);
- ``POST /v1/admin/review/{id}/approve | amend | reject`` — one decision, one audit row with the
  state before and after; amend keeps the AI value and stores the correction alongside;
- ``POST /v1/admin/review/bulk-approve`` — many pending items at once (ids, document page or
  urban parcel), one audit row per item;
- ``GET /v1/admin/audit`` — who changed what and when, filterable by entity, actor, action, time.
Nothing here writes to the serving tables; publishing is a separate job.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from api.deps import AuditReaderPrincipal, ReviewerPrincipal, ReviewServiceDep
from api.schemas.review import (
    AmendIn,
    ApproveIn,
    AuditPage,
    BulkApproveIn,
    BulkResult,
    EntityType,
    RejectIn,
    ReviewCounters,
    ReviewItem,
    ReviewPage,
    ReviewStatus,
)


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin", tags=["review"], dependencies=[Depends(_no_store)])
Id = Annotated[int, Path(gt=0)]
RESPONSES = {
    401: {"description": "Missing or unknown bearer token (`unauthorized`)"},
    403: {"description": "The principal's role may not review (`forbidden`)"},
    404: {"description": "No such item (`not_found`)"},
    409: {"description": "The item has been published; decisions are closed (`conflict`)"},
}


@router.get("/review", response_model=ReviewPage, summary="The review queue of staged items")
async def list_review_items(
    principal: ReviewerPrincipal,
    service: ReviewServiceDep,
    document_id: Annotated[int | None, Query(gt=0)] = None,
    zone_id: Annotated[int | None, Query(gt=0)] = None,
    status: Annotated[ReviewStatus | None, Query()] = None,
    entity_type: Annotated[EntityType | None, Query()] = None,
    urban_parcel_id: Annotated[int | None, Query(gt=0)] = None,
    source_page: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReviewPage:
    return await service.list_items(
        document_id=document_id,
        zone_id=zone_id,
        status=status,
        entity_type=entity_type,
        urban_parcel_id=urban_parcel_id,
        source_page=source_page,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/review/summary",
    response_model=list[ReviewCounters],
    summary="Per-document counters and whether the document can be published",
)
async def review_summary(
    principal: ReviewerPrincipal,
    service: ReviewServiceDep,
    document_id: Annotated[int | None, Query(gt=0)] = None,
) -> list[ReviewCounters]:
    return await service.counters(document_id=document_id)


@router.post(
    "/review/bulk-approve",
    response_model=BulkResult,
    summary="Approve many pending items (by ids, document page or urban parcel)",
    responses=RESPONSES,
)
async def bulk_approve(
    principal: ReviewerPrincipal, service: ReviewServiceDep, payload: BulkApproveIn
) -> BulkResult:
    return await service.bulk_approve(principal, payload)


@router.get("/review/{item_id}", response_model=ReviewItem, responses=RESPONSES)
async def get_review_item(
    principal: ReviewerPrincipal, service: ReviewServiceDep, item_id: Id
) -> ReviewItem:
    return await service.get_item(item_id)


@router.post(
    "/review/{item_id}/approve",
    response_model=ReviewItem,
    summary="Accept the AI value as extracted",
    responses=RESPONSES,
)
async def approve_item(
    principal: ReviewerPrincipal,
    service: ReviewServiceDep,
    item_id: Id,
    payload: ApproveIn | None = None,
) -> ReviewItem:
    return await service.approve(principal, item_id, payload.note if payload else None)


@router.post(
    "/review/{item_id}/amend",
    response_model=ReviewItem,
    summary="Store a corrected value alongside the AI value (which stays retrievable)",
    responses={**RESPONSES, 422: {"description": "The value does not match the parameter type"}},
)
async def amend_item(
    principal: ReviewerPrincipal, service: ReviewServiceDep, item_id: Id, payload: AmendIn
) -> ReviewItem:
    return await service.amend(principal, item_id, payload)


@router.post(
    "/review/{item_id}/reject",
    response_model=ReviewItem,
    summary="Reject the value with a reason; it will not publish",
    responses=RESPONSES,
)
async def reject_item(
    principal: ReviewerPrincipal, service: ReviewServiceDep, item_id: Id, payload: RejectIn
) -> ReviewItem:
    return await service.reject(principal, item_id, payload.note)


@router.get(
    "/audit",
    response_model=AuditPage,
    summary="Who changed what and when (append-only audit trail)",
    tags=["admin"],
)
async def list_audit(
    principal: AuditReaderPrincipal,
    service: ReviewServiceDep,
    entity_type: Annotated[str | None, Query(max_length=60)] = None,
    entity_id: Annotated[int | None, Query(gt=0)] = None,
    actor: Annotated[str | None, Query(max_length=254)] = None,
    actor_user_id: Annotated[int | None, Query(gt=0)] = None,
    action: Annotated[str | None, Query(max_length=60, description="Prefix, e.g. review.")] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditPage:
    return await service.list_audit(
        entity_type=entity_type,
        entity_id=entity_id,
        actor=actor,
        actor_user_id=actor_user_id,
        action=action,
        from_=from_,
        to=to,
        limit=limit,
        offset=offset,
    )
