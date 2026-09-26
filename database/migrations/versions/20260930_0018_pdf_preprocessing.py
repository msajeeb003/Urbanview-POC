"""stored_files.preprocess and the preprocess_file job: PDF pre-processing for extraction

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-30

The pre-processing job reads a stored planning PDF into pages, tables and chunks for the AI
extraction (core.extraction.preprocess / chunking). Its manifest (pages, tables, scanned pages,
chunk plan, page image keys, a summary for the admin records) is persisted on the file record,
keyed by the file's checksum so a re-run skips unchanged files.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TYPES_BEFORE = "'extract_document', 'process_geometry', 'publish_approved', 'send_email'"
TYPES_AFTER = (
    "'extract_document', 'preprocess_file', 'process_geometry', 'publish_approved', 'send_email'"
)
COMMENT_BEFORE = "extract_document | process_geometry | publish_approved | send_email"
COMMENT_AFTER = (
    "extract_document | preprocess_file | process_geometry | publish_approved | send_email"
)


def upgrade() -> None:
    op.add_column(
        "stored_files",
        sa.Column(
            "preprocess",
            postgresql.JSONB(astext_type=sa.Text()),
            comment="PDF pre-processing manifest (core.extraction.manifest); null = not run",
        ),
    )
    op.drop_constraint("ck_pipeline_jobs_type", "pipeline_jobs", type_="check")
    op.create_check_constraint("ck_pipeline_jobs_type", "pipeline_jobs", f"type IN ({TYPES_AFTER})")
    op.alter_column(
        "pipeline_jobs",
        "type",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=COMMENT_AFTER,
        existing_comment=COMMENT_BEFORE,
    )


def downgrade() -> None:
    op.execute("DELETE FROM pipeline_jobs WHERE type = 'preprocess_file'")
    op.alter_column(
        "pipeline_jobs",
        "type",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=COMMENT_BEFORE,
        existing_comment=COMMENT_AFTER,
    )
    op.drop_constraint("ck_pipeline_jobs_type", "pipeline_jobs", type_="check")
    op.create_check_constraint(
        "ck_pipeline_jobs_type", "pipeline_jobs", f"type IN ({TYPES_BEFORE})"
    )
    op.drop_column("stored_files", "preprocess")
