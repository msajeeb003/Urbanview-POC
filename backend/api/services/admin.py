"""Staff pipeline API (``/v1/admin/files``, ``/documents``, ``/jobs``): files, document versions,
jobs, the coverage switch, and the audit log behind all of it.

Everything here is role-gated (``admin``) and operates the data pipeline. Nothing writes to the
serving tables except the coverage switch (``planning_documents.coverage_live``), which decides
whether a document's coverage area takes part in location resolution. Extraction and geometry
jobs are queued to Celery and write to STAGING only (``jobs/base.py``, ``jobs/enqueue.py``);
publishing is a separate item. Every action is written to ``audit_log`` (actor, action,
entity, details, request id).

Files: uploads go to the private bucket under ``{municipality}/uploads/{kind}/{sha256}/{name}``.
The SHA-256 is computed while reading; a checksum that is already known answers with the existing
record and stores nothing (``UploadResult.created = false``).

Documents: ``POST /admin/documents`` creates one ``planning_documents`` row per *version*. A
registration with ``replaces_document_id`` retires that current version (``is_current_version =
false``, its coverage taken offline) and inserts version n+1 in the same lineage, copying the
previous coverage geometry so the geometry job can replace it. Previous versions stay, and the
values that cite them stay with them.
"""

from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
import os
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import UploadFile
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from api.schemas.admin import (
    DocumentIn,
    DocumentList,
    DocumentOut,
    ExtractionRunOut,
    FileKind,
    FileList,
    FileSummary,
    JobOut,
    ReviewSummary,
    StoredFileOut,
    UploadResult,
    VersionRef,
)
from api.services.audit import write_audit
from api.services.jobs import JOB_JSON, job_out
from api.services.review import can_publish
from core.auth import Principal
from core.errors import AppError, ConflictError, NotFoundError, ServiceUnavailableError
from core.extraction.manifest import PreprocessSummary
from core.extraction.runs import RUN_JSON, insert_run, reusable_run, run_dedupe_key
from core.municipality import MunicipalityProfile
from core.storage import ObjectStorage
from jobs.enqueue import JobDispatcher, enqueue_job

log = logging.getLogger("urbanview.admin")

MB = 1024 * 1024
READ_CHUNK = MB
JOB_HISTORY_LIMIT = 10


# --- upload validation ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KindSpec:
    extensions: frozenset[str]
    mimes: frozenset[str]


GENERIC_MIMES = frozenset({"", "application/octet-stream", "binary/octet-stream"})
ZIP_MIMES = frozenset({"application/zip", "application/x-zip-compressed"})
KIND_SPECS: dict[str, KindSpec] = {
    "planning_document": KindSpec(frozenset({".pdf"}), frozenset({"application/pdf"})),
    "expert_report": KindSpec(frozenset({".pdf"}), frozenset({"application/pdf"})),
    "gis": KindSpec(
        frozenset({".zip", ".geojson", ".json", ".gpkg", ".dxf", ".kml", ".kmz", ".pdf"}),
        ZIP_MIMES
        | frozenset(
            {
                "application/geo+json",
                "application/json",
                "application/geopackage+sqlite3",
                "application/x-sqlite3",
                "application/dxf",
                "image/vnd.dxf",
                "application/vnd.google-earth.kml+xml",
                "application/vnd.google-earth.kmz",
                "application/pdf",
            }
        ),
    ),
    # market figures (core.market): official statistics tables and the client's range sheets
    "market_data": KindSpec(
        frozenset({".csv", ".tsv", ".txt", ".xlsx"}),
        frozenset(
            {
                "text/csv",
                "text/plain",
                "text/tab-separated-values",
                "application/csv",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }
        ),
    ),
    "cadastral_extract": KindSpec(
        frozenset({".csv", ".txt", ".xlsx", ".xls", ".zip", ".pdf", ".json", ".geojson"}),
        ZIP_MIMES
        | frozenset(
            {
                "text/csv",
                "text/plain",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.ms-excel",
                "application/pdf",
                "application/json",
                "application/geo+json",
            }
        ),
    ),
}
MAGIC: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".zip": (b"PK\x03\x04",),
    ".kmz": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04",),
    ".gpkg": (b"SQLite format 3\x00",),
}
EXTENSION_MIMES = {
    ".geojson": "application/geo+json",
    ".gpkg": "application/geopackage+sqlite3",
    ".dxf": "application/dxf",
    ".kml": "application/vnd.google-earth.kml+xml",
    ".kmz": "application/vnd.google-earth.kmz",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
}


def _validation_error(problems: list[dict[str, Any]]) -> AppError:
    return AppError(
        "Request validation failed", code="validation_error", status_code=422, details=problems
    )


