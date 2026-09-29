"""geometry review: staged batches carry an origin, topology QA and a reviewer's decision

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-15

The A2 check of the review queue (pilot technical scope §5 ``staging.geometry_draft``: origin
vector_pdf / manual_qgis / official_gis, ``qa_status`` / ``qa_issues`` overlaps, gaps, area
deviation, ``review_status`` / ``reviewed_by``; §8.2 A2): staged geometry is reviewed like the
extracted values before the publish job may apply it.

- ``geometry_batches.origin``: where the drawing came from (georeferenced vector sheets, a QGIS
  redraw or drawing, an official GIS file such as the cadastre or a supplied plan drawing).
- ``document_id`` / ``dataset_version``: the planning document and the producing dataset's label
  (georeferencing, zone import, cadastral import), so the queue groups and names batches.
- ``qa_status`` pass | warn | fail and ``qa_issues`` (``core.geometry_qa``): computed when the
  batch is staged; null for batches staged before this revision (``python -m core.geometry_qa
  recheck`` computes them).
- ``review_state`` pending_review | approved | rejected with ``reviewer``, ``reviewed_by_user_id``,
  ``reviewed_at`` and ``review_note``. A new batch starts pending (server default); batches
  published or superseded before this revision are null (published before geometry review).

Backfill: origin, document and dataset label from the georeferencing, zone and cadastral datasets
that list the batch; rejected batches say ``rejected``. A downgrade drops the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORIGIN_COMMENT = (
    "vector_pdf (drawing layers read from a vector plan PDF) | manual_qgis (drawn or redrawn in "
    "QGIS) | official_gis (an official GIS file: a supplied plan drawing, the cadastre)"
)
QA_STATUS_COMMENT = "topology QA (core.geometry_qa): pass | warn | fail; null = not checked yet"
REVIEW_STATE_COMMENT = (
    "pending_review | approved | rejected; null = published before geometry review (0033)"
)

BACKFILL = [
    # batches that were never decided: published / superseded before the review step
    "UPDATE geometry_batches SET review_state = NULL WHERE status IN ('published', 'superseded')",
    "UPDATE geometry_batches SET review_state = 'rejected' WHERE status = 'rejected'",
    # georeferencing runs list their batches as {layer: batch id}
    """
    UPDATE geometry_batches b
    SET origin = CASE g.source WHEN 'extraction' THEN 'vector_pdf'
                               WHEN 'manual_redraw' THEN 'manual_qgis'
                               ELSE 'official_gis' END,
        document_id = g.document_id,
        dataset_version = g.dataset_version
    FROM georef_datasets g, jsonb_each_text(g.batches) AS layer(layer_id, batch_id)
    WHERE CAST(layer.batch_id AS bigint) = b.id AND b.municipality_id = g.municipality_id
    """,
    """
    UPDATE geometry_batches b
    SET origin = 'manual_qgis', dataset_version = z.dataset_version
    FROM zone_datasets z
    WHERE z.zones_batch_id = b.id
    """,
    """
    UPDATE geometry_batches b
    SET origin = 'official_gis', dataset_version = c.dataset_version
    FROM cadastral_datasets c
    WHERE b.id IN (c.parcels_batch_id, c.ko_batch_id)
    """,
    # anything else the producer named
    """
    UPDATE geometry_batches
    SET origin = CASE WHEN produced_by LIKE 'import_cadastre:%' THEN 'official_gis'
                      WHEN produced_by LIKE 'import_zones:%' THEN 'manual_qgis' END,
        dataset_version = COALESCE(dataset_version, qa_report->>'dataset_version')
    WHERE origin IS NULL
    """,
]


def upgrade() -> None:
    op.add_column(
        "geometry_batches",
        sa.Column("origin", sa.Text(), nullable=True, comment=ORIGIN_COMMENT),
    )
    op.add_column(
        "geometry_batches",
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_documents.id", ondelete="SET NULL"),
            nullable=True,
            comment="the planning document a georeferenced or drawn batch belongs to",
        ),
    )
    op.add_column(
        "geometry_batches",
        sa.Column(
            "dataset_version",
            sa.Text(),
            nullable=True,
            comment="label of the producing dataset (georeferencing, zone or cadastral import)",
        ),
    )
    op.add_column(
        "geometry_batches",
        sa.Column("qa_status", sa.Text(), nullable=True, comment=QA_STATUS_COMMENT),
    )
    op.add_column(
        "geometry_batches",
        sa.Column(
            "qa_issues",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="overlaps, gaps, area deviation, invalid geometry, the dataset's warnings",
        ),
    )
    op.add_column(
        "geometry_batches",
        sa.Column(
            "review_state",
            sa.Text(),
            nullable=True,
            server_default=sa.text("'pending_review'"),
            comment=REVIEW_STATE_COMMENT,
        ),
    )
    op.add_column("geometry_batches", sa.Column("reviewer", sa.Text(), nullable=True))
    op.add_column(
        "geometry_batches",
        sa.Column(
            "reviewed_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "geometry_batches",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("geometry_batches", sa.Column("review_note", sa.Text(), nullable=True))
    for statement in BACKFILL:
        op.execute(statement)
    op.create_check_constraint(
        "ck_geometry_batches_origin",
        "geometry_batches",
        "origin IN ('vector_pdf', 'manual_qgis', 'official_gis')",
    )
    op.create_check_constraint(
        "ck_geometry_batches_qa_status",
        "geometry_batches",
        "qa_status IN ('pass', 'warn', 'fail')",
    )
    op.create_check_constraint(
        "ck_geometry_batches_review_state",
        "geometry_batches",
        "review_state IN ('pending_review', 'approved', 'rejected')",
    )
    op.create_index(
        "ix_geometry_batches_review",
        "geometry_batches",
        ["municipality_id", "review_state"],
        postgresql_where=sa.text("status = 'staged'"),
    )


def downgrade() -> None:
    op.drop_index("ix_geometry_batches_review", table_name="geometry_batches")
    for name in ("review_state", "qa_status", "origin"):
        op.drop_constraint(f"ck_geometry_batches_{name}", "geometry_batches", type_="check")
    for column in (
        "review_note",
        "reviewed_at",
        "reviewed_by_user_id",
        "reviewer",
        "review_state",
        "qa_issues",
        "qa_status",
        "dataset_version",
        "document_id",
        "origin",
    ):
        op.drop_column("geometry_batches", column)
