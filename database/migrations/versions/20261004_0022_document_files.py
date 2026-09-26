"""planning_document_files: several files per document version; values cite their file

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-04

A planning document version is often several PDFs (the text part, the drawing sheets, annexes).
``planning_document_files`` links a version to its stored files with a ``role``: ``text`` (read
by the extraction job), ``drawing`` (read by the geometry job) or ``both``. Each file is
extracted on its own (``extraction_runs.file_id`` already records the file a run read), so a
value cites the file it came from: ``planning_parameter_values.source_file_id`` (set by the
publish job from the item's run; null on seeded rows = the document's own ``file_key``).

``planning_documents.file_id`` / ``file_key`` / ``page_count`` stay as the version's primary file
(the first text / both file): the source viewer's document route and the page images use it.
Backfill: every document with a ``file_id`` gets that file as ``both`` (the old single-file model
used it for everything), every existing value its document's file.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "planning_document_files",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            sa.BigInteger(),
            sa.ForeignKey("stored_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'text'"),
            comment="text (extraction) | drawing (geometry) | both",
        ),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="display order within the version",
        ),
        sa.Column("added_by", sa.Text(), comment="principal subject"),
        sa.Column(
            "added_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "role IN ('text', 'drawing', 'both')", name="ck_planning_document_files_role"
        ),
    )
    op.create_index(
        "uq_planning_document_files_document_file",
        "planning_document_files",
        ["document_id", "file_id"],
        unique=True,
    )
    op.create_index("ix_planning_document_files_file", "planning_document_files", ["file_id"])
    op.execute(
        """
        INSERT INTO planning_document_files (municipality_id, document_id, file_id, role,
                                             position, added_by, added_at)
        SELECT municipality_id, id, file_id, 'both', 0, registered_by,
               COALESCE(registered_at, created_at)
        FROM planning_documents
        WHERE file_id IS NOT NULL
        """
    )

    op.add_column(
        "planning_parameter_values",
        sa.Column(
            "source_file_id",
            sa.BigInteger(),
            sa.ForeignKey("stored_files.id", ondelete="SET NULL"),
            comment="the stored file the value is cited from; null = the document's file_key",
        ),
    )
    op.execute(
        """
        UPDATE planning_parameter_values v
        SET source_file_id = d.file_id
        FROM planning_documents d
        WHERE d.id = v.document_id AND d.file_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("planning_parameter_values", "source_file_id")
    op.drop_index("ix_planning_document_files_file", table_name="planning_document_files")
    op.drop_index("uq_planning_document_files_document_file", table_name="planning_document_files")
    op.drop_table("planning_document_files")