def validate_upload(
    kind: str, filename: str | None, content_type: str | None, head: bytes
) -> tuple[str, str]:
    """Extension and MIME type to record, or a 422: the kind decides the accepted extensions and
    declared types, and the first bytes must match the extension's signature when it has one."""
    spec = KIND_SPECS[kind]
    extension = os.path.splitext((filename or "").replace("\\", "/").rsplit("/", 1)[-1])[1].lower()
    declared = (content_type or "").split(";")[0].strip().lower()
    problems: list[dict[str, Any]] = []
    if extension not in spec.extensions:
        problems.append(
            {
                "loc": ["body", "file"],
                "msg": f"{extension or 'a file without extension'} is not accepted for kind "
                f"{kind} (accepted: {', '.join(sorted(spec.extensions))})",
            }
        )
    if declared not in GENERIC_MIMES and declared not in spec.mimes:
        problems.append(
            {"loc": ["body", "file"], "msg": f"content type {declared} is not accepted for {kind}"}
        )
    signatures = MAGIC.get(extension)
    if signatures and not any(head.startswith(s) for s in signatures):
        problems.append(
            {"loc": ["body", "file"], "msg": f"the content does not look like a {extension} file"}
        )
    if problems:
        raise _validation_error(problems)
    if declared in GENERIC_MIMES:
        mime = EXTENSION_MIMES.get(extension) or mimetypes.guess_type(f"x{extension}")[0]
        declared = mime or "application/octet-stream"
    return extension, declared


