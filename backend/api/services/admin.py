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

Files of a version (``planning_document_files``, migration 0022): a version has any number of
stored files, each with a role (``text`` = read by the extraction job, ``drawing`` = by the
geometry job, ``both``), and each file is extracted by its own run. ``planning_documents.file_id``
stays the primary file (the first text / both PDF) for the document-level source viewer route.
Files are added to the current version and removed from it until an item read
from them is approved (removal supersedes the file's open items). ``DocumentOut.state`` says where
the version stands: no_files, processing, ready_for_review, failed, published, reviewed,
not_extracted.
"""

from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
import os
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import UploadFile
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from api.schemas.admin import (
    DocumentFileIn,
    DocumentFileOut,
    DocumentIn,
    DocumentList,
    DocumentOut,
    DocumentPatchIn,
    ExtractionRunOut,
    FileKind,
    FileRole,
    FileSummary,
    GeoreferenceOut,
    GeoreferenceSheet,
    ItemCounts,
    JobOut,
    JobStateFilter,
    MunicipalityRef,
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
           COALESCE((SELECT jsonb_agg(pf.document_id ORDER BY pf.document_id)
                     FROM planning_document_files pf WHERE pf.file_id = f.id),
                    '[]'::jsonb) AS document_ids,
           COALESCE((SELECT jsonb_agg({_JOB_JSON} ORDER BY j.requested_at DESC, j.id DESC)
                     FROM (SELECT * FROM pipeline_jobs p WHERE p.file_id = f.id
                           ORDER BY p.requested_at DESC, p.id DESC LIMIT :jobs_limit) j),
                    '[]'::jsonb) AS jobs
    FROM stored_files f
    WHERE f.municipality_id = :m {extra}
    ORDER BY f.uploaded_at DESC, f.id DESC
    LIMIT :limit OFFSET :offset
    """


# The extraction state of one file of a version, from its latest run (``lr``) and that run's job
# (``xj``): the job decides while it exists (a manual retry re-queues a failed run's job; a job
# whose message never reached the broker failed while its run still says queued).
_EXTRACTION_STATE = """CASE WHEN lr.status IS NULL THEN 'none'
             WHEN lr.status = 'ready_for_review' THEN 'ready_for_review'
             WHEN xj.status = 'retrying' THEN 'retrying'
             WHEN xj.status = 'queued' THEN 'queued'
             WHEN xj.status = 'running' THEN 'extracting'
             WHEN xj.status IN ('failed', 'cancelled') THEN 'failed'
             ELSE lr.status END"""
_ACTIVE_EXTRACTION = ("queued", "extracting", "retrying")

# One row per document version: its files in display order, each with its latest extraction run
# over this version (and the run's job), its latest geometry job, the review items read from it
# and the served values citing it; plus the flags the document state is made of.
_FILES_SQL = f"""
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
               'file_id', pf.file_id, 'role', pf.role, 'position', pf.position,
               'is_primary', COALESCE(pf.file_id = d.file_id, false),
               'kind', sf.kind, 'original_filename', sf.original_filename,
               'mime_type', sf.mime_type, 'size_bytes', sf.size_bytes, 'sha256', sf.sha256,
               'page_count', sf.page_count, 'uploaded_at', sf.uploaded_at,
               'added_by', pf.added_by, 'added_at', pf.added_at,
               'preprocessing', sf.preprocess -> 'summary',
               'extraction_state', xs.state,
               'extraction', (SELECT {RUN_JSON} FROM extraction_runs r WHERE r.id = lr.id),
               'extraction_job', (SELECT {_JOB_JSON} FROM pipeline_jobs j WHERE j.id = xj.id),
               'geometry_job', (SELECT {_JOB_JSON} FROM pipeline_jobs j WHERE j.id = gj.id),
               'items', jsonb_build_object(
                   'pending', ic.pending, 'approved', ic.approved, 'amended', ic.amended,
                   'rejected', ic.rejected, 'published', ic.published, 'total', ic.total),
               'values_cited', pv.n)
             ORDER BY pf.position, pf.id), '[]'::jsonb) AS files,
           count(pf.id) AS file_count,
           COALESCE(bool_or(xs.state IN ('queued', 'extracting', 'retrying')), false)
               AS extracting,
           COALESCE(bool_or(xs.state = 'failed'), false) AS extraction_failed,
           COALESCE(bool_or(xs.state = 'ready_for_review'), false) AS extraction_ready,
           array_remove(array_agg(xj.status) || array_agg(gj.status), NULL) AS job_states
    FROM planning_document_files pf
    JOIN stored_files sf ON sf.id = pf.file_id
    LEFT JOIN LATERAL (
        SELECT r.id, r.status, r.job_id FROM extraction_runs r
        WHERE r.municipality_id = d.municipality_id AND r.document_id = d.id
          AND r.file_id = pf.file_id
        ORDER BY r.id DESC LIMIT 1) lr ON true
    LEFT JOIN pipeline_jobs xj ON xj.id = lr.job_id
    CROSS JOIN LATERAL (SELECT {_EXTRACTION_STATE} AS state) xs
    LEFT JOIN LATERAL (
        SELECT g.id, g.status FROM pipeline_jobs g
        WHERE g.municipality_id = d.municipality_id AND g.file_id = pf.file_id
          AND g.type = 'process_geometry'
        ORDER BY g.requested_at DESC, g.id DESC LIMIT 1) gj ON true
    CROSS JOIN LATERAL (
        SELECT count(*) FILTER (WHERE e.review_state = 'pending_review') AS pending,
               count(*) FILTER (WHERE e.review_state = 'approved') AS approved,
               count(*) FILTER (WHERE e.review_state = 'amended') AS amended,
               count(*) FILTER (WHERE e.review_state = 'rejected') AS rejected,
               count(*) FILTER (WHERE e.published_value_id IS NOT NULL) AS published,
               count(*) AS total
        FROM planning_parameter_extractions e
        JOIN extraction_runs er ON er.id = e.run_id
        WHERE e.document_id = d.id AND er.file_id = pf.file_id
          AND e.superseded_at IS NULL) ic
    CROSS JOIN LATERAL (
        SELECT count(*) AS n FROM planning_parameter_values v
        WHERE v.document_id = d.id AND v.source_file_id = pf.file_id) pv
    WHERE pf.document_id = d.id
"""
_REVIEW_SQL = """
    SELECT count(*) FILTER (WHERE e.review_state = 'pending_review') AS pending,
           count(*) FILTER (WHERE e.review_state = 'approved') AS approved,
           count(*) FILTER (WHERE e.review_state = 'amended') AS amended,
           count(*) FILTER (WHERE e.review_state = 'rejected') AS rejected,
           count(*) AS total,
           count(*) FILTER (WHERE e.review_state IN ('approved', 'amended')
                            AND e.published_value_id IS NULL) AS unpublished
    FROM planning_parameter_extractions e
    WHERE e.document_id = d.id AND e.superseded_at IS NULL
"""
# First match wins (the DocumentState vocabulary in api.schemas.admin).
_STATE_SQL = """
    SELECT CASE WHEN fa.file_count = 0 THEN 'no_files'
                WHEN fa.extracting THEN 'processing'
                WHEN rv.pending > 0 THEN 'ready_for_review'
                WHEN fa.extraction_failed THEN 'failed'
                WHEN rv.approved + rv.amended > 0 AND rv.unpublished = 0 THEN 'published'
                WHEN rv.total > 0 THEN 'reviewed'
                WHEN fa.extraction_ready THEN 'ready_for_review'
                ELSE 'not_extracted' END AS state
"""
# The per-file job filter: the latest extraction and geometry job statuses of any file.
JOB_STATE_FILTERS: dict[str, tuple[str, ...]] = {
    "queued": ("queued",),
    "running": ("running", "retrying"),
    "succeeded": ("succeeded",),
    "failed": ("failed", "cancelled"),
}


# the latest georeferencing run of a document version (core.gis.georef, migration 0025)
_GEOREF_JSON = """
    jsonb_build_object(
        'dataset_version', g.dataset_version, 'status', g.status, 'source', g.source,
        'crs', g.crs, 'method', g.method, 'rmse_m', g.rmse_m,
        'max_rmse_m', g.transform->'max_rmse_m', 'max_residual_m', g.max_residual_m,
        'points_used', g.points_used, 'sheets', g.sheets, 'snap', g.snap,
        'validation', g.validation, 'created_at', g.created_at, 'published_at', g.published_at)
"""


def _document_sql(extra: str) -> str:
    return f"""
    SELECT d.id, d.municipality_id, d.name, d.short_code, d.type, d.status::text AS status,
           d.source, d.source_url, d.zone_id,
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
           (SELECT {_GEOREF_JSON} FROM georef_datasets g WHERE g.document_id = d.id
            ORDER BY g.id DESC LIMIT 1) AS georeference,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
                         'id', v.id, 'version', v.version, 'status', v.status::text,
                         'is_current_version', v.is_current_version, 'name', v.name,
                         'registered_by', v.registered_by, 'registered_at', v.registered_at,
                         'file_count', (SELECT count(*) FROM planning_document_files vf
                                        WHERE vf.document_id = v.id))
                         ORDER BY v.version, v.id)
                     FROM planning_documents v
                     WHERE COALESCE(v.lineage_id, v.id) = COALESCE(d.lineage_id, d.id)),
                    '[]'::jsonb) AS versions,
           COALESCE((SELECT jsonb_agg({_JOB_JSON} ORDER BY j.requested_at DESC, j.id DESC)
                     FROM (SELECT * FROM pipeline_jobs p WHERE p.document_id = d.id
                           ORDER BY p.requested_at DESC, p.id DESC LIMIT :jobs_limit) j),
                    '[]'::jsonb) AS jobs,
           jsonb_build_object('pending', rv.pending, 'approved', rv.approved,
                              'amended', rv.amended, 'rejected', rv.rejected,
                              'total', rv.total) AS review,
           fa.files, st.state, count(*) OVER () AS total
    FROM planning_documents d
    LEFT JOIN zones z ON z.id = d.zone_id
    LEFT JOIN stored_files f ON f.id = d.file_id
    CROSS JOIN LATERAL ({_REVIEW_SQL}) rv
    CROSS JOIN LATERAL ({_FILES_SQL}) fa
    CROSS JOIN LATERAL ({_STATE_SQL}) st
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
           d.coverage_live, d.zone_id, d.short_code,
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
        municipality_id, name, short_code, type, status, source, source_url, zone_id,
        amends_document_id,
        coverage_geom, file_id, file_key, page_count, lineage_id, version, is_current_version,
        licence_note, adopted_on, registered_by, registered_at, dataset_version)
    VALUES (
        :m, :name, :short_code, :type, CAST(:status AS planning_document_status), :source,
        :source_url,
        :zone_id, :amends_document_id,
        (SELECT p.coverage_geom FROM planning_documents p WHERE p.id = :previous_id),
        :file_id, :file_key, :page_count, :lineage_id, :version, true, :licence_note,
        CAST(:adopted_on AS date), :registered_by, :registered_at, NULL)
    RETURNING id
    """
)
SET_LINEAGE_SQL = text("UPDATE planning_documents SET lineage_id = id WHERE id = :id")
DOCUMENT_FOR_JOB_SQL = text(
    """
    SELECT d.id, d.file_id AS primary_file_id, pf.file_id, pf.role, sf.sha256, sf.kind
    FROM planning_documents d
    LEFT JOIN planning_document_files pf ON pf.document_id = d.id
    LEFT JOIN stored_files sf ON sf.id = pf.file_id
    WHERE d.id = :id AND d.municipality_id = :m
    ORDER BY pf.position, pf.id
    """
)
DOCUMENT_LOCK_SQL = text(
    "SELECT id, is_current_version FROM planning_documents "
    "WHERE id = :id AND municipality_id = :m FOR UPDATE"
)
DOCUMENT_FILES_SQL = text(
    """
    SELECT pf.file_id, pf.role, sf.kind, sf.object_key, sf.page_count
    FROM planning_document_files pf JOIN stored_files sf ON sf.id = pf.file_id
    WHERE pf.document_id = :id
    ORDER BY pf.position, pf.id
    """
)
NEXT_POSITION_SQL = text(
    "SELECT COALESCE(max(position) + 1, 0) FROM planning_document_files WHERE document_id = :id"
)
INSERT_LINK_SQL = text(
    """
    INSERT INTO planning_document_files (municipality_id, document_id, file_id, role, position,
                                         added_by, added_by_user_id, added_at)
    VALUES (:m, :document_id, :file_id, :role, :position, :added_by, :added_by_user_id, :at)
    ON CONFLICT (document_id, file_id) DO NOTHING
    RETURNING file_id
    """
)
LINK_SQL = text(
    """
    SELECT pf.role, sf.kind FROM planning_document_files pf
    JOIN stored_files sf ON sf.id = pf.file_id
    WHERE pf.document_id = :document_id AND pf.file_id = :file_id
    FOR UPDATE OF pf
    """
)
SET_ROLE_SQL = text(
    "UPDATE planning_document_files SET role = :role "
    "WHERE document_id = :document_id AND file_id = :file_id"
)
DELETE_LINK_SQL = text(
    "DELETE FROM planning_document_files WHERE document_id = :document_id AND file_id = :file_id"
)
# The version's primary file follows its files (the first text / both PDF).
SET_PRIMARY_SQL = text(
    """
    UPDATE planning_documents
    SET file_id = CAST(:file_id AS bigint), file_key = CAST(:file_key AS text),
        page_count = CAST(:page_count AS integer)
    WHERE id = :id AND file_id IS DISTINCT FROM CAST(:file_id AS bigint)
    """
)
# Why a file cannot leave its version: items read from it were accepted, served values cite it,
# or its extraction is still running.
DETACH_STATE_SQL = text(
    f"""
    SELECT
      (SELECT count(*) FROM planning_parameter_extractions e
       JOIN extraction_runs r ON r.id = e.run_id
       WHERE e.document_id = :document_id AND r.file_id = :file_id
         AND e.superseded_at IS NULL
         AND (e.review_state IN ('approved', 'amended') OR e.published_value_id IS NOT NULL))
          AS accepted,
      (SELECT count(*) FROM planning_parameter_values v
       WHERE v.document_id = :document_id AND v.source_file_id = :file_id) AS cited,
      EXISTS (SELECT 1 FROM (SELECT r.status, r.job_id FROM extraction_runs r
                             WHERE r.document_id = :document_id AND r.file_id = :file_id
                             ORDER BY r.id DESC LIMIT 1) lr
              LEFT JOIN pipeline_jobs xj ON xj.id = lr.job_id
              WHERE ({_EXTRACTION_STATE}) IN ('queued', 'extracting', 'retrying')) AS active
    """
)
SUPERSEDE_FILE_ITEMS_SQL = text(
    """
    UPDATE planning_parameter_extractions e
    SET superseded_at = :at
    FROM extraction_runs r
    WHERE r.id = e.run_id AND e.document_id = :document_id AND r.file_id = :file_id
      AND e.superseded_at IS NULL AND e.published_value_id IS NULL
    RETURNING e.id
    """
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
SHORT_CODE_TAKEN_SQL = text(
    """
    SELECT id, name FROM planning_documents
    WHERE municipality_id = :m AND is_current_version AND lower(short_code) = lower(:code)
      AND COALESCE(lineage_id, id) <> :lineage
    LIMIT 1
    """
)
DOCUMENT_EDIT_SQL = text(
    """
    SELECT d.id, COALESCE(d.lineage_id, d.id) AS lineage_id, d.is_current_version, d.name,
           d.short_code, d.status::text AS status, d.zone_id, d.source, d.source_url,
           d.adopted_on, d.licence_note
    FROM planning_documents d
    WHERE d.id = :id AND d.municipality_id = :m
    FOR UPDATE
    """
)
# the columns PATCH may set, in the order they are written (status is the enum)
EDITABLE = (
    "name",
    "short_code",
    "status",
    "zone_id",
    "source",
    "source_url",
    "adopted_on",
    "licence_note",
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
        municipality_id=row["municipality_id"],
        name=row["name"],
        short_code=row.get("short_code"),
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
        files=[_document_file_out(f) for f in row.get("files") or []],
        state=row.get("state") or "no_files",
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
        georeference=_georeference(row.get("georeference")),
    )


def _georeference(raw: Mapping[str, Any] | None) -> GeoreferenceOut | None:
    if not raw:
        return None
    snap = raw.get("snap") or {}
    validation = raw.get("validation") or {}
    return GeoreferenceOut(
        dataset_version=raw["dataset_version"],
        status=raw["status"],
        source=raw["source"],
        crs=raw["crs"],
        method=raw["method"],
        rmse_m=raw["rmse_m"],
        max_rmse_m=raw.get("max_rmse_m"),
        max_residual_m=raw.get("max_residual_m"),
        points_used=raw["points_used"],
        sheets=[GeoreferenceSheet(**s) for s in raw.get("sheets") or []],
        snap_tolerance_m=snap.get("tolerance_m"),
        snapped_vertices=snap.get("snapped_vertices"),
        snapped_ratio=snap.get("snapped_ratio"),
        near_misses=snap.get("near_misses"),
        cadastral_overlap_share=(snap.get("overlap") or {}).get("overlap_share"),
        systematic_offset_m=snap.get("systematic_offset_m"),
        errors=[e["code"] for e in validation.get("errors") or []],
        warnings=[w["code"] for w in validation.get("warnings") or []],
        created_at=raw["created_at"],
        published_at=raw.get("published_at"),
    )


def _redraw_pages(preprocessing: PreprocessSummary | None) -> list[int] | None:
    """Pages to redraw in QGIS (the week-1 assessment's class C); a manifest of an older
    pre-processing version names its scanned pages (a subset: every one is a raster sheet)."""
    if preprocessing is None:
        return None
    if preprocessing.redraw_pages is not None:
        return list(preprocessing.redraw_pages)
    return list(preprocessing.scanned_pages)


def _plain(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value


def _short_code(value: str | None) -> str | None:
    return " ".join(value.split()) or None if value is not None else None


def _document_file_out(raw: Mapping[str, Any]) -> DocumentFileOut:
    """One file of a version as the admin screens show it (``_FILES_SQL``)."""
    counts = raw.get("items") or {}
    items = ItemCounts(**{k: int(counts.get(k) or 0) for k in ItemCounts.model_fields})
    extraction = _extraction(raw.get("extraction"))
    extraction_job = _job_out(raw["extraction_job"]) if raw.get("extraction_job") else None
    geometry_job = _job_out(raw["geometry_job"]) if raw.get("geometry_job") else None
    state = raw.get("extraction_state") or "none"
    if items.approved or items.amended or items.published:
        blocker: str | None = "items_accepted"
    elif int(raw.get("values_cited") or 0):
        blocker = "values_published"
    elif state in _ACTIVE_EXTRACTION:
        blocker = "extraction_active"
    else:
        blocker = None
    error = None
    if state == "failed":
        error = (extraction.error if extraction else None) or (
            extraction_job.error if extraction_job else None
        )
    preprocessing = _preprocessing(raw.get("preprocessing"))
    return DocumentFileOut(
        file_id=raw["file_id"],
        role=raw["role"],
        position=raw["position"],
        is_primary=bool(raw.get("is_primary")),
        kind=raw["kind"],
        original_filename=raw["original_filename"],
        mime_type=raw["mime_type"],
        size_bytes=raw["size_bytes"],
        sha256=raw["sha256"],
        page_count=raw.get("page_count"),
        scanned_pages=list(preprocessing.scanned_pages) if preprocessing else None,
        redraw_pages=_redraw_pages(preprocessing),
        uploaded_at=raw["uploaded_at"],
        added_by=raw.get("added_by"),
        added_at=raw["added_at"],
        preprocessing=preprocessing,
        extraction_state=state,
        extraction_error=error,
        extraction=extraction,
        extraction_job=extraction_job,
        geometry_job=geometry_job,
        items=items,
        can_remove=blocker is None,
        remove_blocker=blocker,
    )


@dataclass(frozen=True, slots=True)
class _DocFile:
    """A stored file on its way onto a version (checked by ``AdminService._checked_files``)."""

    file_id: int
    role: str
    kind: str
    object_key: str | None
    page_count: int | None


def _primary(files: Sequence[_DocFile]) -> _DocFile | None:
    """The version's primary file: the first text / both PDF, else the first PDF."""
    pdfs = [f for f in files if f.kind == FileKind.planning_document.value]
    return next((f for f in pdfs if f.role != "drawing"), pdfs[0] if pdfs else None)


def _extraction_file(
    document_id: int, rows: Sequence[Mapping[str, Any]], file_id: int | None
) -> Mapping[str, Any]:
    """The file an extraction reads: the one asked for (a PDF of the version that is not a
    drawing), or by default the primary file when it is read as text, else the first text / both
    PDF."""
    details: dict[str, Any] = {"document_id": document_id}
    files = [r for r in rows if r["file_id"] is not None]
    if file_id is None:
        readable = [
            r
            for r in files
            if r["kind"] == FileKind.planning_document.value and r["role"] != "drawing"
        ]
        primary = rows[0]["primary_file_id"]
        chosen = next((r for r in readable if r["file_id"] == primary), None) or next(
            iter(readable), None
        )
        if chosen is None:
            raise ConflictError(
                "The document has no file to extract; upload the PDF and add it to the document "
                "first",
                details={**details, "reason": "no_file"},
            )
        return chosen
    details["file_id"] = file_id
    chosen = next((r for r in files if r["file_id"] == file_id), None)
    if chosen is None:
        raise NotFoundError(
            f"File {file_id} is not a file of document {document_id}", details=details
        )
    if chosen["kind"] != FileKind.planning_document.value:
        raise ConflictError(
            "Only PDFs are extracted; a GIS file goes to the geometry job",
            details={**details, "reason": "not_a_pdf"},
        )
    if chosen["role"] == "drawing":
        raise ConflictError(
            "The file is a drawing (read by the geometry job); set its role to text or both to "
            "extract it",
            details={**details, "reason": "drawing_file"},
        )
    return chosen


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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
            files = await self._checked_files(
                session, payload.files, problems, legacy_file_id=payload.file_id
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
            short_code = _short_code(payload.short_code)
            if short_code is None and previous is not None:
                short_code = previous["short_code"]
            await self._check_short_code(
                session, short_code, previous["lineage_id"] if previous is not None else None
            )

            primary = _primary(files)
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
                        "short_code": short_code,
                        "type": doc_type,
                        "status": payload.status,
                        "source": payload.source,
                        "source_url": payload.source_url,
                        "zone_id": payload.zone_id
                        if payload.zone_id is not None
                        else (previous["zone_id"] if previous is not None else None),
                        "amends_document_id": payload.amends_document_id,
                        "previous_id": previous["id"] if previous is not None else None,
                        "file_id": primary.file_id if primary is not None else None,
                        "file_key": primary.object_key if primary is not None else None,
                        "page_count": primary.page_count if primary is not None else None,
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
            await self._link_files(session, principal, int(new_id), files, now)
            await self._audit(
                session,
                principal,
                "document.register",
                "planning_document",
                new_id,
                {
                    "name": payload.name.strip(),
                    "short_code": short_code,
                    "type": doc_type,
                    "status": payload.status,
                    "file_id": primary.file_id if primary is not None else None,
                    "files": [{"file_id": f.file_id, "role": f.role} for f in files],
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
        zone_id: int | None = None,
        state: str | None = None,
        job_state: JobStateFilter | None = None,
        q: str | None = None,
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
        if zone_id is not None:
            clauses.append("AND d.zone_id = :zone_id")
            params["zone_id"] = zone_id
        if state is not None:
            clauses.append("AND st.state = :state")
            params["state"] = state
        if job_state is not None:
            clauses.append("AND fa.job_states && CAST(:job_states AS text[])")
            params["job_states"] = list(JOB_STATE_FILTERS[job_state])
        if q is not None and q.strip():
            clauses.append("AND d.name ILIKE :q ESCAPE '\\'")
            params["q"] = _like(q.strip())
        async with self.session_factory() as session:
            rows = (
                (await session.execute(text(_document_sql(" ".join(clauses))), params))
                .mappings()
                .all()
            )
        return DocumentList(
            municipality=MunicipalityRef(id=self.municipality.id, name=self.municipality.name),
            items=[_document_out(r) for r in rows],
            total=int(rows[0]["total"]) if rows else 0,
            limit=limit,
            offset=offset,
        )

    async def _check_short_code(
        self, session: AsyncSession, code: str | None, lineage_id: int | None
    ) -> None:
        """A short code names one document: no other current document may carry it (its own
        versions share it)."""
        if code is None:
            return
        taken = (
            (
                await session.execute(
                    SHORT_CODE_TAKEN_SQL,
                    {"m": self.municipality_id, "code": code, "lineage": lineage_id or -1},
                )
            )
            .mappings()
            .first()
        )
        if taken is not None:
            raise ConflictError(
                f"The short code {code} already names {taken['name']}",
                details={"reason": "short_code_taken", "document_id": taken["id"]},
            )

    async def update_document(
        self, principal: Principal, document_id: int, patch: DocumentPatchIn
    ) -> DocumentOut:
        """Change the facts of the current version (status, name, short code, zone, source, link,
        adoption date, licence note). Only what differs is written, with one ``document.update``
        audit row holding the before and after of the changed fields."""
        async with self.session_factory() as session:
            row = (
                (
                    await session.execute(
                        DOCUMENT_EDIT_SQL, {"m": self.municipality_id, "id": document_id}
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFoundError(
                    f"No planning document with id {document_id}",
                    details={"document_id": document_id},
                )
            if not row["is_current_version"]:
                raise ConflictError(
                    "Only the current version of a document can be edited",
                    details={"document_id": document_id, "reason": "not_current_version"},
                )
            wanted: dict[str, Any] = {}
            for key in EDITABLE:
                if key not in patch.model_fields_set:
                    continue
                value = getattr(patch, key)
                if isinstance(value, str):
                    value = _short_code(value) if key == "short_code" else (value.strip() or None)
                wanted[key] = value
            changes = {k: v for k, v in wanted.items() if v != row[k]}
            if changes.get("zone_id") is not None and not await self._exists(
                session, ZONE_EXISTS_SQL, changes["zone_id"]
            ):
                raise _validation_error([{"loc": ["body", "zone_id"], "msg": "no such zone"}])
            if changes.get("short_code") is not None:
                await self._check_short_code(session, changes["short_code"], row["lineage_id"])
            if changes:
                sets = ", ".join(
                    "status = CAST(:status AS planning_document_status)"
                    if key == "status"
                    else f"{key} = :{key}"
                    for key in changes
                )
                await session.execute(
                    text(f"UPDATE planning_documents SET {sets} WHERE id = :id"),
                    {**changes, "id": document_id},
                )
                await self._audit(
                    session,
                    principal,
                    "document.update",
                    "planning_document",
                    document_id,
                    {"fields": sorted(changes)},
                    before={k: _plain(row[k]) for k in changes},
                    after={k: _plain(v) for k, v in changes.items()},
                )
                await session.commit()
        return await self.get_document(document_id)

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

    # --- files of a version -----------------------------------------------------------------------

    async def attach_files(
        self, principal: Principal, document_id: int, entries: Sequence[DocumentFileIn]
    ) -> tuple[DocumentOut, list[int]]:
        """Put stored files on the current version of a document. Idempotent: a file already on
        it stays as it is (role included); the ids actually added come back with the document."""
        async with self.session_factory() as session:
            await self._lock_current(session, document_id)
            problems: list[dict[str, Any]] = []
            files = await self._checked_files(session, entries, problems)
            if problems:
                raise _validation_error(problems)
            added = await self._link_files(session, principal, document_id, files, self.clock())
            if added:
                await self._sync_primary(session, document_id)
                await self._audit(
                    session,
                    principal,
                    "document.file_attach",
                    "planning_document",
                    document_id,
                    {
                        "files": [
                            {"file_id": f.file_id, "role": f.role}
                            for f in files
                            if f.file_id in added
                        ]
                    },
                )
            await session.commit()
        return await self.get_document(document_id), added

    async def set_file_role(
        self, principal: Principal, document_id: int, file_id: int, role: FileRole
    ) -> DocumentOut:
        """What a file of the current version is read for: text, drawing or both."""
        async with self.session_factory() as session:
            await self._lock_current(session, document_id)
            link = await self._link(session, document_id, file_id)
            if link["kind"] == FileKind.gis.value and role != "drawing":
                raise _validation_error(
                    [
                        {
                            "loc": ["body", "role"],
                            "msg": "a GIS file is read by the geometry job only: its role must "
                            "be drawing",
                        }
                    ]
                )
            if link["role"] != role:
                await session.execute(
                    SET_ROLE_SQL, {"document_id": document_id, "file_id": file_id, "role": role}
                )
                await self._sync_primary(session, document_id)
                await self._audit(
                    session,
                    principal,
                    "document.file_role",
                    "planning_document",
                    document_id,
                    {"file_id": file_id},
                    before={"role": link["role"]},
                    after={"role": role},
                )
            await session.commit()
        return await self.get_document(document_id)

    async def detach_file(
        self, principal: Principal, document_id: int, file_id: int
    ) -> DocumentOut:
        """Take a file off the current version. Refused (409) once an item read from it was
        approved or amended, while served values cite it and while its extraction runs; its
        other open items are superseded (they leave the review queue). The stored file stays."""
        async with self.session_factory() as session:
            await self._lock_current(session, document_id)
            link = await self._link(session, document_id, file_id)
            params = {"document_id": document_id, "file_id": file_id}
            state = (await session.execute(DETACH_STATE_SQL, params)).mappings().one()
            details = {"document_id": document_id, "file_id": file_id}
            if state["accepted"]:
                raise ConflictError(
                    "Items read from this file were approved; it can no longer be removed",
                    details={**details, "reason": "items_accepted", "items": state["accepted"]},
                )
            if state["cited"]:
                raise ConflictError(
                    "Published values cite this file; it can no longer be removed",
                    details={**details, "reason": "values_published", "values": state["cited"]},
                )
            if state["active"]:
                raise ConflictError(
                    "The file is being extracted; remove it once the job has finished",
                    details={**details, "reason": "extraction_active"},
                )
            superseded = (
                await session.execute(SUPERSEDE_FILE_ITEMS_SQL, {**params, "at": self.clock()})
            ).all()
            await session.execute(DELETE_LINK_SQL, params)
            await self._sync_primary(session, document_id)
            await self._audit(
                session,
                principal,
                "document.file_detach",
                "planning_document",
                document_id,
                {"file_id": file_id, "role": link["role"], "items_superseded": len(superseded)},
            )
            await session.commit()
        return await self.get_document(document_id)

    async def _lock_current(self, session: AsyncSession, document_id: int) -> None:
        row = (
            (
                await session.execute(
                    DOCUMENT_LOCK_SQL, {"m": self.municipality_id, "id": document_id}
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFoundError(
                f"No planning document with id {document_id}",
                details={"document_id": document_id},
            )
        if not row["is_current_version"]:
            raise ConflictError(
                "Only the current version of a document takes file changes",
                details={"document_id": document_id, "reason": "not_current_version"},
            )

    async def _link(
        self, session: AsyncSession, document_id: int, file_id: int
    ) -> Mapping[str, Any]:
        link = (
            (await session.execute(LINK_SQL, {"document_id": document_id, "file_id": file_id}))
            .mappings()
            .first()
        )
        if link is None:
            raise NotFoundError(
                f"File {file_id} is not a file of document {document_id}",
                details={"document_id": document_id, "file_id": file_id},
            )
        return link

    async def _checked_files(
        self,
        session: AsyncSession,
        entries: Sequence[DocumentFileIn],
        problems: list[dict[str, Any]],
        *,
        legacy_file_id: int | None = None,
    ) -> list[_DocFile]:
        """The stored files to put on a version: planning-document PDFs in any role, GIS files as
        drawings only. Problems are appended for the caller's 422 (``file_id`` of the single-file
        form keeps its own location and wording)."""
        checked: list[_DocFile] = []
        for index, entry in enumerate(entries):
            legacy = legacy_file_id is not None and entry.file_id == legacy_file_id
            loc: list[Any] = ["body", "file_id"] if legacy else ["body", "files", index, "file_id"]
            row = (
                (
                    await session.execute(
                        FILE_REF_SQL, {"m": self.municipality_id, "id": entry.file_id}
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                problems.append({"loc": loc, "msg": "no such stored file"})
                continue
            kind = row["kind"]
            if kind == FileKind.planning_document.value or (
                kind == FileKind.gis.value and entry.role == "drawing"
            ):
                checked.append(
                    _DocFile(int(row["id"]), entry.role, kind, row["object_key"], row["page_count"])
                )
            elif kind == FileKind.gis.value and not legacy:
                problems.append(
                    {
                        "loc": ["body", "files", index, "role"],
                        "msg": "a GIS file is read by the geometry job only: its role must be "
                        "drawing",
                    }
                )
            else:
                problems.append(
                    {
                        "loc": loc,
                        "msg": f"the file is a {kind}; a planning_document PDF is required "
                        "(GIS files only as drawings)",
                    }
                )
        return checked

    async def _link_files(
        self,
        session: AsyncSession,
        principal: Principal,
        document_id: int,
        files: Sequence[_DocFile],
        at: datetime,
    ) -> list[int]:
        position = int((await session.execute(NEXT_POSITION_SQL, {"id": document_id})).scalar_one())
        added: list[int] = []
        for f in files:
            new = (
                await session.execute(
                    INSERT_LINK_SQL,
                    {
                        "m": self.municipality_id,
                        "document_id": document_id,
                        "file_id": f.file_id,
                        "role": f.role,
                        "position": position,
                        "added_by": principal.subject,
                        "added_by_user_id": principal.user_id,
                        "at": at,
                    },
                )
            ).scalar_one_or_none()
            if new is not None:
                added.append(int(new))
                position += 1
        return added

    async def _sync_primary(self, session: AsyncSession, document_id: int) -> None:
        rows = (await session.execute(DOCUMENT_FILES_SQL, {"id": document_id})).mappings().all()
        primary = _primary(
            [
                _DocFile(int(r["file_id"]), r["role"], r["kind"], r["object_key"], r["page_count"])
                for r in rows
            ]
        )
        await session.execute(
            SET_PRIMARY_SQL,
            {
                "id": document_id,
                "file_id": primary.file_id if primary is not None else None,
                "file_key": primary.object_key if primary is not None else None,
                "page_count": primary.page_count if primary is not None else None,
            },
        )

    # --- jobs ------------------------------------------------------------------------------------

    async def enqueue_extract(
        self,
        principal: Principal,
        document_id: int,
        *,
        file_id: int | None = None,
        force: bool = False,
    ) -> EnqueuedJob:
        """Queue one extraction run over a file of the document version (default: its primary
        text file). Idempotent: the run that already read the same file with the same model,
        prompt and schema versions answers (unless ``force``), and an identical job still queued /
        running is returned as it is."""
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        DOCUMENT_FOR_JOB_SQL, {"m": self.municipality_id, "id": document_id}
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                raise NotFoundError(
                    f"No planning document with id {document_id}",
                    details={"document_id": document_id},
                )
            chosen = _extraction_file(document_id, rows, file_id)
            if not force:
                done = await reusable_run(
                    session,
                    municipality_id=self.municipality_id,
                    document_id=document_id,
                    sha256=chosen["sha256"],
                    model=self.extraction_model,
                )
                if done is not None:
                    return EnqueuedJob(job=await self.get_job(int(done["job_id"])), created=False)
        target_file = int(chosen["file_id"])

        async def create_run(session: AsyncSession, job_id: int) -> None:
            await insert_run(
                session,
                municipality_id=self.municipality_id,
                document_id=document_id,
                job_id=job_id,
                model=self.extraction_model,
                file_id=target_file,
            )

        return await self._enqueue(
            principal,
            "extract_document",
            target_type="document",
            target_id=document_id,
            payload={"document_id": document_id, "file_id": target_file},
            document_id=document_id,
            file_id=target_file,
            checksum=chosen["sha256"],
            key=run_dedupe_key(document_id, chosen["sha256"], self.extraction_model),
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

    async def enqueue_zone_import(
        self, principal: Principal, file_id: int, *, dry_run: bool = False
    ) -> EnqueuedJob:
        """Queue the zone import of a GeoPackage drawn in QGIS (``import_zones``: the worker
        validates it with ``core.zones`` and stages the zones and their documents; the publish
        job applies them)."""
        async with self.session_factory() as session:
            file_row = (
                (await session.execute(FILE_REF_SQL, {"m": self.municipality_id, "id": file_id}))
                .mappings()
                .first()
            )
        if file_row is None:
            raise NotFoundError(f"No stored file with id {file_id}", details={"file_id": file_id})
        key = str(file_row["object_key"] or "").lower()
        if file_row["kind"] != FileKind.gis.value or not key.endswith(".gpkg"):
            raise ConflictError(
                "Zones are imported from the GeoPackage drawn in QGIS (a .gpkg GIS file)",
                details={"file_id": file_id, "reason": "not_a_geopackage"},
            )
        mode = "dry_run" if dry_run else "stage"
        return await self._enqueue(
            principal,
            "import_zones",
            target_type="file",
            target_id=file_id,
            payload={"file_id": file_id, "dry_run": dry_run},
            file_id=file_id,
            key=f"import_zones:file:{file_id}:sha256:{file_row['sha256']}:{mode}",
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
