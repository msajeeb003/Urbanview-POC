"""admin pipeline: staff users and sessions, stored files, document versions and the coverage
switch, pipeline jobs, audit log

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-24

The staff API (``/v1/admin``) operates the data pipeline: uploads go to the private bucket and
are recorded in ``stored_files`` (de-duplicated by SHA-256); planning documents are registered
per *version* (``planning_documents.lineage_id`` / ``version`` / ``is_current_version``, previous
versions retained); jobs queued to Celery are tracked in ``pipeline_jobs``; every admin action is
written to ``audit_log``. ``planning_documents.coverage_live`` is the admin switch deciding
whether a document's coverage area takes part in location resolution; ``coverage_geom`` becomes
nullable because a document is registered before its geometry job has run.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(name: str, **kwargs):
    return sa.Column(name, sa.DateTime(timezone=True), **kwargs)


def upgrade() -> None:
    op.create_table(
        "staff_users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "email", sa.Text(), nullable=False, comment="lower-case; the only personal datum"
        ),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False, comment="admin | reviewer | expert"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        _timestamp("created_at", nullable=False, server_default=sa.func.now()),
        _timestamp("last_login_at", nullable=True),
        sa.CheckConstraint("role IN ('admin', 'reviewer', 'expert')", name="ck_staff_users_role"),
    )
    op.create_index(
        "uq_staff_users_email", "staff_users", ["municipality_id", "email"], unique=True
    )

    op.create_table(
        "staff_sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "token_hash",
            sa.Text(),
            nullable=False,
            comment="sha256 of the bearer token; the token itself is never stored",
        ),
        sa.Column("created_via", sa.Text(), nullable=False, comment="magic_link | cli | test"),
        _timestamp("created_at", nullable=False, server_default=sa.func.now()),
        _timestamp("expires_at", nullable=False),
        _timestamp("revoked_at", nullable=True),
    )
    op.create_index("uq_staff_sessions_token_hash", "staff_sessions", ["token_hash"], unique=True)
    op.create_index("ix_staff_sessions_user_id", "staff_sessions", ["user_id"])

    op.create_table(
        "stored_files",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "kind", sa.Text(), nullable=False, comment="planning_document | gis | cadastral_extract"
        ),
        sa.Column(
            "object_key",
            sa.Text(),
            nullable=False,
            comment="{municipality}/uploads/{kind}/{sha256}/{filename} in the private bucket",
        ),
        sa.Column("sha256", sa.Text(), nullable=False, comment="hex digest; de-duplication key"),
        sa.Column("original_filename", sa.Text(), nullable=False, comment="sanitised"),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True, comment="PDFs only"),
        sa.Column("uploaded_by", sa.Text(), nullable=False, comment="principal subject"),
        sa.Column(
            "uploaded_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _timestamp("uploaded_at", nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "kind IN ('planning_document', 'gis', 'cadastral_extract')",
            name="ck_stored_files_kind",
        ),
    )
    op.create_index(
        "uq_stored_files_sha256", "stored_files", ["municipality_id", "sha256"], unique=True
    )
    op.create_index("uq_stored_files_object_key", "stored_files", ["object_key"], unique=True)
    op.create_index(
        "ix_stored_files_kind_time", "stored_files", ["municipality_id", "kind", "uploaded_at"]
    )

    # planning documents: registration, versions, coverage switch
    op.alter_column("planning_documents", "coverage_geom", nullable=True)
    op.add_column(
        "planning_documents",
        sa.Column(
            "file_id",
            sa.BigInteger(),
            sa.ForeignKey("stored_files.id", ondelete="SET NULL"),
            nullable=True,
            comment="the uploaded PDF this version was registered against",
        ),
    )
    op.add_column(
        "planning_documents",
        sa.Column(
            "lineage_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_documents.id", ondelete="SET NULL"),
            nullable=True,
            comment="first version of this document; null on legacy rows = itself",
        ),
    )
    op.add_column(
        "planning_documents",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "planning_documents",
        sa.Column(
            "is_current_version", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )
    op.add_column(
        "planning_documents",
        sa.Column(
            "licence_note",
            sa.Text(),
            nullable=True,
            comment="licence / permission to use the document",
        ),
    )
    op.add_column("planning_documents", sa.Column("registered_by", sa.Text(), nullable=True))
    op.add_column("planning_documents", _timestamp("registered_at", nullable=True))
    op.add_column(
        "planning_documents",
        sa.Column(
            "coverage_live",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="the coverage area takes part in location resolution (admin switch)",
        ),
    )
    op.add_column("planning_documents", _timestamp("coverage_live_changed_at", nullable=True))
    op.add_column(
        "planning_documents", sa.Column("coverage_live_changed_by", sa.Text(), nullable=True)
    )
    op.create_index("ix_planning_documents_file_id", "planning_documents", ["file_id"])
    op.create_index("ix_planning_documents_lineage_id", "planning_documents", ["lineage_id"])
    op.create_index(
        "uq_planning_documents_current_version",
        "planning_documents",
        ["lineage_id"],
        unique=True,
        postgresql_where=sa.text("is_current_version AND lineage_id IS NOT NULL"),
    )

    op.create_table(
        "pipeline_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, comment="extract | geo"),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'queued'"),
            comment="queued | running | succeeded | failed | cancelled",
        ),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "file_id",
            sa.BigInteger(),
            sa.ForeignKey("stored_files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("celery_task_id", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=False, comment="principal subject"),
        sa.Column(
            "requested_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _timestamp("requested_at", nullable=False, server_default=sa.func.now()),
        _timestamp("started_at", nullable=True),
        _timestamp("finished_at", nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint("kind IN ('extract', 'geo')", name="ck_pipeline_jobs_kind"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_pipeline_jobs_status",
        ),
    )
    op.create_index(
        "ix_pipeline_jobs_document",
        "pipeline_jobs",
        ["municipality_id", "document_id", "requested_at"],
    )
    op.create_index(
        "ix_pipeline_jobs_file", "pipeline_jobs", ["municipality_id", "file_id", "requested_at"]
    )
    op.create_index("ix_pipeline_jobs_status", "pipeline_jobs", ["municipality_id", "status"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False, comment="principal subject"),
        sa.Column(
            "actor_user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.Text(), nullable=False, comment="e.g. file.upload, job.enqueue"),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("request_id", sa.Text(), nullable=True),
        _timestamp("created_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_log_time", "audit_log", ["municipality_id", "created_at"])
    op.create_index(
        "ix_audit_log_entity", "audit_log", ["municipality_id", "entity_type", "entity_id"]
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("pipeline_jobs")
    op.drop_index("uq_planning_documents_current_version", table_name="planning_documents")
    op.drop_index("ix_planning_documents_lineage_id", table_name="planning_documents")
    op.drop_index("ix_planning_documents_file_id", table_name="planning_documents")
    for column in (
        "coverage_live_changed_by",
        "coverage_live_changed_at",
        "coverage_live",
        "registered_at",
        "registered_by",
        "licence_note",
        "is_current_version",
        "version",
        "lineage_id",
        "file_id",
    ):
        op.drop_column("planning_documents", column)
    op.alter_column("planning_documents", "coverage_geom", nullable=False)
    op.drop_table("stored_files")
    op.drop_table("staff_sessions")
    op.drop_table("staff_users")