def safe_filename(name: str | None, fallback: str = "file") -> str:
    """ASCII, no path, no odd characters, ≤ 120 chars, extension kept."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    base = re.sub(r"_+\.", ".", base)
    base = re.sub(r"_{2,}", "_", base).strip("._-")
    if not base:
        return fallback
    if len(base) > 120:
        stem, dot, ext = base.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            base = stem[: 120 - len(ext) - 1].rstrip("._-") + "." + ext
        else:
            base = base[:120]
    return base


def _pdf_page_count(data: bytes) -> int | None:
    try:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(data), strict=False).pages)
    except Exception:  # noqa: BLE001 - an unreadable PDF is stored with an unknown page count
        return None


class StorageWriter(Protocol):
    def put_bytes(self, key: str, data: bytes, content_type: str = ...) -> str: ...


# --- SQL -----------------------------------------------------------------------------------------

_JOB_JSON = JOB_JSON


def _file_sql(extra: str) -> str:
    return f"""
    SELECT f.id, f.kind, f.original_filename, f.mime_type, f.size_bytes, f.sha256, f.page_count,
           f.uploaded_by, f.uploaded_at, f.preprocess -> 'summary' AS preprocessing,
           (SELECT {RUN_JSON} FROM extraction_runs r WHERE r.file_id = f.id
            ORDER BY r.id DESC LIMIT 1) AS extraction,
           COALESCE((SELECT jsonb_agg(d.id ORDER BY d.id) FROM planning_documents d
                     WHERE d.file_id = f.id), '[]'::jsonb) AS document_ids,
           COALESCE((SELECT jsonb_agg({_JOB_JSON} ORDER BY j.requested_at DESC, j.id DESC)
                     FROM (SELECT * FROM pipeline_jobs p WHERE p.file_id = f.id
                           ORDER BY p.requested_at DESC, p.id DESC LIMIT :jobs_limit) j),
                    '[]'::jsonb) AS jobs
    FROM stored_files f
    WHERE f.municipality_id = :m {extra}
    ORDER BY f.uploaded_at DESC, f.id DESC
    LIMIT :limit OFFSET :offset
    """


def _document_sql(extra: str) -> str:
    return f"""
    SELECT d.id, d.name, d.type, d.status::text AS status, d.source, d.source_url, d.zone_id,
           z.name AS zone_name, d.amends_document_id, d.licence_note, d.adopted_on, d.file_id,
           d.page_count,
           COALESCE(d.lineage_id, d.id) AS lineage_id, d.version, d.is_current_version,
           (d.coverage_geom IS NOT NULL) AS has_coverage, d.coverage_live,
           d.coverage_live_changed_at, d.coverage_live_changed_by,
           d.registered_by, d.registered_at, d.created_at,
           CASE WHEN f.id IS NULL THEN NULL ELSE jsonb_build_object(
               'id', f.id, 'kind', f.kind, 'original_filename', f.original_filename,
               'mime_type', f.mime_type, 'size_bytes', f.size_bytes, 'sha256', f.sha256,
               'page_count', f.page_count, 'uploaded_by', f.uploaded_by,
               'uploaded_at', f.uploaded_at) END AS file,
           f.preprocess -> 'summary' AS preprocessing,
           (SELECT {RUN_JSON} FROM extraction_runs r WHERE r.document_id = d.id
            ORDER BY r.id DESC LIMIT 1) AS extraction,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
                         'id', v.id, 'version', v.version, 'status', v.status::text,
                         'is_current_version', v.is_current_version,
                         'registered_at', v.registered_at) ORDER BY v.version, v.id)
                     FROM planning_documents v
                     WHERE COALESCE(v.lineage_id, v.id) = COALESCE(d.lineage_id, d.id)),
                    '[]'::jsonb) AS versions,
           COALESCE((SELECT jsonb_agg({_JOB_JSON} ORDER BY j.requested_at DESC, j.id DESC)
                     FROM (SELECT * FROM pipeline_jobs p WHERE p.document_id = d.id
                           ORDER BY p.requested_at DESC, p.id DESC LIMIT :jobs_limit) j),
                    '[]'::jsonb) AS jobs,
           (SELECT jsonb_build_object(
                'pending', count(*) FILTER (WHERE e.review_state = 'pending_review'),
                'approved', count(*) FILTER (WHERE e.review_state = 'approved'),
                'amended', count(*) FILTER (WHERE e.review_state = 'amended'),
                'rejected', count(*) FILTER (WHERE e.review_state = 'rejected'),
                'total', count(*))
            FROM planning_parameter_extractions e
            WHERE e.document_id = d.id AND e.superseded_at IS NULL) AS review
    FROM planning_documents d
    LEFT JOIN zones z ON z.id = d.zone_id
    LEFT JOIN stored_files f ON f.id = d.file_id
    WHERE d.municipality_id = :m {extra}
    ORDER BY d.registered_at DESC NULLS LAST, d.id DESC
    LIMIT :limit OFFSET :offset
    """


FILE_BY_ID_SQL = text(_file_sql("AND f.id = :id"))
FILE_BY_SHA_SQL = text(
    "SELECT id FROM stored_files WHERE municipality_id = :m AND sha256 = :sha256"
)
INSERT_FILE_SQL = text(
    """
    INSERT INTO stored_files (municipality_id, kind, object_key, sha256, original_filename,
                              mime_type, size_bytes, page_count, uploaded_by, uploaded_by_user_id)
    VALUES (:m, :kind, :object_key, :sha256, :original_filename, :mime_type, :size_bytes,
            :page_count, :uploaded_by, :uploaded_by_user_id)
    RETURNING id
    """
)
FILE_REF_SQL = text(
    "SELECT id, kind, object_key, page_count, sha256 FROM stored_files "
    "WHERE id = :id AND municipality_id = :m"
)
ZONE_EXISTS_SQL = text("SELECT 1 FROM zones WHERE id = :id AND municipality_id = :m")
DOCUMENT_EXISTS_SQL = text(
    "SELECT 1 FROM planning_documents WHERE id = :id AND municipality_id = :m"
)
DOCUMENT_BY_ID_SQL = text(_document_sql("AND d.id = :id"))
DOCUMENT_VERSION_SQL = text(
    """
    SELECT d.id, COALESCE(d.lineage_id, d.id) AS lineage_id, d.version, d.is_current_version,
           d.coverage_live, d.zone_id,
           (SELECT c.id FROM planning_documents c
            WHERE COALESCE(c.lineage_id, c.id) = COALESCE(d.lineage_id, d.id)
              AND c.is_current_version
            ORDER BY c.version DESC LIMIT 1) AS current_id
    FROM planning_documents d
    WHERE d.id = :id AND d.municipality_id = :m
    """
)
RETIRE_VERSION_SQL = text(
    """
    UPDATE planning_documents
    SET is_current_version = false,
        coverage_live_changed_at = CASE WHEN coverage_live THEN :at
                                        ELSE coverage_live_changed_at END,
        coverage_live_changed_by = CASE WHEN coverage_live THEN :by
                                        ELSE coverage_live_changed_by END,
        coverage_live = false
    WHERE id = :id
    """
)
INSERT_DOCUMENT_SQL = text(
    """
    INSERT INTO planning_documents (
        municipality_id, name, type, status, source, source_url, zone_id, amends_document_id,
        coverage_geom, file_id, file_key, page_count, page_images_rendered, lineage_id, version,
        is_current_version, licence_note, adopted_on, registered_by, registered_at,
        dataset_version)
    VALUES (
        :m, :name, :type, CAST(:status AS planning_document_status), :source, :source_url,
        :zone_id, :amends_document_id,
        (SELECT p.coverage_geom FROM planning_documents p WHERE p.id = :previous_id),
        :file_id, :file_key, :page_count, false, :lineage_id, :version, true, :licence_note,
        CAST(:adopted_on AS date), :registered_by, :registered_at, NULL)
    RETURNING id
    """
)
SET_LINEAGE_SQL = text("UPDATE planning_documents SET lineage_id = id WHERE id = :id")
DOCUMENT_FOR_JOB_SQL = text(
    "SELECT d.id, d.file_id, f.sha256 FROM planning_documents d "
    "LEFT JOIN stored_files f ON f.id = d.file_id "
    "WHERE d.id = :id AND d.municipality_id = :m"
)
JOB_SQL = text(
    """
    INSERT INTO pipeline_jobs (municipality_id, kind, status, document_id, file_id,
                               requested_by, requested_by_user_id)
    VALUES (:m, :kind, 'queued', :document_id, :file_id, :requested_by, :requested_by_user_id)
    RETURNING id
    """
)
SET_TASK_SQL = text(
    "UPDATE pipeline_jobs SET celery_task_id = :task_id WHERE id = :id AND celery_task_id IS NULL"
)
FAIL_JOB_SQL = text(
    "UPDATE pipeline_jobs SET status = 'failed', error = :error, finished_at = :at WHERE id = :id"
)
JOB_SQL = text(
    f"SELECT {_JOB_JSON} AS job FROM pipeline_jobs j WHERE j.id = :id AND j.municipality_id = :m"
)
COVERAGE_STATE_SQL = text(
    """
    SELECT id, status::text AS status, is_current_version, coverage_live,
           (coverage_geom IS NOT NULL) AS has_coverage
    FROM planning_documents WHERE id = :id AND municipality_id = :m
    """
)
SET_COVERAGE_SQL = text(
    """
    UPDATE planning_documents
    SET coverage_live = :live, coverage_live_changed_at = :at, coverage_live_changed_by = :by
    WHERE id = :id
    """
)


# --- service -------------------------------------------------------------------------------------


_job_out = job_out


@dataclass(frozen=True, slots=True)
class EnqueuedJob:
    """The job an enqueue call ended with; ``created`` is False when an identical job was
    already queued / running and was returned instead (idempotency)."""

    job: JobOut
    created: bool


def _file_out(row: Mapping[str, Any]) -> StoredFileOut:
    return StoredFileOut(
        id=row["id"],
        kind=row["kind"],
        original_filename=row["original_filename"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        page_count=row["page_count"],
        uploaded_by=row["uploaded_by"],
        uploaded_at=row["uploaded_at"],
        document_ids=list(row["document_ids"] or []),
        jobs=[_job_out(j) for j in row["jobs"] or []],
        preprocessing=_preprocessing(row.get("preprocessing")),
        extraction=_extraction(row.get("extraction")),
    )


def _extraction(raw: Any) -> ExtractionRunOut | None:
    return ExtractionRunOut.model_validate(raw) if raw else None


def _preprocessing(raw: Any) -> PreprocessSummary | None:
    """The file's pre-processing summary; None when it has not run (or is of another shape)."""
    if not raw:
        return None
    try:
        return PreprocessSummary.model_validate(raw)
    except ValidationError:
        return None


