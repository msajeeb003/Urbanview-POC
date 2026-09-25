"""The publish button (roles ``admin`` and ``reviewer``).

- ``POST /v1/admin/publish``: refuses (409, ``reason = pending_review``) while any document has
  items pending review, naming the documents; otherwise queues the ``publish_approved`` job
  (202 with the job; 200 with the running one when a publish is already active);
- ``GET /v1/admin/publish``: the status screen: current version (who, when, label, archive
  link), earlier versions, the active job with its per-step progress, the last job, blockers;
- ``POST /v1/admin/publish/rollback``: flips the current pointer back to an earlier version
  (default the previous one) without recomputing anything; audited.

Responses are ``Cache-Control: no-store``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from api.deps import PublisherPrincipal, PublishServiceDep
from api.schemas.admin import JobOut
from api.schemas.publish import PublishRequest, PublishStatus, RollbackRequest


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin/publish", tags=["admin"], dependencies=[Depends(_no_store)])

RESPONSES = {
    401: {"description": "Missing or invalid bearer token"},
    403: {"description": "Role not allowed"},
}


@router.post(
    "",
    response_model=JobOut,
    status_code=202,
    summary="Publish everything approved",
    responses={
        **RESPONSES,
        200: {"description": "A publish job is already queued or running; returned as is"},
        409: {"description": "Items pending review (details.documents names them)"},
        503: {"description": "The job queue is unavailable"},
    },
)
async def publish(
    principal: PublisherPrincipal,
    service: PublishServiceDep,
    response: Response,
    payload: PublishRequest | None = None,
) -> JobOut:
    payload = payload or PublishRequest()
    enqueued = await service.enqueue(principal, label=payload.label, notes=payload.notes)
    response.status_code = 202 if enqueued.created else 200
    return enqueued.job


@router.get("", response_model=PublishStatus, summary="Publish status", responses=RESPONSES)
async def publish_status(
    principal: PublisherPrincipal, service: PublishServiceDep
) -> PublishStatus:
    return await service.status()


@router.post(
    "/rollback",
    response_model=PublishStatus,
    summary="Roll the map back to an earlier version",
    responses={
        **RESPONSES,
        404: {"description": "No such version"},
        409: {"description": "Nothing to roll back to, already current, or pruned"},
    },
)
async def rollback(
    principal: PublisherPrincipal,
    service: PublishServiceDep,
    payload: RollbackRequest | None = None,
) -> PublishStatus:
    payload = payload or RollbackRequest()
    return await service.rollback(principal, payload.version_id)
