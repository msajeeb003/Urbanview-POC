"""market_imports / market_data: reviewed market inputs for the engine (core.market)

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-01

Market figures (official statistics such as Monstat's, the client's zone ranges, pasted portal
listings) are imported raw (``market_imports``: source, retrieval date, file checksum, the table as
read), normalised to zone-level low / expected / high inputs per metric (``market_data``) and
reviewed; only an approved or amended row writes a new ``financial_assumptions`` version, with its
effective date and the provenance of each rate (``effective_from``, ``rate_sources``). Nothing
unreviewed reaches the panel. Also: the ``market_data`` upload kind and the ``import_market_data``
job type.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FILE_KINDS_BEFORE = "'planning_document', 'gis', 'cadastral_extract', 'expert_report'"
FILE_KINDS_AFTER = FILE_KINDS_BEFORE + ", 'market_data'"
JOB_TYPES_BEFORE = (
    "'extract_document', 'preprocess_file', 'process_geometry', 'publish_approved', 'send_email'"
)
JOB_TYPES_AFTER = JOB_TYPES_BEFORE + ", 'import_market_data'"
JOB_COMMENT_BEFORE = (
    "extract_document | preprocess_file | process_geometry | publish_approved | send_email"
)
JOB_COMMENT_AFTER = JOB_COMMENT_BEFORE + " | import_market_data"
TARGET_COMMENT_BEFORE = "document | file | publish_run | email"
TARGET_COMMENT_AFTER = TARGET_COMMENT_BEFORE + " | market_import"
ZONE_COMMENT_BEFORE = "null = municipality-wide default"
ZONE_COMMENT_AFTER = (
    "null = municipality-wide row: range factors for market imports, never a zone's figures"
)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    review_state = postgresql.ENUM(name="review_state", create_type=False)
    op.create_table(
        "market_imports",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "kind", sa.Text(), nullable=False, comment="statistics | client_ranges | listings"
        ),
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            comment="who published the figures: Monstat, the client, a listings portal",
        ),
        sa.Column(
            "retrieved_on",
            sa.Date(),
            nullable=False,
            comment="when the figures were retrieved or received",
        ),
        sa.Column(
            "file_id",
            sa.BigInteger(),
            sa.ForeignKey("stored_files.id", ondelete="SET NULL"),
            comment="the uploaded table; null for pasted listings",
        ),
        sa.Column("filename", sa.Text()),
        sa.Column(
            "sha256",
            sa.Text(),
            nullable=False,
            comment="checksum of the file, or of the pasted listings",
        ),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="the table as read, cells untouched",
        ),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'received'"),
            comment="received | normalised | failed",
        ),
        sa.Column("normaliser", sa.Text(), comment="rules, or llm:<model> with the prompt version"),
        sa.Column(
            "report",
            postgresql.JSONB(astext_type=sa.Text()),
            comment="normalisation report: rows mapped, rows not mapped and why, issues",
        ),
        sa.Column("error", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.Column(
            "job_id", sa.BigInteger(), sa.ForeignKey("pipeline_jobs.id", ondelete="SET NULL")
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="SET NULL"),
        ),
        _created_at(),
        sa.Column("normalised_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('statistics', 'client_ranges', 'listings')", name="ck_market_imports_kind"
        ),
        sa.CheckConstraint(
            "status IN ('received', 'normalised', 'failed')", name="ck_market_imports_status"
        ),
    )
    op.create_index(
        "uq_market_imports_checksum",
        "market_imports",
        ["municipality_id", "kind", "sha256"],
        unique=True,
    )
    op.create_index("ix_market_imports_time", "market_imports", ["municipality_id", "created_at"])

    op.create_table(
        "market_data",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "import_id",
            sa.BigInteger(),
            sa.ForeignKey("market_imports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "zone_id",
            sa.BigInteger(),
            sa.ForeignKey("zones.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "metric",
            sa.Text(),
            nullable=False,
            comment="land_rate | build_rate | design_rate | sale_rate",
        ),
        sa.Column("currency", sa.Text(), nullable=False, server_default=sa.text("'EUR'")),
        sa.Column("unit", sa.Text(), nullable=False, server_default=sa.text("'EUR/m²'")),
        sa.Column("low", sa.Float(precision=53), comment="null when no range could be read"),
        sa.Column("expected", sa.Float(precision=53), nullable=False),
        sa.Column("high", sa.Float(precision=53), comment="null when no range could be read"),
        sa.Column(
            "range_basis",
            sa.Text(),
            nullable=False,
            comment="stated | derived (configured range factors) | listings | unavailable",
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_date", sa.Date(), comment="the date or period end the figure refers to"),
        sa.Column("effective_from", sa.Date(), comment="set on approval: applies from this date"),
        sa.Column("confidence", sa.Float(precision=53)),
        sa.Column("notes", sa.Text()),
        sa.Column(
            "flags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="what the reviewer should look at (municipality_level, range_derived ...)",
        ),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="the source row and cells the figure was read from",
        ),
        sa.Column(
            "mapping",
            postgresql.JSONB(astext_type=sa.Text()),
            comment="how the source geography became the zone (method, printed name, confidence)",
        ),
        sa.Column("normaliser", sa.Text(), nullable=False, comment="rules or llm:<model>"),
        sa.Column(
            "review_status",
            review_state,
            nullable=False,
            server_default=sa.text("'pending_review'"),
        ),
        sa.Column("amended_low", sa.Float(precision=53)),
        sa.Column("amended_expected", sa.Float(precision=53)),
        sa.Column("amended_high", sa.Float(precision=53)),
        sa.Column("reviewer", sa.Text()),
        sa.Column(
            "reviewed_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="SET NULL"),
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_note", sa.Text()),
        sa.Column(
            "applied_assumption_id",
            sa.BigInteger(),
            sa.ForeignKey("financial_assumptions.id", ondelete="SET NULL"),
            comment="the assumptions version this item wrote; the item is then closed",
        ),
        _created_at(),
        sa.CheckConstraint(
            "metric IN ('land_rate', 'build_rate', 'design_rate', 'sale_rate')",
            name="ck_market_data_metric",
        ),
        sa.CheckConstraint(
            "range_basis IN ('stated', 'derived', 'listings', 'unavailable')",
            name="ck_market_data_range_basis",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_market_data_confidence",
        ),
        sa.CheckConstraint(
            "review_status <> 'amended' OR amended_expected IS NOT NULL",
            name="ck_market_data_amended",
        ),
    )
    op.create_index(
        "ix_market_data_queue", "market_data", ["municipality_id", "review_status", "zone_id"]
    )
    op.create_index("ix_market_data_import", "market_data", ["import_id"])

    op.alter_column(
        "financial_assumptions",
        "zone_id",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
        comment=ZONE_COMMENT_AFTER,
        existing_comment=ZONE_COMMENT_BEFORE,
    )
    op.add_column(
        "financial_assumptions",
        sa.Column(
            "effective_from", sa.Date(), comment="the date the version applies from, when stated"
        ),
    )
    op.add_column(
        "financial_assumptions",
        sa.Column(
            "rate_sources",
            postgresql.JSONB(astext_type=sa.Text()),
            comment="per rate: source, source date, the market input that set it",
        ),
    )

    op.drop_constraint("ck_stored_files_kind", "stored_files", type_="check")
    op.create_check_constraint(
        "ck_stored_files_kind", "stored_files", f"kind IN ({FILE_KINDS_AFTER})"
    )
    op.drop_constraint("ck_pipeline_jobs_type", "pipeline_jobs", type_="check")
    op.create_check_constraint(
        "ck_pipeline_jobs_type", "pipeline_jobs", f"type IN ({JOB_TYPES_AFTER})"
    )
    op.alter_column(
        "pipeline_jobs",
        "type",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=JOB_COMMENT_AFTER,
        existing_comment=JOB_COMMENT_BEFORE,
    )
    op.alter_column(
        "pipeline_jobs",
        "target_type",
        existing_type=sa.Text(),
        existing_nullable=True,
        comment=TARGET_COMMENT_AFTER,
        existing_comment=TARGET_COMMENT_BEFORE,
    )


def downgrade() -> None:
    op.execute("DELETE FROM pipeline_jobs WHERE type = 'import_market_data'")
    op.alter_column(
        "pipeline_jobs",
        "target_type",
        existing_type=sa.Text(),
        existing_nullable=True,
        comment=TARGET_COMMENT_BEFORE,
        existing_comment=TARGET_COMMENT_AFTER,
    )
    op.alter_column(
        "pipeline_jobs",
        "type",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=JOB_COMMENT_BEFORE,
        existing_comment=JOB_COMMENT_AFTER,
    )
    op.drop_constraint("ck_pipeline_jobs_type", "pipeline_jobs", type_="check")
    op.create_check_constraint(
        "ck_pipeline_jobs_type", "pipeline_jobs", f"type IN ({JOB_TYPES_BEFORE})"
    )
    op.drop_index("ix_market_data_import", table_name="market_data")
    op.drop_index("ix_market_data_queue", table_name="market_data")
    op.drop_table("market_data")
    op.drop_index("ix_market_imports_time", table_name="market_imports")
    op.drop_index("uq_market_imports_checksum", table_name="market_imports")
    op.drop_table("market_imports")
    op.execute("DELETE FROM stored_files WHERE kind = 'market_data'")
    op.drop_constraint("ck_stored_files_kind", "stored_files", type_="check")
    op.create_check_constraint(
        "ck_stored_files_kind", "stored_files", f"kind IN ({FILE_KINDS_BEFORE})"
    )
    op.drop_column("financial_assumptions", "rate_sources")
    op.drop_column("financial_assumptions", "effective_from")
    op.alter_column(
        "financial_assumptions",
        "zone_id",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
        comment=ZONE_COMMENT_BEFORE,
        existing_comment=ZONE_COMMENT_AFTER,
    )