def _document_out(row: Mapping[str, Any]) -> DocumentOut:
    return DocumentOut(
        id=row["id"],
        lineage_id=row["lineage_id"],
        version=row["version"],
        is_current_version=row["is_current_version"],
        name=row["name"],
        type=row["type"],
        status=row["status"],
        source=row["source"],
        source_url=row["source_url"],
        zone_id=row["zone_id"],
        zone_name=row["zone_name"],
        amends_document_id=row["amends_document_id"],
        licence_note=row["licence_note"],
        adopted_on=row["adopted_on"],
        file=FileSummary(**row["file"]) if row["file"] else None,
        page_count=row["page_count"],
        has_coverage=bool(row["has_coverage"]),
        coverage_live=bool(row["coverage_live"]),
        coverage_live_changed_at=row["coverage_live_changed_at"],
        coverage_live_changed_by=row["coverage_live_changed_by"],
        registered_by=row["registered_by"],
        registered_at=row["registered_at"],
        created_at=row["created_at"],
        versions=[VersionRef(**v) for v in row["versions"] or []],
        jobs=[_job_out(j) for j in row["jobs"] or []],
        review=_review_summary(row["review"]),
        preprocessing=_preprocessing(row.get("preprocessing")),
        extraction=_extraction(row.get("extraction")),
    )


