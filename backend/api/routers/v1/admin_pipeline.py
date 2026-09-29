"""Staff pipeline API (writes: role ``admin``; the listings and reads: ``admin`` and ``reviewer``,
read-only documents per the pilot scope): files, planning document versions and
their files, jobs, coverage.

- ``POST /v1/admin/files`` (multipart ``file`` + ``kind``): upload to the private bucket,
  de-duplicated by SHA-256 (201 new, 200 when the checksum was already known);
- ``POST /v1/admin/documents``: register a document version with its stored files (``files``:
  file id + role text | drawing | both; none is fine, they can follow);
  ``replaces_document_id`` creates the next version and retires the current one;
- ``POST /v1/admin/documents/{id}/files``, ``PATCH`` / ``DELETE .../files/{file_id}``: add
  files to the current version, change what a file is read for, take one off (409 once an item
  read from it was approved);
- ``GET /v1/admin/files`` / ``/documents`` (+ ``/{id}``): listings with job history;
- ``POST /v1/admin/documents/{id}/jobs/extract[?file_id=]`` (one run per file),
  ``POST /v1/admin/files/{id}/jobs/geo``: queue the extraction / geometry job (staging only; both
  run the PDF pre-processing stage first when a PDF's manifest is missing): 202 with the job, or
  200 with the existing job when an identical one is already queued / running (idempotent);
  status, listing and retry live in ``admin_jobs`` (``GET /v1/admin/jobs/{id}`` is the status
  URL). Files and documents carry the pre-processing summary (``preprocessing``: vector / scanned
  pages, tables, chunks, sections);
- ``PATCH /v1/admin/documents/{id}``: change the current version's status, name, short code, zone,
  source, registry link, adoption date or licence note (audited ``document.update``);
- ``PATCH /v1/admin/documents/{id}/coverage``: mark a document's coverage area live or not;
- ``POST /v1/admin/zones/import``: queue the import of a zone GeoPackage drawn in QGIS (validated
  and staged by the worker; there is no zone editor).

Every action lands in ``audit_log``. Responses are ``Cache-Control: no-store``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Path, Query, Response, UploadFile

from api.deps import AdminServiceDep, DocumentReaderPrincipal, PipelinePrincipal
from api.schemas.admin import (
    CoverageIn,
    DocumentFileRoleIn,
    DocumentFilesIn,
    DocumentIn,
    DocumentList,
    DocumentOut,
    DocumentPatchIn,
    DocumentState,
    DocumentStatus,
    FileKind,
    FileList,
    JobOut,
    JobStateFilter,
    StoredFileOut,
    UploadResult,
    ZoneImportIn,
)


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(_no_store)])

RESPONSES = {
    401: {"description": "Missing or unknown bearer token (`unauthorized`)"},
    403: {"description": "The principal is neither admin nor reviewer (`forbidden`)"},
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
    principal: PipelinePrincipal,
    service: AdminServiceDep,
    response: Response,
    file: Annotated[UploadFile, File(description="The file")],
    kind: Annotated[
        FileKind,
        Form(description="planning_document | gis | cadastral_extract | market_data (.csv, .xlsx)"),
    ],
) -> UploadResult:
    result = await service.upload_file(principal, kind, file)
    if not result.created:
        response.status_code = 200
    return result


@router.get("/files", response_model=FileList, summary="Stored files with their job history")
async def list_files(
    principal: DocumentReaderPrincipal,
    service: AdminServiceDep,
    kind: Annotated[FileKind | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FileList:
    return await service.list_files(kind=kind, limit=limit, offset=offset)


@router.get("/files/{file_id}", response_model=StoredFileOut, responses=RESPONSES)
async def get_file(
    principal: DocumentReaderPrincipal, service: AdminServiceDep, file_id: Id
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
    principal: PipelinePrincipal, service: AdminServiceDep, file_id: Id, response: Response
) -> JobOut:
    enqueued = await service.enqueue_geo(principal, file_id)
    response.status_code = 202 if enqueued.created else 200
    return enqueued.job


# --- documents ------------------------------------------------------------------------------------


@router.post(
    "/documents",
    status_code=201,
    response_model=DocumentOut,
    summary="Register a planning document version with its stored files",
    responses={
        **RESPONSES,
        404: {"description": "`replaces_document_id` does not exist"},
        409: {"description": "`replaces_document_id` is not the current version"},
        422: {
            "description": (
                "Unknown type / zone / file, a file that is not a planning PDF (GIS files only "
                "as drawings), or a file listed twice"
            )
        },
    },
)
async def register_document(
    principal: PipelinePrincipal, service: AdminServiceDep, payload: DocumentIn
) -> DocumentOut:
    return await service.register_document(principal, payload)


@router.get(
    "/documents",
    response_model=DocumentList,
    summary="Planning documents (current versions unless include_previous) with job history",
)
async def list_documents(
    principal: DocumentReaderPrincipal,
    service: AdminServiceDep,
    status: Annotated[DocumentStatus | None, Query()] = None,
    lineage_id: Annotated[int | None, Query(gt=0)] = None,
    include_previous: Annotated[bool, Query()] = False,
    zone_id: Annotated[int | None, Query(gt=0)] = None,
    state: Annotated[
        DocumentState | None, Query(description="Where the version stands (DocumentOut.state)")
    ] = None,
    job_state: Annotated[
        JobStateFilter | None,
        Query(description="Some file's latest extraction or geometry job is in this state"),
    ] = None,
    q: Annotated[str | None, Query(max_length=200, description="Part of the name")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentList:
    return await service.list_documents(
        status=status,
        lineage_id=lineage_id,
        include_previous=include_previous,
        zone_id=zone_id,
        state=state,
        job_state=job_state,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/documents/{document_id}", response_model=DocumentOut, responses=RESPONSES)
async def get_document(
    principal: DocumentReaderPrincipal, service: AdminServiceDep, document_id: Id
) -> DocumentOut:
    return await service.get_document(document_id)


@router.patch(
    "/documents/{document_id}",
    response_model=DocumentOut,
    summary="Change the current version's status, name, short code, zone, source or link",
    responses={
        **RESPONSES,
        404: {"description": "No such document"},
        409: {
            "description": (
                "Not the current version (`not_current_version`), or the short code already "
                "names another document (`short_code_taken`)"
            )
        },
        422: {"description": "Nothing to change, an unknown zone, or a value out of bounds"},
    },
)
async def update_document(
    principal: PipelinePrincipal,
    service: AdminServiceDep,
    document_id: Id,
    payload: DocumentPatchIn,
) -> DocumentOut:
    return await service.update_document(principal, document_id, payload)


@router.post(
    "/documents/{document_id}/files",
    status_code=201,
    response_model=DocumentOut,
    summary="Add stored files to the current version of a document",
    responses={
        **RESPONSES,
        200: {"description": "Every file was already on the version; nothing changed"},
        404: {"description": "No such document"},
        409: {"description": "Not the current version (`not_current_version`)"},
        422: {"description": "Unknown file, not a planning PDF, or a GIS file not as a drawing"},
    },
)
async def attach_document_files(
    principal: PipelinePrincipal,
    service: AdminServiceDep,
    document_id: Id,
    payload: DocumentFilesIn,
    response: Response,
) -> DocumentOut:
    document, added = await service.attach_files(principal, document_id, payload.files)
    response.status_code = 201 if added else 200
    return document


@router.patch(
    "/documents/{document_id}/files/{file_id}",
    response_model=DocumentOut,
    summary="Change what a file of the current version is read for (text, drawing, both)",
    responses={
        **RESPONSES,
        404: {"description": "No such document, or the file is not on it"},
        409: {"description": "Not the current version"},
        422: {"description": "A GIS file can only be a drawing"},
    },
)
async def set_document_file_role(
    principal: PipelinePrincipal,
    service: AdminServiceDep,
    document_id: Id,
    file_id: Id,
    payload: DocumentFileRoleIn,
) -> DocumentOut:
    return await service.set_file_role(principal, document_id, file_id, payload.role)


@router.delete(
    "/documents/{document_id}/files/{file_id}",
    response_model=DocumentOut,
    summary="Take a file off the current version (its open review items are superseded)",
    responses={
        **RESPONSES,
        404: {"description": "No such document, or the file is not on it"},
        409: {
            "description": (
                "Not the current version, items read from the file were approved "
                "(`items_accepted`), published values cite it (`values_published`) or it is "
                "being extracted (`extraction_active`)"
            )
        },
    },
)
async def remove_document_file(
    principal: PipelinePrincipal, service: AdminServiceDep, document_id: Id, file_id: Id
) -> DocumentOut:
    return await service.detach_file(principal, document_id, file_id)


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
    principal: PipelinePrincipal, service: AdminServiceDep, document_id: Id, payload: CoverageIn
) -> DocumentOut:
    return await service.set_coverage_live(principal, document_id, payload.live)


@router.post(
    "/documents/{document_id}/jobs/extract",
    status_code=202,
    response_model=JobOut,
    summary="Queue the LLM extraction run over one file of a document version (staging only)",
    responses={
        **RESPONSES,
        200: {
            "description": (
                "Nothing new to do: the same file was already read with the same model, prompt "
                "and schema versions (that run's job, with its summary), or an identical job is "
                "queued or running (returned as is)"
            )
        },
        404: {"description": "No such document, or `file_id` is not one of its files"},
        409: {
            "description": (
                "No file to extract (`no_file`), or the file is a drawing (`drawing_file`) or "
                "not a PDF (`not_a_pdf`)"
            )
        },
    },
)
async def enqueue_extract_job(
    principal: PipelinePrincipal,
    service: AdminServiceDep,
    document_id: Id,
    response: Response,
    file_id: Annotated[
        int | None,
        Query(gt=0, description="The file to read (default: the version's primary text file)"),
    ] = None,
    force: Annotated[
        bool,
        Query(description="Read the file again even though an identical run already finished"),
    ] = False,
) -> JobOut:
    enqueued = await service.enqueue_extract(principal, document_id, file_id=file_id, force=force)
    response.status_code = 202 if enqueued.created else 200
    return enqueued.job


# --- zones ----------------------------------------------------------------------------------------


@router.post(
    "/zones/import",
    status_code=202,
    response_model=JobOut,
    summary="Queue the import of a zone GeoPackage drawn in QGIS (validated, then staged)",
    responses={
        **RESPONSES,
        200: {"description": "The same import of the same file is already queued or running"},
        404: {"description": "No such stored file"},
        409: {"description": "The file is not a GeoPackage GIS file (`not_a_geopackage`)"},
    },
)
async def import_zones(
    principal: PipelinePrincipal,
    service: AdminServiceDep,
    payload: ZoneImportIn,
    response: Response,
) -> JobOut:
    enqueued = await service.enqueue_zone_import(
        principal, payload.file_id, dry_run=payload.dry_run
    )
    response.status_code = 202 if enqueued.created else 200
    return enqueued.job


# --- jobs -----------------------------------------------------------------------------------------
