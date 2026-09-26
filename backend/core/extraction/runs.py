"""Extraction runs (``extraction_runs``, migration 0020): the statements the API (enqueueing
``extract_document``) and the worker (``jobs.extraction_runner``) share. Import-light.

**Idempotency.** A run is identified by the document version, its file's SHA-256, the configured
model and the prompt and schema versions. The API returns the run that already reached
``ready_for_review`` for that key instead of queueing another (the same content read the same way
twice gives nothing new); the job's dedupe key (:func:`run_dedupe_key`) makes an identical request
while one is queued / running return that job. A newer prompt, schema or model, or another file,
is a new run; the new run's items point at the previous ones and supersede what they replace.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.extraction.prompts import PROMPT_VERSION
from core.extraction.schema import SCHEMA_VERSION

RUN_STATUSES: tuple[str, ...] = ("queued", "extracting", "ready_for_review", "failed")


def run_dedupe_key(
    document_id: int,
    sha256: str,
    model: str,
    *,
    prompt_version: str = PROMPT_VERSION,
    schema_version: str = SCHEMA_VERSION,
) -> str:
    """The ``pipeline_jobs.dedupe_key`` of an extraction: one active job per key."""
    return (
        f"extract_document:document:{document_id}:sha256:{sha256}:model:{model}"
        f":prompt:{prompt_version}:schema:{schema_version}"
    )


REUSABLE_RUN_SQL = text(
    """
    SELECT id, job_id FROM extraction_runs
    WHERE municipality_id = :m AND document_id = :document_id AND file_sha256 = :sha256
      AND model = :model AND prompt_version = :prompt_version
      AND schema_version = :schema_version AND status = 'ready_for_review'
      AND job_id IS NOT NULL
    ORDER BY id DESC LIMIT 1
    """
)
INSERT_RUN_SQL = text(
    """
    INSERT INTO extraction_runs (municipality_id, document_id, lineage_id, file_id, file_sha256,
                                 model, prompt_version, schema_version, status, job_id,
                                 requested_by, requested_by_user_id)
    SELECT d.municipality_id, d.id, COALESCE(d.lineage_id, d.id), d.file_id, f.sha256, :model,
           :prompt_version, :schema_version, 'queued', :job_id,
           COALESCE(j.requested_by, 'worker'), j.requested_by_user_id
    FROM planning_documents d
    JOIN stored_files f ON f.id = d.file_id
    LEFT JOIN pipeline_jobs j ON j.id = :job_id
    WHERE d.municipality_id = :m AND d.id = :document_id
    RETURNING id
    """
)
# What admins see of a run (job detail, document and file records).
RUN_JSON = """jsonb_build_object(
    'id', r.id, 'status', r.status, 'document_id', r.document_id, 'file_id', r.file_id,
    'file_sha256', r.file_sha256, 'model', r.model, 'model_version', r.model_version,
    'prompt_version', r.prompt_version, 'schema_version', r.schema_version,
    'pages_total', r.pages_total, 'pages_processed', jsonb_array_length(r.pages_processed),
    'pages_skipped', r.pages_skipped, 'pages_failed', r.pages_failed,
    'chunks_total', r.chunks_total, 'chunks_done', r.chunks_done,
    'chunks_failed', r.chunks_failed, 'items_written', r.items_written,
    'items_low_confidence', r.items_low_confidence, 'items_unmatched', r.items_unmatched,
    'items_superseded', r.items_superseded, 'tokens_in', r.tokens_in,
    'tokens_out', r.tokens_out, 'estimated_cost_eur', r.estimated_cost_eur, 'error', r.error,
    'superseded_by_run_id', r.superseded_by_run_id, 'job_id', r.job_id,
    'created_at', r.created_at, 'started_at', r.started_at, 'finished_at', r.finished_at)"""


async def reusable_run(
    session: AsyncSession,
    *,
    municipality_id: str,
    document_id: int,
    sha256: str,
    model: str,
    prompt_version: str = PROMPT_VERSION,
    schema_version: str = SCHEMA_VERSION,
) -> Mapping[str, Any] | None:
    """The run that already read this content this way and reached ``ready_for_review``."""
    return (
        (
            await session.execute(
                REUSABLE_RUN_SQL,
                {
                    "m": municipality_id,
                    "document_id": document_id,
                    "sha256": sha256,
                    "model": model,
                    "prompt_version": prompt_version,
                    "schema_version": schema_version,
                },
            )
        )
        .mappings()
        .first()
    )


async def insert_run(
    session: AsyncSession,
    *,
    municipality_id: str,
    document_id: int,
    job_id: int | None,
    model: str,
    prompt_version: str = PROMPT_VERSION,
    schema_version: str = SCHEMA_VERSION,
) -> int | None:
    """A queued run for the document's current file; None when the document has no file."""
    new_id = (
        await session.execute(
            INSERT_RUN_SQL,
            {
                "m": municipality_id,
                "document_id": document_id,
                "job_id": job_id,
                "model": model,
                "prompt_version": prompt_version,
                "schema_version": schema_version,
            },
        )
    ).scalar_one_or_none()
    return int(new_id) if new_id is not None else None
