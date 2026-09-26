"""extraction_runs / extraction_run_chunks: the extract_document job's runs (core.extraction)

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-02

One ``extraction_runs`` row per extraction of a document version's file: the file checksum and
the model / prompt / schema versions (the idempotency key: the same content read the same way is
never run twice), the status admins follow (queued -> extracting -> ready_for_review | failed),
pages processed, skipped (scanned, unread) and failed, items written and flagged, tokens and
estimated cost. ``extraction_run_chunks`` checkpoints every chunk x task the run read (its
canonical results, or its error), so a retried job resumes instead of paying twice and a failed
chunk never blocks the rest.

Review items (``planning_parameter_extractions``) link to their run, keep the target as printed
when no geometry matches it (``target_label`` / ``target_key``), point at the item of the previous
run for the same target and field (``previous_item_id``, ``change`` new | same | changed), and
are superseded (``superseded_at``, ``superseded_by_run_id``) instead of deleted when a newer run
or a changed file replaces them. Nothing here touches the serving tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ITEMS = "planning_parameter_extractions"


def upgrade() -> None:
    op.create_table(
        "extraction_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "lineage_id",
            sa.BigInteger(),
            nullable=False,
            comment="the document lineage (first version's id): older versions' items supersede",
        ),
        sa.Column(
            "file_id", sa.BigInteger(), sa.ForeignKey("stored_files.id", ondelete="SET NULL")
        ),
        sa.Column("file_sha256", sa.Text(), nullable=False),
        sa.Column(
            "model", sa.Text(), nullable=False, comment="the configured model (idempotency key)"
        ),
        sa.Column("model_version", sa.Text(), comment="the model id the API reported"),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("preprocess_version", sa.Text()),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'queued'"),
            comment="queued | extracting | ready_for_review | failed",
        ),
        sa.Column(
            "job_id", sa.BigInteger(), sa.ForeignKey("pipeline_jobs.id", ondelete="SET NULL")
        ),
        sa.Column(
            "superseded_by_run_id",
            sa.BigInteger(),
            sa.ForeignKey("extraction_runs.id", ondelete="SET NULL"),
            comment="a later run of the lineage that superseded this run's open items",
        ),
        sa.Column("pages_total", sa.Integer()),
        sa.Column(
            "pages_processed",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="pages read by at least one successful chunk",
        ),
        sa.Column(
            "pages_skipped",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="scanned pages nobody could read (no OCR): never filled in",
        ),
        sa.Column(
            "pages_failed",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="[{page, chunk, task, error}] for chunks that failed after their retry",
        ),
        sa.Column("chunks_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("chunks_done", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("chunks_failed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("items_written", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "items_low_confidence", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "items_unmatched",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="items whose parcel / block matches no geometry (target kept as printed)",
        ),
        sa.Column(
            "items_superseded",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="earlier items this run superseded",
        ),
        sa.Column("tokens_in", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_out", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("estimated_cost_eur", sa.Numeric(12, 4)),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            comment="the run summary the job result and the admin show",
        ),
        sa.Column("error", sa.Text()),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column(
            "requested_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued', 'extracting', 'ready_for_review', 'failed')",
            name="ck_extraction_runs_status",
        ),
    )
    op.create_index(
        "ix_extraction_runs_document", "extraction_runs", ["municipality_id", "document_id"]
    )
    op.create_index(
        "ix_extraction_runs_key",
        "extraction_runs",
        ["document_id", "file_sha256", "prompt_version", "schema_version", "model"],
    )
    op.create_index("uq_extraction_runs_job", "extraction_runs", ["job_id"], unique=True)
    op.create_index("ix_extraction_runs_lineage", "extraction_runs", ["lineage_id"])

    op.create_table(
        "extraction_run_chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey("extraction_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_id", sa.Text(), nullable=False, comment="the manifest's chunk (c001)"),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column(
            "pages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.Text(), nullable=False, comment="done | failed"),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
            comment="model requests, the validation retry included",
        ),
        sa.Column("error", sa.Text()),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            comment="the canonical ExtractionResult (core.extraction.schema)",
        ),
        sa.Column("model_version", sa.Text()),
        sa.Column("tokens_in", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_out", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("status IN ('done', 'failed')", name="ck_extraction_run_chunks_status"),
    )
    op.create_index(
        "uq_extraction_run_chunks_step",
        "extraction_run_chunks",
        ["run_id", "chunk_id", "task"],
        unique=True,
    )

    op.add_column(
        ITEMS,
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey("extraction_runs.id", ondelete="SET NULL"),
            comment="the extraction run that wrote the item; null = manual or seeded",
        ),
    )
    op.add_column(
        ITEMS,
        sa.Column(
            "target_label",
            sa.Text(),
            comment="the parcel number / block label as printed (kept when no geometry matches)",
        ),
    )
    op.add_column(
        ITEMS,
        sa.Column(
            "target_key",
            sa.Text(),
            comment="normalised target: parcel_key, block_key or 'document'",
        ),
    )
    op.add_column(
        ITEMS,
        sa.Column(
            "previous_item_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{ITEMS}.id", ondelete="SET NULL"),
            comment="the previous run's item for the same target and field",
        ),
    )
    op.add_column(
        ITEMS,
        sa.Column("change", sa.Text(), comment="new | same | changed against previous_item_id"),
    )
    op.add_column(
        ITEMS,
        sa.Column(
            "superseded_by_run_id",
            sa.BigInteger(),
            sa.ForeignKey("extraction_runs.id", ondelete="SET NULL"),
            comment="the run (or decision) that replaced this open item; never deleted",
        ),
    )
    op.add_column(ITEMS, sa.Column("superseded_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_planning_parameter_extractions_change",
        ITEMS,
        "change IS NULL OR change IN ('new', 'same', 'changed')",
    )
    op.create_index("ix_planning_parameter_extractions_run", ITEMS, ["run_id"])
    op.create_index(
        "ix_planning_parameter_extractions_target",
        ITEMS,
        ["document_id", "target_key", "field_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_planning_parameter_extractions_target", table_name=ITEMS)
    op.drop_index("ix_planning_parameter_extractions_run", table_name=ITEMS)
    op.drop_constraint("ck_planning_parameter_extractions_change", ITEMS, type_="check")
    for column in (
        "superseded_at",
        "superseded_by_run_id",
        "change",
        "previous_item_id",
        "target_key",
        "target_label",
        "run_id",
    ):
        op.drop_column(ITEMS, column)
    op.drop_index("uq_extraction_run_chunks_step", table_name="extraction_run_chunks")
    op.drop_table("extraction_run_chunks")
    op.drop_index("ix_extraction_runs_lineage", table_name="extraction_runs")
    op.drop_index("uq_extraction_runs_job", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_key", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_document", table_name="extraction_runs")
    op.drop_table("extraction_runs")
