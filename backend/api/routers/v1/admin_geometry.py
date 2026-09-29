"""Geometry review (roles admin and reviewer, like the value queue; experts have no access): the
staged geometry batches (the pilot scope's ``staging.geometry_draft``) with their origin and
validity QA, decided before the publish job may apply them.

- ``GET /v1/admin/geometry`` — staged and rejected batches (``include_history`` adds published and
  superseded ones), pending first, failing QA first; filters: review status, origin, layer,
  document, dataset, QA status; ``counts`` for the queue header;
- ``GET /v1/admin/geometry/{id}`` — one batch; ``.../features`` — its features for the preview
  (simplified GeoJSON, the issue codes naming each feature);
- ``POST /v1/admin/geometry/{id}/approve | reject`` — one decision, one audit row; approve is
  refused for a batch whose QA fails (409 ``qa_failed``), reject needs a reason and is final;
- ``POST /v1/admin/geometry/bulk-approve`` — the pending batches of a dataset, a document or ids.
Nothing here writes to the serving tables; publishing is a separate job.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from api.deps import GeometryReviewServiceDep, ReviewerPrincipal
from api.schemas.geometry_review import (
    GeometryApproveIn,
    GeometryBulkApproveIn,
    GeometryBulkResult,
    GeometryDraft,
    GeometryFeatures,
    GeometryOrigin,
    GeometryPage,
    GeometryRejectIn,
    GeometryReviewStatus,
    QaStatus,
)


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin/geometry", tags=["review"], dependencies=[Depends(_no_store)])
Id = Annotated[int, Path(gt=0)]
RESPONSES = {
    401: {"description": "Missing or unknown bearer token (`unauthorized`)"},
    403: {"description": "The principal's role may not review (`forbidden`)"},
    404: {"description": "No such batch (`not_found`)"},
    409: {
        "description": (
            "The batch is published, superseded or rejected, or its QA fails (`conflict`, "
            "`details.reason`)"
        )
    },
}


@router.get("", response_model=GeometryPage, summary="Staged geometry waiting for review")
async def list_geometry(
    principal: ReviewerPrincipal,
    service: GeometryReviewServiceDep,
    status: Annotated[GeometryReviewStatus | None, Query()] = None,
    origin: Annotated[GeometryOrigin | None, Query()] = None,
    layer_id: Annotated[str | None, Query(max_length=60, pattern=r"^[a-z_]+$")] = None,
    document_id: Annotated[int | None, Query(gt=0)] = None,
    dataset_version: Annotated[str | None, Query(max_length=120)] = None,
    qa_status: Annotated[QaStatus | None, Query()] = None,
    include_history: Annotated[
        bool, Query(description="Also published and superseded batches")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> GeometryPage:
    return await service.list_drafts(
        status=status,
        origin=origin,
        layer_id=layer_id,
        document_id=document_id,
        dataset_version=dataset_version,
        qa_status=qa_status,
        include_history=include_history,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/bulk-approve",
    response_model=GeometryBulkResult,
    summary="Approve the pending batches of a dataset, a document or given ids",
    responses=RESPONSES,
)
async def bulk_approve_geometry(
    principal: ReviewerPrincipal, service: GeometryReviewServiceDep, payload: GeometryBulkApproveIn
) -> GeometryBulkResult:
    return await service.bulk_approve(principal, payload)


@router.get("/{batch_id}", response_model=GeometryDraft, responses=RESPONSES)
async def get_geometry(
    principal: ReviewerPrincipal, service: GeometryReviewServiceDep, batch_id: Id
) -> GeometryDraft:
    return await service.get_draft(batch_id)


@router.get(
    "/{batch_id}/features",
    response_model=GeometryFeatures,
    summary="The batch's features for the review preview (simplified GeoJSON)",
    responses=RESPONSES,
)
async def geometry_features(
    principal: ReviewerPrincipal, service: GeometryReviewServiceDep, batch_id: Id
) -> GeometryFeatures:
    return await service.features(batch_id)


@router.post(
    "/{batch_id}/approve",
    response_model=GeometryDraft,
    summary="Approve the batch for the next publish",
    responses=RESPONSES,
)
async def approve_geometry(
    principal: ReviewerPrincipal,
    service: GeometryReviewServiceDep,
    batch_id: Id,
    payload: GeometryApproveIn | None = None,
) -> GeometryDraft:
    return await service.approve(principal, batch_id, payload.note if payload else None)


@router.post(
    "/{batch_id}/reject",
    response_model=GeometryDraft,
    summary="Reject the batch with a reason; it never publishes (stage the geometry again)",
    responses=RESPONSES,
)
async def reject_geometry(
    principal: ReviewerPrincipal,
    service: GeometryReviewServiceDep,
    batch_id: Id,
    payload: GeometryRejectIn,
) -> GeometryDraft:
    return await service.reject(principal, batch_id, payload.note)
