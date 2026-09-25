"""planning_documents: stored file metadata for the source viewer (file_key, page_count,
page_images_rendered)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-23

Every planning value must be traceable to its document in one click (``GET /v1/source/...``).
For that the API needs to know where the document's PDF lives in the private bucket, how many
pages it has (a page beyond that is a 404) and whether page images were rendered. The ingestion
job sets these; until then a document is "not stored" (null / false) and the viewer answers 404.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "planning_documents",
        sa.Column(
            "file_key",
            sa.Text(),
            nullable=True,
            comment="object key of the stored PDF in the private bucket; null = not stored",
        ),
    )
    op.add_column(
        "planning_documents",
        sa.Column(
            "page_count",
            sa.Integer(),
            nullable=True,
            comment="pages in the stored PDF (set at ingestion)",
        ),
    )
    op.add_column(
        "planning_documents",
        sa.Column(
            "page_images_rendered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="page images at <municipality>/planning-documents/<id>/pages/NNNN.png",
        ),
    )
    op.create_check_constraint(
        "ck_planning_documents_page_count",
        "planning_documents",
        "page_count IS NULL OR page_count > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_planning_documents_page_count", "planning_documents", type_="check")
    op.drop_column("planning_documents", "page_images_rendered")
    op.drop_column("planning_documents", "page_count")
    op.drop_column("planning_documents", "file_key")