def _review_summary(raw: Mapping[str, Any] | None) -> ReviewSummary:
    counts = {
        k: int((raw or {}).get(k) or 0)
        for k in ("pending", "approved", "amended", "rejected", "total")
    }
    ok, blockers = can_publish(counts["pending"], counts["approved"], counts["amended"])
    return ReviewSummary(**counts, can_publish=ok, publish_blockers=blockers)


class AdminService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        storage: StorageWriter,
        dispatcher: JobDispatcher,
        municipality: MunicipalityProfile,
        upload_max_bytes: int = 100 * MB,
        jobs_limit: int = JOB_HISTORY_LIMIT,
        max_attempts: int = 3,
        extraction_model: str = "claude-sonnet-5",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.dispatcher = dispatcher
        self.municipality = municipality
        self.extraction_model = extraction_model
        self.upload_max_bytes = int(upload_max_bytes)
        self.jobs_limit = int(jobs_limit)
        self.max_attempts = int(max_attempts)
        self.clock = clock

    @property
    def municipality_id(self) -> str:
        return self.municipality.id

    # --- audit -----------------------------------------------------------------------------------

    async def _audit(
        self,
        session: AsyncSession,
        principal: Principal,
        action: str,
        entity_type: str | None,
        entity_id: int | None,
        details: Mapping[str, Any] | None = None,
        *,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        note: str | None = None,
    ) -> None:
        await write_audit(
            session,
            municipality_id=self.municipality_id,
            principal=principal,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
            before=before,
            after=after,
            note=note,
        )

    # --- files -----------------------------------------------------------------------------------

    async def upload_file(
        self, principal: Principal, kind: FileKind, upload: UploadFile
    ) -> UploadResult:
        hasher = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while chunk := await upload.read(READ_CHUNK):
            total += len(chunk)
            if total > self.upload_max_bytes:
                raise AppError(
                    f"File larger than {self.upload_max_bytes // MB} MB",
                    code="payload_too_large",
                    status_code=413,
                )
            hasher.update(chunk)
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise _validation_error([{"loc": ["body", "file"], "msg": "the file is empty"}])
        extension, mime = validate_upload(
            kind.value, upload.filename, upload.content_type, data[:16]
        )
        sha256 = hasher.hexdigest()
        filename = safe_filename(upload.filename, fallback=f"upload{extension}")
        details = {"kind": kind.value, "sha256": sha256, "original_filename": filename}

        async with self.session_factory() as session:
            existing_id = (
                await session.execute(
                    FILE_BY_SHA_SQL, {"m": self.municipality_id, "sha256": sha256}
                )
            ).scalar_one_or_none()
            if existing_id is not None:
                await self._audit(
                    session, principal, "file.upload_duplicate", "stored_file", existing_id, details
                )
                await session.commit()
                return UploadResult(file=await self.get_file(int(existing_id)), created=False)

        page_count = await run_in_threadpool(_pdf_page_count, data) if extension == ".pdf" else None
        key = ObjectStorage.upload_key(self.municipality_id, kind.value, sha256, filename)
        try:
            await run_in_threadpool(self.storage.put_bytes, key, data, mime)
        except (BotoCoreError, ClientError) as exc:
            log.warning("upload to object storage failed (%s: %s)", type(exc).__name__, exc)
            raise ServiceUnavailableError("Object storage is not available") from exc

        params = {
            "m": self.municipality_id,
            "kind": kind.value,
            "object_key": key,
            "sha256": sha256,
            "original_filename": filename,
            "mime_type": mime,
            "size_bytes": len(data),
            "page_count": page_count,
            "uploaded_by": principal.subject,
            "uploaded_by_user_id": principal.user_id,
        }
        async with self.session_factory() as session:
            try:
                file_id = (await session.execute(INSERT_FILE_SQL, params)).scalar_one()
            except IntegrityError:
                # lost a race with an identical upload: the same object key, the same content
                await session.rollback()
                existing_id = (
                    await session.execute(
                        FILE_BY_SHA_SQL, {"m": self.municipality_id, "sha256": sha256}
                    )
                ).scalar_one()
                await self._audit(
                    session, principal, "file.upload_duplicate", "stored_file", existing_id, details
                )
                await session.commit()
                return UploadResult(file=await self.get_file(int(existing_id)), created=False)
            await self._audit(
                session,
                principal,
                "file.upload",
                "stored_file",
                file_id,
                {**details, "mime_type": mime, "size_bytes": len(data), "page_count": page_count},
            )
            await session.commit()
        return UploadResult(file=await self.get_file(int(file_id)), created=True)

    async def get_file(self, file_id: int) -> StoredFileOut:
        params = {"m": self.municipality_id, "id": file_id, "jobs_limit": self.jobs_limit}
        async with self.session_factory() as session:
            row = (
                (await session.execute(FILE_BY_ID_SQL, {**params, "limit": 1, "offset": 0}))
                .mappings()
                .first()
            )
        if row is None:
            raise NotFoundError(f"No stored file with id {file_id}", details={"file_id": file_id})
        return _file_out(row)

    async def list_files(
        self, *, kind: FileKind | None = None, limit: int = 50, offset: int = 0
    ) -> FileList:
        params: dict[str, Any] = {
            "m": self.municipality_id,
            "jobs_limit": self.jobs_limit,
            "limit": limit,
            "offset": offset,
        }
        extra = ""
        if kind is not None:
            extra, params["kind"] = "AND f.kind = :kind", kind.value
        async with self.session_factory() as session:
            rows = (await session.execute(text(_file_sql(extra)), params)).mappings().all()
        return FileList(items=[_file_out(r) for r in rows], limit=limit, offset=offset)

    # --- documents -------------------------------------------------------------------------------

    async def register_document(self, principal: Principal, payload: DocumentIn) -> DocumentOut:
        doc_type = payload.type.strip().upper()
        m = self.municipality_id
        async with self.session_factory() as session:
            problems: list[dict[str, Any]] = []
            known_types = self.municipality.terminology.document_types
            if known_types and doc_type not in known_types:
                problems.append(
                    {
                        "loc": ["body", "type"],
                        "msg": f"type must be one of {', '.join(sorted(known_types))}",
                    }
                )
            file_row = (
                (await session.execute(FILE_REF_SQL, {"m": m, "id": payload.file_id}))
                .mappings()
                .first()
            )
            if file_row is None:
                problems.append({"loc": ["body", "file_id"], "msg": "no such stored file"})
            elif file_row["kind"] != FileKind.planning_document.value:
                problems.append(
                    {
                        "loc": ["body", "file_id"],
                        "msg": f"the file is a {file_row['kind']}; a planning_document PDF "
                        "is required",
                    }
                )
            if payload.zone_id is not None and not await self._exists(
                session, ZONE_EXISTS_SQL, payload.zone_id
            ):
                problems.append({"loc": ["body", "zone_id"], "msg": "no such zone"})
            if payload.amends_document_id is not None and not await self._exists(
                session, DOCUMENT_EXISTS_SQL, payload.amends_document_id
            ):
                problems.append(
                    {"loc": ["body", "amends_document_id"], "msg": "no such planning document"}
                )
            previous = None
            if payload.replaces_document_id is not None:
                previous = (
                    (
                        await session.execute(
                            DOCUMENT_VERSION_SQL, {"m": m, "id": payload.replaces_document_id}
                        )
                    )
                    .mappings()
                    .first()
                )
                if previous is None:
                    raise NotFoundError(
                        f"No planning document with id {payload.replaces_document_id}",
                        details={"document_id": payload.replaces_document_id},
                    )
                if not previous["is_current_version"]:
                    raise ConflictError(
                        "Only the current version of a document can be replaced",
                        details={
                            "document_id": previous["id"],
                            "current_version_id": previous["current_id"],
                        },
                    )
            if problems:
                raise _validation_error(problems)

            now = self.clock()
            if previous is not None:
                await session.execute(
                    RETIRE_VERSION_SQL, {"id": previous["id"], "at": now, "by": principal.subject}
                )
            new_id = (
                await session.execute(
                    INSERT_DOCUMENT_SQL,
                    {
                        "m": m,
                        "name": payload.name.strip(),
                        "type": doc_type,
                        "status": payload.status,
                        "source": payload.source,
                        "source_url": payload.source_url,
                        "zone_id": payload.zone_id
                        if payload.zone_id is not None
                        else (previous["zone_id"] if previous is not None else None),
                        "amends_document_id": payload.amends_document_id,
                        "previous_id": previous["id"] if previous is not None else None,
                        "file_id": file_row["id"],
                        "file_key": file_row["object_key"],
                        "page_count": file_row["page_count"],
                        "lineage_id": previous["lineage_id"] if previous is not None else None,
                        "version": (previous["version"] + 1) if previous is not None else 1,
                        "licence_note": payload.licence_note,
                        "adopted_on": payload.adopted_on,
                        "registered_by": principal.subject,
                        "registered_at": now,
                    },
                )
            ).scalar_one()
            if previous is None:
                await session.execute(SET_LINEAGE_SQL, {"id": new_id})
            await self._audit(
                session,
                principal,
                "document.register",
                "planning_document",
                new_id,
                {
                    "name": payload.name.strip(),
                    "type": doc_type,
                    "status": payload.status,
                    "file_id": file_row["id"],
                    "version": (previous["version"] + 1) if previous is not None else 1,
                    "replaces_document_id": previous["id"] if previous is not None else None,
                },
            )
            if previous is not None and previous["coverage_live"]:
                await self._audit(
                    session,
                    principal,
                    "coverage.set_live",
                    "planning_document",
                    previous["id"],
                    {"live": False, "reason": "superseded_by_version", "new_version_id": new_id},
                )
            await session.commit()
        return await self.get_document(int(new_id))

    async def get_document(self, document_id: int) -> DocumentOut:
        params = {"m": self.municipality_id, "id": document_id, "jobs_limit": self.jobs_limit}
        async with self.session_factory() as session:
            row = (
                (await session.execute(DOCUMENT_BY_ID_SQL, {**params, "limit": 1, "offset": 0}))
                .mappings()
                .first()
            )
        if row is None:
            raise NotFoundError(
                f"No planning document with id {document_id}", details={"document_id": document_id}
            )
        return _document_out(row)

    async def list_documents(
        self,
        *,
        status: str | None = None,
        lineage_id: int | None = None,
        include_previous: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> DocumentList:
        params: dict[str, Any] = {
            "m": self.municipality_id,
            "jobs_limit": self.jobs_limit,
            "limit": limit,
            "offset": offset,
        }
        clauses: list[str] = []
        if status is not None:
            clauses.append("AND d.status = CAST(:status AS planning_document_status)")
            params["status"] = status
        if lineage_id is not None:
            clauses.append("AND COALESCE(d.lineage_id, d.id) = :lineage_id")
            params["lineage_id"] = lineage_id
        if not include_previous:
            clauses.append("AND d.is_current_version")
        async with self.session_factory() as session:
            rows = (
                (await session.execute(text(_document_sql(" ".join(clauses))), params))
                .mappings()
                .all()
            )
        return DocumentList(items=[_document_out(r) for r in rows], limit=limit, offset=offset)

    async def set_coverage_live(
        self, principal: Principal, document_id: int, live: bool
    ) -> DocumentOut:
        async with self.session_factory() as session:
            state = (
                (
                    await session.execute(
                        COVERAGE_STATE_SQL, {"m": self.municipality_id, "id": document_id}
                    )
                )
                .mappings()
                .first()
            )
            if state is None:
                raise NotFoundError(
                    f"No planning document with id {document_id}",
                    details={"document_id": document_id},
                )
            if live and not state["is_current_version"]:
                raise ConflictError(
                    "Only the current version of a document can have live coverage",
                    details={"document_id": document_id, "reason": "not_current_version"},
                )
            if live and not state["has_coverage"]:
                raise ConflictError(
                    "The document has no coverage geometry yet; run the geometry job first",
                    details={"document_id": document_id, "reason": "no_coverage_geometry"},
                )
            await session.execute(
                SET_COVERAGE_SQL,
                {"id": document_id, "live": live, "at": self.clock(), "by": principal.subject},
            )
            await self._audit(
                session,
                principal,
                "coverage.set_live",
                "planning_document",
                document_id,
                {"live": live, "was_live": bool(state["coverage_live"]), "status": state["status"]},
                before={"coverage_live": bool(state["coverage_live"])},
                after={"coverage_live": live},
            )
            await session.commit()
        return await self.get_document(document_id)

    # --- jobs ------------------------------------------------------------------------------------

    async def enqueue_extract(
        self, principal: Principal, document_id: int, *, force: bool = False
    ) -> EnqueuedJob:
        """Queue one extraction run of the document version's file. Idempotent: the run that
        already read the same file with the same model, prompt and schema versions answers
        (unless ``force``), and an identical job still queued / running is returned as it is."""
        async with self.session_factory() as session:
            doc = (
                (
                    await session.execute(
                        DOCUMENT_FOR_JOB_SQL, {"m": self.municipality_id, "id": document_id}
                    )
                )
                .mappings()
                .first()
            )
            if doc is None:
                raise NotFoundError(
                    f"No planning document with id {document_id}",
                    details={"document_id": document_id},
                )
            if doc["file_id"] is None:
                raise ConflictError(
                    "The document has no stored file; upload the PDF and register it first",
                    details={"document_id": document_id, "reason": "no_file"},
                )
            if not force:
                done = await reusable_run(
                    session,
                    municipality_id=self.municipality_id,
                    document_id=document_id,
                    sha256=doc["sha256"],
                    model=self.extraction_model,
                )
                if done is not None:
                    return EnqueuedJob(job=await self.get_job(int(done["job_id"])), created=False)

        async def create_run(session: AsyncSession, job_id: int) -> None:
            await insert_run(
                session,
                municipality_id=self.municipality_id,
                document_id=document_id,
                job_id=job_id,
                model=self.extraction_model,
            )

        return await self._enqueue(
            principal,
            "extract_document",
            target_type="document",
            target_id=document_id,
            payload={"document_id": document_id, "file_id": doc["file_id"]},
            document_id=document_id,
            checksum=doc["sha256"],
            key=run_dedupe_key(document_id, doc["sha256"], self.extraction_model),
            also_on_created=create_run,
        )

    async def enqueue_geo(self, principal: Principal, file_id: int) -> EnqueuedJob:
        async with self.session_factory() as session:
            file_row = (
                (await session.execute(FILE_REF_SQL, {"m": self.municipality_id, "id": file_id}))
                .mappings()
                .first()
            )
        if file_row is None:
            raise NotFoundError(f"No stored file with id {file_id}", details={"file_id": file_id})
        if file_row["kind"] not in (FileKind.gis.value, FileKind.planning_document.value):
            raise ConflictError(
                "The geometry job takes GIS files or vector PDFs; cadastral extracts have "
                "their own import",
                details={"file_id": file_id, "kind": file_row["kind"]},
            )
        return await self._enqueue(
            principal,
            "process_geometry",
            target_type="file",
            target_id=file_id,
            payload={"file_id": file_id, "kind": file_row["kind"]},
            file_id=file_id,
            checksum=file_row["sha256"],
        )

    async def enqueue_preprocess(
        self, principal: Principal, file_id: int, *, force: bool = False
    ) -> EnqueuedJob:
        async with self.session_factory() as session:
            file_row = (
                (await session.execute(FILE_REF_SQL, {"m": self.municipality_id, "id": file_id}))
                .mappings()
                .first()
            )
        if file_row is None:
            raise NotFoundError(f"No stored file with id {file_id}", details={"file_id": file_id})
        if file_row["kind"] != FileKind.planning_document.value:
            raise ConflictError(
                "Pre-processing reads planning-document PDFs",
                details={"file_id": file_id, "kind": file_row["kind"]},
            )
        return await self._enqueue(
            principal,
            "preprocess_file",
            target_type="file",
            target_id=file_id,
            payload={"file_id": file_id, "force": force},
            file_id=file_id,
            checksum=file_row["sha256"],
        )

    async def _enqueue(
        self,
        principal: Principal,
        job_type: str,
        *,
        target_type: str,
        target_id: int,
        payload: dict[str, Any],
        document_id: int | None = None,
        file_id: int | None = None,
        checksum: str | None = None,
        key: str | None = None,
        also_on_created: Callable[[AsyncSession, int], Awaitable[None]] | None = None,
    ) -> EnqueuedJob:
        """One job per target while it is queued / running (``jobs.enqueue``); the audit rows
        are written inside the enqueue transactions (``also_on_created`` too)."""

        async def on_created(session: AsyncSession, job_id: int) -> None:
            if also_on_created is not None:
                await also_on_created(session, job_id)
            await self._audit(
                session,
                principal,
                "job.enqueue",
                "pipeline_job",
                job_id,
                {
                    "type": job_type,
                    "target_type": target_type,
                    "target_id": target_id,
                    "document_id": document_id,
                    "file_id": file_id,
                },
            )

        async def on_dispatch_failed(session: AsyncSession, job_id: int, error: str) -> None:
            await self._audit(
                session,
                principal,
                "job.enqueue_failed",
                "pipeline_job",
                job_id,
                {"type": job_type, "error": error},
            )

        outcome = await enqueue_job(
            self.session_factory,
            self.dispatcher,
            municipality_id=self.municipality_id,
            job_type=job_type,
            payload=payload,
            target_type=target_type,
            target_id=target_id,
            document_id=document_id,
            file_id=file_id,
            checksum=checksum,
            key=key,
            max_attempts=self.max_attempts,
            requested_by=principal.subject,
            requested_by_user_id=principal.user_id,
            on_created=on_created,
            on_dispatch_failed=on_dispatch_failed,
        )
        return EnqueuedJob(job=await self.get_job(outcome.job_id), created=outcome.created)

    async def get_job(self, job_id: int) -> JobOut:
        async with self.session_factory() as session:
            job = (
                await session.execute(JOB_SQL, {"m": self.municipality_id, "id": job_id})
            ).scalar_one_or_none()
        if job is None:
            raise NotFoundError(f"No job with id {job_id}", details={"job_id": job_id})
        return _job_out(job)

    # --- helpers ---------------------------------------------------------------------------------

    async def _exists(self, session: AsyncSession, statement: Any, entity_id: int) -> bool:
        row = await session.execute(statement, {"m": self.municipality_id, "id": entity_id})
        return row.first() is not None
