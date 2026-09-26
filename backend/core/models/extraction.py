"""Extraction runs (migration 0020): the ``extract_document`` job's record of one reading of a
document version's file (``jobs.extraction_runner``).

``extraction_runs`` holds the idempotency key (file checksum + model + prompt and schema
versions), the admin-visible status (queued -> extracting -> ready_for_review | failed) and what
the run read, skipped, failed, wrote and cost; ``extraction_run_chunks`` checkpoints every
chunk x task (its canonical result or its error), so a retried job resumes. Review items link to
their run (``planning_parameter_extractions.run_id``). Comments and CHECKs mirror the migration.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

RUN_STATUSES: tuple[str, ...] = ("queued", "extracting", "ready_for_review", "failed")


def _jsonb_list(comment: str | None = None) -> Mapped[list[Any]]:
    return mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment=comment)


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("planning_documents.id", ondelete="CASCADE"), nullable=False
    )
    lineage_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="the document lineage (first version's id): older versions' items supersede",
    )
    file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("stored_files.id", ondelete="SET NULL")
    )
    file_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(
        Text, nullable=False, comment="the configured model (idempotency key)"
    )
    model_version: Mapped[str | None] = mapped_column(Text, comment="the model id the API reported")
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    preprocess_version: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'queued'"),
        comment="queued | extracting | ready_for_review | failed",
    )
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_jobs.id", ondelete="SET NULL")
    )
    superseded_by_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        comment="a later run of the lineage that superseded this run's open items",
    )
    pages_total: Mapped[int | None] = mapped_column(Integer)
    pages_processed: Mapped[list[Any]] = _jsonb_list("pages read by at least one successful chunk")
    pages_skipped: Mapped[list[Any]] = _jsonb_list(
        "scanned pages nobody could read (no OCR): never filled in"
    )
    pages_failed: Mapped[list[Any]] = _jsonb_list(
        "[{page, chunk, task, error}] for chunks that failed after their retry"
    )
    chunks_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    chunks_done: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    chunks_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    items_written: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    items_low_confidence: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    items_unmatched: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="items whose parcel / block matches no geometry (target kept as printed)",
    )
    items_superseded: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="earlier items this run superseded",
    )
    tokens_in: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    estimated_cost_eur: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="the run summary the job result and the admin show"
    )
    error: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'extracting', 'ready_for_review', 'failed')",
            name="ck_extraction_runs_status",
        ),
        Index("ix_extraction_runs_document", "municipality_id", "document_id"),
        Index(
            "ix_extraction_runs_key",
            "document_id",
            "file_sha256",
            "prompt_version",
            "schema_version",
            "model",
        ),
        Index("uq_extraction_runs_job", "job_id", unique=True),
        Index("ix_extraction_runs_lineage", "lineage_id"),
    )


class ExtractionRunChunk(Base):
    """One chunk x task of a run: done with its canonical result, or failed with its error."""

    __tablename__ = "extraction_run_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("extraction_runs.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[str] = mapped_column(
        Text, nullable=False, comment="the manifest's chunk (c001)"
    )
    task: Mapped[str] = mapped_column(Text, nullable=False)
    pages: Mapped[list[Any]] = _jsonb_list()
    status: Mapped[str] = mapped_column(Text, nullable=False, comment="done | failed")
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
        comment="model requests, the validation retry included",
    )
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="the canonical ExtractionResult (core.extraction.schema)"
    )
    model_version: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("status IN ('done', 'failed')", name="ck_extraction_run_chunks_status"),
        Index("uq_extraction_run_chunks_step", "run_id", "chunk_id", "task", unique=True),
    )
