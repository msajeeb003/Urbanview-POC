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

FILE_KINDS: tuple[str, ...] = ("planning_document", "gis", "cadastral_extract", "market_data")
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
    )  # + expert_report since migration 0009, market_data since 0019 (the CHECK below)
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
    # PDF pre-processing (migration 0018): pages, tables, scanned pages, chunk plan, page image
    # keys and the admin summary, for this checksum (core.extraction.manifest)
    preprocess: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="PDF pre-processing manifest (core.extraction.manifest); null = not run"
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('planning_document', 'gis', 'cadastral_extract', 'expert_report', "
            "'market_data')",
            name="ck_stored_files_kind",
        ),
        Index("uq_stored_files_sha256", "municipality_id", "sha256", unique=True),
        Index("uq_stored_files_object_key", "object_key", unique=True),
        Index("ix_stored_files_kind_time", "municipality_id", "kind", "uploaded_at"),
    )


FILE_ROLES: tuple[str, ...] = ("text", "drawing", "both")


class PlanningDocumentFile(Base):
    """A stored file of a planning document version (migration 0022) and what it is read for:
    ``text`` (the extraction job), ``drawing`` (the geometry job) or ``both``. Each file is
    extracted on its own; ``planning_documents.file_id`` stays the version's primary file."""

    __tablename__ = "planning_document_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("planning_documents.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("stored_files.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'text'"),
        comment="text (extraction) | drawing (geometry) | both",
    )
    position: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="display order within the version",
    )
    added_by: Mapped[str | None] = mapped_column(Text, comment="principal subject")
    added_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    added_at: Mapped[datetime] = _timestamp(nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "role IN ('text', 'drawing', 'both')", name="ck_planning_document_files_role"
        ),
        Index("uq_planning_document_files_document_file", "document_id", "file_id", unique=True),
        Index("ix_planning_document_files_file", "file_id"),
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
        comment=(
            "extract_document | preprocess_file | process_geometry | publish_approved | send_email"
            " | import_market_data | refresh_heatmaps | ai_check | import_zones"
        ),
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
        Text, comment="document | file | publish_run | email | market_import | ai_settings"
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
            "type IN ('extract_document', 'preprocess_file', 'process_geometry', "
            "'publish_approved', 'send_email', 'import_market_data', 'refresh_heatmaps', "
            "'ai_check', 'import_zones')",
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


ENGINE_PROPOSAL_KINDS: tuple[str, ...] = ("formula", "data_input")
ENGINE_PROPOSAL_STATUSES: tuple[str, ...] = ("new", "pending", "accepted", "declined")


class EngineProposal(Base):
    """A formula or data input proposed for the calculation engine (migration 0023): recorded and
    audited for the client's review, it changes no calculation. The deterministic engine changes
    only with a new ``FORMULA_VERSION`` and fixtures the client validated."""

    __tablename__ = "engine_proposals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, comment="formula | data_input")
    name: Mapped[str] = mapped_column(
        Text, nullable=False, comment="the output (formula) or dataset (data input) name"
    )
    expression: Mapped[str | None] = mapped_column(Text, comment="formula: the expression")
    source: Mapped[str | None] = mapped_column(Text, comment="formula: where its inputs come from")
    provides: Mapped[str | None] = mapped_column(Text, comment="data input: what it provides")
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'new'"),
        comment="new (formula) | pending (data input) | accepted | declined",
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False, comment="principal subject")
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _timestamp(nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('formula', 'data_input')", name="ck_engine_proposals_kind"),
        CheckConstraint(
            "status IN ('new', 'pending', 'accepted', 'declined')",
            name="ck_engine_proposals_status",
        ),
        CheckConstraint(
            "(kind = 'formula' AND expression IS NOT NULL) OR "
            "(kind = 'data_input' AND provides IS NOT NULL)",
            name="ck_engine_proposals_content",
        ),
        Index("ix_engine_proposals_kind", "municipality_id", "kind", "created_at"),
    )


SECRET_CIPHERTEXT_COMMENT = (
    "Fernet token under SECRETS_ENCRYPTION_KEY; never returned, logged or audited"
)
SECRET_LAST4_COMMENT = "the last 4 characters: the only part ever shown"
SECRET_SET_AT_COMMENT = "when the value was saved; a connection test names the value it used by it"


class AppSecret(Base):
    """A secret saved from the admin console (migration 0028), encrypted with Fernet under
    SECRETS_ENCRYPTION_KEY (core.app_secrets); only last4 is ever shown."""

    __tablename__ = "app_secrets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False, comment="anthropic_api_key")
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False, comment=SECRET_CIPHERTEXT_COMMENT)
    last4: Mapped[str] = mapped_column(Text, nullable=False, comment=SECRET_LAST4_COMMENT)
    set_by: Mapped[str] = mapped_column(Text, nullable=False, comment="principal subject")
    set_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    set_at: Mapped[datetime] = _timestamp(
        nullable=False, server_default=func.now(), comment=SECRET_SET_AT_COMMENT
    )

    __table_args__ = (
        CheckConstraint("name IN ('anthropic_api_key')", name="ck_app_secrets_name"),
        CheckConstraint("char_length(last4) = 4", name="ck_app_secrets_last4"),
        Index("uq_app_secrets_name", "municipality_id", "name", unique=True),
    )
