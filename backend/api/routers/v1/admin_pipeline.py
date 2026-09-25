"""Staff pipeline API (role ``admin``): files, planning document versions, jobs, coverage.

- ``POST /v1/admin/files`` (multipart ``file`` + ``kind``): upload to the private bucket,
  de-duplicated by SHA-256 (201 new, 200 when the checksum was already known);
- ``POST /v1/admin/documents``: register a document version against a stored PDF;
  ``replaces_document_id`` creates the next version and retires the current one;
- ``GET /v1/admin/files`` / ``/documents`` (+ ``/{id}``): listings with job history;
- ``POST /v1/admin/documents/{id}/jobs/extract``, ``POST /v1/admin/files/{id}/jobs/geo``: queue
  the extraction / geometry job (staging only): 202 with the job, or 200 with the existing job
  when an identical one is already queued / running (idempotent); status, listing and retry
  live in ``admin_jobs`` (``GET /v1/admin/jobs/{id}`` is the status URL);
- ``PATCH /v1/admin/documents/{id}/coverage``: mark a document's coverage area live or not.

Every action lands in ``audit_log``. Responses are ``Cache-Control: no-store``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Path, Query, Response, UploadFile

from api.deps import AdminPrincipal, AdminServiceDep
from api.schemas.admin import (
    CoverageIn,
    DocumentIn,
    DocumentList,
    DocumentOut,
    DocumentStatus,
    FileKind,
    FileList,
    JobOut,
    StoredFileOut,
    UploadResult,
)


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(_no_store)])

RESPONSES = {
    401: {"description": "Missing or unknown bearer token (`unauthorized`)"},
    403: {"description": "The principal's role is not admin (`forbidden`)"},
    503: {"description": "Database, object storage or job queue unavailable"},
}
Id = Annotated[int, Path(gt=0)]


# --- files ----------------------------------------------------------------------------------------


@router.post(
    "/files",
    status_code=201,
    response_model=UploadResult,
    summary="Upload a planning document PDF, GIS file or cadastral extract (de-duplicated)",
    responses={
        **RESPONSES,
        200: {"description": "The checksum was already known: the existing record"},
        413: {"description": "Larger than ADMIN_UPLOAD_MAX_MB (`payload_too_large`)"},
        422: {"description": "Extension, content type or signature not accepted for the kind"},
    },
)
async def upload_file(
    principal: AdminPrincipal,
    service: AdminServiceDep,
    response: Response,
    file: Annotated[UploadFile, File(description="The file")],
    kind: Annotated[FileKind, Form(description="planning_document | gis | cadastral_extract")],
) -> UploadResult:
    result = await service.upload_file(principal, kind, file)
    if not result.created:
        response.status_code = 200
    return result


@router.get("/files", response_model=FileList, summary="Stored files with their job history")
async def list_files(
    principal: AdminPrincipal,
    service: AdminServiceDep,
    kind: Annotated[FileKind | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FileList:
    return await service.list_files(kind=kind, limit=limit, offset=offset)


@router.get("/files/{file_id}", response_model=StoredFileOut, responses=RESPONSES)
async def get_file(
    principal: AdminPrincipal, service: AdminServiceDep, file_id: Id
) -> StoredFileOut:
    return await service.get_file(file_id)


@router.post(
    "/files/{file_id}/jobs/geo",
    status_code=202,
    response_model=JobOut,
    summary="Queue the geometry job for a GIS file or vector PDF (staging only)",
    responses={
        **RESPONSES,
        200: {"description": "An identical job is already queued or running; returned as is"},
        409: {"description": "The file kind has no geometry job"},
    },
)
async def enqueue_geo_job(
    principal: AdminPrincipal, service: AdminServiceDep, file_id: Id, response: Response
) -> JobOut:
    enqueued = await service.enqueue_geo(principal, file_id)
    response.status_code = 202 if enqueued.created else 200
    return enqueued.job


# --- documents ------------------------------------------------------------------------------------


@router.post(
    "/documents",
    status_code=201,
    response_model=DocumentOut,
    summary="Register a planning document version against a stored PDF",
    responses={
        **RESPONSES,
        404: {"description": "`replaces_document_id` does not exist"},
        409: {"description": "`replaces_document_id` is not the current version"},
        422: {"description": "Unknown type / zone / file, or the file is not a planning PDF"},
    },
)
async def register_document(
    principal: AdminPrincipal, service: AdminServiceDep, payload: DocumentIn
) -> DocumentOut:
    return await service.register_document(principal, payload)


@router.get(
    "/documents",
    response_model=DocumentList,
    summary="Planning documents (current versions unless include_previous) with job history",
)
async def list_documents(
    principal: AdminPrincipal,
    service: AdminServiceDep,
    status: Annotated[DocumentStatus | None, Query()] = None,
    lineage_id: Annotated[int | None, Query(gt=0)] = None,
    include_previous: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentList:
    return await service.list_documents(
        status=status,
        lineage_id=lineage_id,
        include_previous=include_previous,
        limit=limit,
        offset=offset,
    )


@router.get("/documents/{document_id}", response_model=DocumentOut, responses=RESPONSES)
async def get_document(
    principal: AdminPrincipal, service: AdminServiceDep, document_id: Id
) -> DocumentOut:
    return await service.get_document(document_id)


@router.patch(
    "/documents/{document_id}/coverage",
    response_model=DocumentOut,
    summary="Mark a document's coverage area live (takes part in location resolution) or not",
    responses={
        **RESPONSES,
        409: {"description": "Not the current version, or no coverage geometry yet"},
    },
)
async def set_coverage(
    principal: AdminPrincipal, service: AdminServiceDep, document_id: Id, payload: CoverageIn
) -> DocumentOut:
    return await service.set_coverage_live(principal, document_id, payload.live)


@router.post(
    "/documents/{document_id}/jobs/extract",
    status_code=202,
    response_model=JobOut,
    summary="Queue the LLM extraction job for a document version (staging only)",
    responses={
        **RESPONSES,
        200: {"description": "An identical job is already queued or running; returned as is"},
        409: {"description": "The document has no stored file"},
    },
)
async def enqueue_extract_job(
    principal: AdminPrincipal, service: AdminServiceDep, document_id: Id, response: Response
) -> JobOut:
    enqueued = await service.enqueue_extract(principal, document_id)
    response.status_code = 202 if enqueued.created else 200
    return enqueued.job


# --- jobs -----------------------------------------------------------------------------------------
