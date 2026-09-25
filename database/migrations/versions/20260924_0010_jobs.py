"""background jobs: type / queue / target / payload, retries with attempts, idempotency keys and
LLM cost fields on pipeline_jobs

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-24

One job system for every long-running task (``jobs/base.py``): ``type`` names the task
(extract_document | process_geometry | publish_approved | send_email), ``queue`` the Celery
queue, ``target_type`` / ``target_id`` what it works on, ``payload`` its input. The base task
writes the status transitions (``retrying`` joins the states), counts ``attempts`` against
``max_attempts`` with exponential backoff, and records wall time and LLM cost on completion so
extraction spend per document is visible. ``dedupe_key`` with a partial unique index makes
enqueueing idempotent while a job is queued / running / retrying.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "pipeline_jobs"


def upgrade() -> None:
    op.drop_constraint("ck_pipeline_jobs_kind", TABLE, type_="check")
    op.create_check_constraint(
        "ck_pipeline_jobs_kind", TABLE, "kind IN ('extract', 'geo', 'publish', 'email')"
    )
    op.drop_constraint("ck_pipeline_jobs_status", TABLE, type_="check")
    op.create_check_constraint(
        "ck_pipeline_jobs_status",
        TABLE,
        "status IN ('queued', 'running', 'retrying', 'succeeded', 'failed', 'cancelled')",
    )
    op.alter_column(
        TABLE,
        "kind",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment="extract | geo | publish | email",
        existing_comment="extract | geo",
    )
    op.alter_column(
        TABLE,
        "status",
        existing_type=sa.Text(),
        existing_nullable=False,
        existing_server_default=sa.text("'queued'"),
        comment="queued | running | retrying | succeeded | failed | cancelled",
        existing_comment="queued | running | succeeded | failed | cancelled",
    )
    op.add_column(
        TABLE,
        sa.Column(
            "type",
            sa.Text(),
            nullable=True,
            comment="extract_document | process_geometry | publish_approved | send_email",
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "queue",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
            comment="Celery queue",
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "target_type", sa.Text(), nullable=True, comment="document | file | publish_run | email"
        ),
    )
    op.add_column(TABLE, sa.Column("target_id", sa.BigInteger(), nullable=True))
    op.add_column(
        TABLE,
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="the task's input",
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "dedupe_key",
            sa.Text(),
            nullable=True,
            comment="idempotency key; unique while queued / running / retrying",
        ),
    )
    op.add_column(
        TABLE, sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0"))
    )
    op.add_column(
        TABLE, sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3"))
    )
    op.add_column(
        TABLE,
        sa.Column("manual_retries", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(TABLE, sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        TABLE, sa.Column("wall_time_ms", sa.Integer(), nullable=True, comment="last attempt")
    )
    op.add_column(TABLE, sa.Column("llm_model", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("llm_tokens_in", sa.Integer(), nullable=True))
    op.add_column(TABLE, sa.Column("llm_tokens_out", sa.Integer(), nullable=True))
    op.add_column(
        TABLE,
        sa.Column(
            "estimated_cost_eur",
            sa.Numeric(12, 4),
            nullable=True,
            comment="from the configured LLM prices",
        ),
    )
    op.execute(
        f"""
        UPDATE {TABLE} SET
            type = CASE kind WHEN 'extract' THEN 'extract_document' WHEN 'geo' THEN 'process_geometry'
                             WHEN 'publish' THEN 'publish_approved' ELSE 'send_email' END,
            queue = CASE kind WHEN 'extract' THEN 'extraction' WHEN 'geo' THEN 'geo'
                              WHEN 'publish' THEN 'publish' ELSE 'email' END,
            target_type = CASE WHEN document_id IS NOT NULL THEN 'document'
                               WHEN file_id IS NOT NULL THEN 'file' END,
            target_id = COALESCE(document_id, file_id),
            payload = CASE WHEN document_id IS NOT NULL THEN jsonb_build_object('document_id', document_id)
                           WHEN file_id IS NOT NULL THEN jsonb_build_object('file_id', file_id)
                           ELSE '{{}}'::jsonb END
        """
    )
    op.alter_column(TABLE, "type", nullable=False)
    op.create_check_constraint(
        "ck_pipeline_jobs_type",
        TABLE,
        "type IN ('extract_document', 'process_geometry', 'publish_approved', 'send_email')",
    )
    op.create_check_constraint(
        "ck_pipeline_jobs_attempts", TABLE, "attempts >= 0 AND max_attempts >= 1"
    )
    op.create_index(
        "uq_pipeline_jobs_active_dedupe",
        TABLE,
        ["municipality_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text(
            "dedupe_key IS NOT NULL AND status IN ('queued', 'running', 'retrying')"
        ),
    )
    op.create_index(
        "ix_pipeline_jobs_target",
        TABLE,
        ["municipality_id", "target_type", "target_id", "requested_at"],
    )
    op.create_index("ix_pipeline_jobs_type_status", TABLE, ["municipality_id", "type", "status"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_jobs_type_status", table_name=TABLE)
    op.drop_index("ix_pipeline_jobs_target", table_name=TABLE)
    op.drop_index("uq_pipeline_jobs_active_dedupe", table_name=TABLE)
    op.drop_constraint("ck_pipeline_jobs_attempts", TABLE, type_="check")
    op.drop_constraint("ck_pipeline_jobs_type", TABLE, type_="check")
    for column in (
        "estimated_cost_eur",
        "llm_tokens_out",
        "llm_tokens_in",
        "llm_model",
        "wall_time_ms",
        "next_retry_at",
        "manual_retries",
        "max_attempts",
        "attempts",
        "dedupe_key",
        "payload",
        "target_id",
        "target_type",
        "queue",
        "type",
    ):
        op.drop_column(TABLE, column)
    op.alter_column(
        TABLE,
        "status",
        existing_type=sa.Text(),
        existing_nullable=False,
        existing_server_default=sa.text("'queued'"),
        comment="queued | running | succeeded | failed | cancelled",
        existing_comment="queued | running | retrying | succeeded | failed | cancelled",
    )
    op.alter_column(
        TABLE,
        "kind",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment="extract | geo",
        existing_comment="extract | geo | publish | email",
    )
    op.execute(f"UPDATE {TABLE} SET status = 'failed' WHERE status = 'retrying'")
    op.drop_constraint("ck_pipeline_jobs_status", TABLE, type_="check")
    op.create_check_constraint(
        "ck_pipeline_jobs_status",
        TABLE,
        "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
    )
    op.execute(f"DELETE FROM {TABLE} WHERE kind NOT IN ('extract', 'geo')")
    op.drop_constraint("ck_pipeline_jobs_kind", TABLE, type_="check")
    op.create_check_constraint("ck_pipeline_jobs_kind", TABLE, "kind IN ('extract', 'geo')")
