"""Job status API (role ``admin``): every background job (``jobs/``) is a ``pipeline_jobs`` row.

- ``GET /v1/admin/jobs``: newest first; filters ``type``, ``status``, ``target=document:12``
  (also ``file:``, ``publish_run:``, ``email:``), ``document_id``, ``file_id``; paging;
- ``GET /v1/admin/jobs/costs``: LLM tokens, estimated cost and wall time summed per target;
- ``GET /v1/admin/jobs/{id}``: the status URL every enqueue response carries;
- ``POST /v1/admin/jobs/{id}/retry``: re-queue a failed / cancelled job (audited); 409 while a
  job for the same target is active or the job is not in a retryable state.

Responses are ``Cache-Control: no-store``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from api.deps import AdminPrincipal, JobServiceDep
from api.schemas.admin import (
    TARGET_PATTERN,
    JobCostSummary,
    JobList,
    JobOut,
    JobStatus,
    JobType,
)


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin/jobs", tags=["admin"], dependencies=[Depends(_no_store)])

Id = Annotated[int, Path(gt=0)]
Target = Annotated[
    str | None,
    Query(pattern=TARGET_PATTERN, description="`<target_type>:<id>`, e.g. `document:12`"),
]
RESPONSES = {
    401: {"description": "Missing or invalid bearer token"},
    403: {"description": "Role not allowed"},
    404: {"description": "No such job"},
}


@router.get("", response_model=JobList, summary="List jobs", responses=RESPONSES)
async def list_jobs(
    principal: AdminPrincipal,
    service: JobServiceDep,
    type: Annotated[JobType | None, Query()] = None,
    status: Annotated[JobStatus | None, Query()] = None,
    target: Target = None,
    document_id: Annotated[int | None, Query(gt=0)] = None,
    file_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobList:
    return await service.list_jobs(
        type=type,
        status=status,
        target=target,
        document_id=document_id,
        file_id=file_id,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/costs", response_model=JobCostSummary, summary="Job cost per target", responses=RESPONSES
)
async def job_costs(
    principal: AdminPrincipal,
    service: JobServiceDep,
    target: Target = None,
    type: Annotated[JobType | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> JobCostSummary:
    return await service.costs(target=target, type=type, limit=limit)


@router.get("/{job_id}", response_model=JobOut, summary="Job status", responses=RESPONSES)
async def get_job(principal: AdminPrincipal, service: JobServiceDep, job_id: Id) -> JobOut:
    return await service.get_job(job_id)


@router.post(
    "/{job_id}/retry",
    response_model=JobOut,
    status_code=202,
    summary="Re-queue a failed job",
    responses={**RESPONSES, 409: {"description": "Not retryable, or the target is already active"}},
)
async def retry_job(principal: AdminPrincipal, service: JobServiceDep, job_id: Id) -> JobOut:
    return await service.retry(principal, job_id)
