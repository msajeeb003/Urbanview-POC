"""parcel panel: planning_value_gaps (serving record of expert-rejected fields per publish
version) and a backfill of parcel_links for current versions published before the pipeline

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-26

The display-shaped parcel panel (``GET /v1/parcels/{id}/panel``) explains a missing planning value
as ``not_in_document`` or ``rejected`` without ever reading the review queue: the publish job
writes one ``planning_value_gaps`` row per field whose extracted value the expert rejected and
nothing replaced, scoped like ``planning_parameter_values`` (parcel / block / zone / document).

The panel takes its calculation basis from ``parcel_links``. A current version published before
the publish pipeline existed (the seeded sample) has none, so they are computed here once with the
documented default thresholds (1 m² and 2 % of the cadastral area, ``LOCATE_MIN_OVERLAP_*``); the
next publish recomputes them with the configured values.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "planning_value_gaps"

BACKFILL_LINKS = """
WITH targets AS (
    SELECT v.id AS version_id, v.municipality_id
    FROM publish_versions v
    WHERE v.is_current
      AND NOT EXISTS (SELECT 1 FROM parcel_links l WHERE l.publish_version_id = v.id)
),
pairs AS (
    SELECT t.version_id, t.municipality_id, c.id AS cadastral_parcel_id,
           u.id AS urban_parcel_id, c.area_m2 AS cad_area, u.area_m2 AS up_area,
           ST_Area(CAST(ST_Intersection(u.geom, c.geom) AS geography)) AS overlap_m2
    FROM targets t
    JOIN cadastral_parcels c ON c.municipality_id = t.municipality_id
    JOIN urban_parcels u ON u.municipality_id = c.municipality_id
         AND ST_Intersects(u.geom, c.geom)
    JOIN planning_documents d ON d.id = u.document_id AND d.status = 'adopted'
         AND d.coverage_live AND d.is_current_version
),
kept AS (
    SELECT *, row_number() OVER (PARTITION BY version_id, cadastral_parcel_id
                                 ORDER BY overlap_m2 DESC, up_area ASC, urban_parcel_id ASC)
              AS rank
    FROM pairs
    WHERE overlap_m2 >= 1.0 AND overlap_m2 >= 0.02 * cad_area
)
INSERT INTO parcel_links (municipality_id, publish_version_id, cadastral_parcel_id,
                          urban_parcel_id, overlap_m2, overlap_fraction, area_delta_m2, rank)
SELECT municipality_id, version_id, cadastral_parcel_id, urban_parcel_id, overlap_m2,
       CASE WHEN cad_area > 0 THEN overlap_m2 / cad_area ELSE 0 END, up_area - cad_area, rank
FROM kept
"""


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "publish_version_id",
            sa.BigInteger(),
            sa.ForeignKey("publish_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_documents.id", ondelete="CASCADE"),
            nullable=False,
            comment="the document the rejected value was extracted from",
        ),
        sa.Column(
            "urban_parcel_id",
            sa.BigInteger(),
            sa.ForeignKey("urban_parcels.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "block_id",
            sa.BigInteger(),
            sa.ForeignKey("urban_blocks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "zone_id",
            sa.BigInteger(),
            sa.ForeignKey("zones.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "field_key", sa.Text(), sa.ForeignKey("planning_fields.key"), nullable=False
        ),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=False,
            comment="rejected: the expert rejected the extracted value and nothing replaced it",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("reason IN ('rejected')", name="ck_planning_value_gaps_reason"),
        sa.CheckConstraint(
            "num_nonnulls(urban_parcel_id, block_id, zone_id) <= 1",
            name="ck_planning_value_gaps_one_scope",
        ),
    )
    op.create_index(
        "uq_planning_value_gaps_parcel",
        TABLE,
        ["publish_version_id", "urban_parcel_id", "field_key"],
        unique=True,
        postgresql_where=sa.text("urban_parcel_id IS NOT NULL"),
    )
    op.create_index(
        "uq_planning_value_gaps_block",
        TABLE,
        ["publish_version_id", "block_id", "field_key"],
        unique=True,
        postgresql_where=sa.text("block_id IS NOT NULL"),
    )
    op.create_index(
        "uq_planning_value_gaps_zone",
        TABLE,
        ["publish_version_id", "zone_id", "field_key"],
        unique=True,
        postgresql_where=sa.text("zone_id IS NOT NULL"),
    )
    op.create_index(
        "uq_planning_value_gaps_document",
        TABLE,
        ["publish_version_id", "document_id", "field_key"],
        unique=True,
        postgresql_where=sa.text(
            "urban_parcel_id IS NULL AND block_id IS NULL AND zone_id IS NULL"
        ),
    )
    op.execute(BACKFILL_LINKS)


def downgrade() -> None:
    op.drop_index("uq_planning_value_gaps_document", table_name=TABLE)
    op.drop_index("uq_planning_value_gaps_zone", table_name=TABLE)
    op.drop_index("uq_planning_value_gaps_block", table_name=TABLE)
    op.drop_index("uq_planning_value_gaps_parcel", table_name=TABLE)
    op.drop_table(TABLE)
    # parcel_links (0011) keep the backfilled rows: they are ordinary links of that version.
