"""Admin pipeline tables (migration 0006): staff users and sessions, stored files, pipeline jobs,
audit log. The planning document version columns live on ``core.models.planning.PlanningDocument``.

Conventions: every table carries ``municipality_id`` except the two that hang off a user or a
document by foreign key; CHECK constraints and index names mirror the migration because Alembic
does not compare CHECKs and compares index names.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
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

FILE_KINDS: tuple[str, ...] = ("planning_document", "gis", "cadastral_extract")
JOB_KINDS: tuple[str, ...] = ("extract", "geo")
JOB_STATUSES: tuple[str, ...] = ("queued", "running", "succeeded", "failed", "cancelled")


def _timestamp(**kwargs: Any) -> Mapped[Any]:
    return mapped_column(DateTime(timezone=True), **kwargs)


class StaffUser(Base):
    __tablename__ = "staff_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(
        Text, nullable=False, comment="lower-case; the only personal datum"
    )
    display_name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, nullable=False, comment="admin | reviewer | expert")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = _timestamp(nullable=False, server_default=func.now())
    last_login_at: Mapped[datetime | None] = _timestamp(nullable=True)

    __table_args__ = (
        CheckConstraint("role IN ('admin', 'reviewer', 'expert')", name="ck_staff_users_role"),
        Index("uq_staff_users_email", "municipality_id", "email", unique=True),
    )


class StaffSession(Base):
    __tablename__ = "staff_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("staff_users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        Text, nullable=False, comment="sha256 of the bearer token; the token itself is never stored"
    )
    created_via: Mapped[str] = mapped_column(
        Text, nullable=False, comment="magic_link | cli | test"
    )
    created_at: Mapped[datetime] = _timestamp(nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = _timestamp(nullable=False)
    revoked_at: Mapped[datetime | None] = _timestamp(nullable=True)

    __table_args__ = (
        Index("uq_staff_sessions_token_hash", "token_hash", unique=True),
        Index("ix_staff_sessions_user_id", "user_id"),
    )


class StaffLoginToken(Base):
    """A magic-link token (migration 0012): single use, short expiry; only the hash is stored,
    the token itself travels in the e-mail once."""

    __tablename__ = "staff_login_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("staff_users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="sha256 of the single-use token; the token itself only travels in the e-mail",
    )
    email_log_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("email_log.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _timestamp(nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = _timestamp(nullable=False)
    used_at: Mapped[datetime | None] = _timestamp(nullable=True)

    __table_args__ = (
        Index("uq_staff_login_tokens_token_hash", "token_hash", unique=True),
        Index("ix_staff_login_tokens_user_id", "user_id"),
    )


class StoredFile(Base):
    __tablename__ = "stored_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(
        Text, nullable=False, comment="planning_document | gis | cadastral_extract"
    )  # + expert_report since migration 0009 (the CHECK below)
    object_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="{municipality}/uploads/{kind}/{sha256}/{filename} in the private bucket",
    )
    sha256: Mapped[str] = mapped_column(
        Text, nullable=False, comment="hex digest; de-duplication key"
    )
    original_filename: Mapped[str] = mapped_column(Text, nullable=False, comment="sanitised")
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, comment="PDFs only")
    uploaded_by: Mapped[str] = mapped_column(Text, nullable=False, comment="principal subject")
    uploaded_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    uploaded_at: Mapped[datetime] = _timestamp(nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "kind IN ('planning_document', 'gis', 'cadastral_extract', 'expert_report')",
            name="ck_stored_files_kind",
        ),
        Index("uq_stored_files_sha256", "municipality_id", "sha256", unique=True),
        Index("uq_stored_files_object_key", "object_key", unique=True),
        Index("ix_stored_files_kind_time", "municipality_id", "kind", "uploaded_at"),
    )


class PipelineJob(Base):
    """One background job (``jobs/``): what to run, on what, its lifecycle and what it cost."""

    __tablename__ = "pipeline_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(
        Text, nullable=False, comment="extract | geo | publish | email"
    )
    type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="extract_document | process_geometry | publish_approved | send_email",
    )
    queue: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'default'"), comment="Celery queue"
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'queued'"),
        comment="queued | running | retrying | succeeded | failed | cancelled",
    )
    document_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("planning_documents.id", ondelete="SET NULL")
    )
    file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("stored_files.id", ondelete="SET NULL")
    )
    target_type: Mapped[str | None] = mapped_column(
        Text, comment="document | file | publish_run | email"
    )
    target_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="the task's input"
    )
    dedupe_key: Mapped[str | None] = mapped_column(
        Text, comment="idempotency key; unique while queued / running / retrying"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    manual_retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_retry_at: Mapped[datetime | None] = _timestamp(nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False, comment="principal subject")
    requested_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    requested_at: Mapped[datetime] = _timestamp(nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = _timestamp(nullable=True)
    finished_at: Mapped[datetime | None] = _timestamp(nullable=True)
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    progress: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="per-step progress written by the running task"
    )
    wall_time_ms: Mapped[int | None] = mapped_column(Integer, comment="last attempt")
    llm_model: Mapped[str | None] = mapped_column(Text)
    llm_tokens_in: Mapped[int | None] = mapped_column(Integer)
    llm_tokens_out: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_eur: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), comment="from the configured LLM prices"
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('extract', 'geo', 'publish', 'email')", name="ck_pipeline_jobs_kind"
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'retrying', 'succeeded', 'failed', 'cancelled')",
            name="ck_pipeline_jobs_status",
        ),
        CheckConstraint(
            "type IN ('extract_document', 'process_geometry', 'publish_approved', 'send_email')",
            name="ck_pipeline_jobs_type",
        ),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_pipeline_jobs_attempts"),
        Index("ix_pipeline_jobs_document", "municipality_id", "document_id", "requested_at"),
        Index("ix_pipeline_jobs_file", "municipality_id", "file_id", "requested_at"),
        Index("ix_pipeline_jobs_status", "municipality_id", "status"),
        Index(
            "ix_pipeline_jobs_target", "municipality_id", "target_type", "target_id", "requested_at"
        ),
        Index("ix_pipeline_jobs_type_status", "municipality_id", "type", "status"),
        Index(
            "uq_pipeline_jobs_active_dedupe",
            "municipality_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text(
                "dedupe_key IS NOT NULL AND status IN ('queued', 'running', 'retrying')"
            ),
        ),
    )


class AuditLogEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False, comment="principal subject")
    # No foreign key (migration 0008): the append-only log outlives staff users; the subject
    # string next to it says who it was.
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(
        Text, nullable=False, comment="e.g. file.upload, job.enqueue"
    )
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    before: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="state before the action (entity-specific summary)"
    )
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, comment="state after the action")
    note: Mapped[str | None] = mapped_column(Text, comment="free text from the actor")
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _timestamp(nullable=False, server_default=func.now())

    # Append-only: migration 0008 installs triggers that raise on UPDATE, DELETE and TRUNCATE.
    __table_args__ = (
        Index("ix_audit_log_time", "municipality_id", "created_at"),
        Index("ix_audit_log_entity", "municipality_id", "entity_type", "entity_id"),
    )
