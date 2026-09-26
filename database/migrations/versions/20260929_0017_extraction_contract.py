"""planning_parameter_extractions: the extraction contract stored with every item

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-29

AI extraction writes review items in the canonical contract (core.extraction,
docs/specs/extraction-contract.md). Each item keeps the leaf exactly as extracted (``payload``)
with the schema and prompt versions that produced it, so items stay readable after the schema or
the prompts change (``core.extraction.read_payload`` reads each major version). The flags the
validator raised (low_confidence, out_of_range ...) and the extraction method are columns the
review queue filters and shows; ``job_id`` is the extraction run. Seeded and manual rows leave
them null (flags empty).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "planning_parameter_extractions"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "schema_version",
            sa.Text(),
            comment="extraction contract version of payload; null = manual or seeded row",
        ),
    )
    op.add_column(
        TABLE,
        sa.Column("prompt_version", sa.Text(), comment="prompt set that produced the item"),
    )
    op.add_column(
        TABLE,
        sa.Column("extraction_method", sa.Text(), comment="text | table | ocr"),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "flags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="validator flags for the reviewer (low_confidence, out_of_range ...)",
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            comment="the extraction item as produced (core.extraction.read_payload)",
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey("pipeline_jobs.id", ondelete="SET NULL"),
            comment="the extraction job that produced the item",
        ),
    )
    op.create_check_constraint(
        "ck_planning_parameter_extractions_method",
        TABLE,
        "extraction_method IS NULL OR extraction_method IN ('text', 'table', 'ocr')",
    )
    op.create_check_constraint(
        "ck_planning_parameter_extractions_payload_version",
        TABLE,
        "payload IS NULL OR schema_version IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_planning_parameter_extractions_payload_version", TABLE, type_="check")
    op.drop_constraint("ck_planning_parameter_extractions_method", TABLE, type_="check")
    for column in (
        "job_id",
        "payload",
        "flags",
        "extraction_method",
        "prompt_version",
        "schema_version",
    ):
        op.drop_column(TABLE, column)
