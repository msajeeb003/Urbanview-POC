"""parcel_links: relation (same / reduced / enlarged / split / merged / none), both ratios, both areas

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-08

The cadastral <-> planned parcel links (BRD §2.2, ``core.parcel_links``) gain what the panel states
about the correspondence: the overlap as a share of either parcel (``overlap_m2`` and
``overlap_fraction`` renamed ``overlap_area_m2`` and ``overlap_ratio_of_cadastral``, plus
``overlap_ratio_of_urban``), both areas as the cadastre and the plan state them, the relation of the
cadastral parcel, the share taken for roads / public space (``reduction_pct``) and the version
label. A cadastral parcel no planned parcel covers gets a ``none`` row (``urban_parcel_id`` null).
``publish_versions.links_summary`` keeps each version's counts and recompute time.

Existing links are classified in place (a frozen copy of the rules: split 10 %, same 2 %, the
stored areas) and the versions still holding their links get their ``none`` rows; the next publish
or ``python -m core.parcel_links recompute`` recomputes them in the metric CRS.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "parcel_links"
RELATIONS = "relation IN ('same', 'reduced', 'enlarged', 'split', 'merged', 'none')"
NONE_RULE = "(urban_parcel_id IS NULL) = (relation = 'none')"

BACKFILL_COLUMNS = """
UPDATE parcel_links l
SET dataset_version = v.label,
    cadastral_area_m2 = c.area_m2,
    urban_area_m2 = u.area_m2,
    overlap_ratio_of_urban = CASE WHEN u.area_m2 > 0 THEN l.overlap_area_m2 / u.area_m2 END
FROM publish_versions v, cadastral_parcels c, urban_parcels u
WHERE v.id = l.publish_version_id AND c.id = l.cadastral_parcel_id AND u.id = l.urban_parcel_id
"""
BACKFILL_RELATIONS = """
WITH per_cad AS (
    SELECT publish_version_id, cadastral_parcel_id,
           count(*) FILTER (WHERE overlap_ratio_of_cadastral >= 0.1) AS significant,
           sum(overlap_ratio_of_cadastral) AS covered
    FROM parcel_links GROUP BY publish_version_id, cadastral_parcel_id
), per_up AS (
    SELECT publish_version_id, urban_parcel_id,
           count(*) FILTER (WHERE overlap_ratio_of_cadastral >= 0.1) AS significant
    FROM parcel_links GROUP BY publish_version_id, urban_parcel_id
), cases AS (
    SELECT p.publish_version_id, p.cadastral_parcel_id,
           CASE WHEN pc.significant >= 2 THEN 'split'
                WHEN p.overlap_ratio_of_cadastral >= 0.1 AND pu.significant >= 2 THEN 'merged'
                WHEN p.overlap_ratio_of_cadastral < 0.1 THEN 'reduced'
                WHEN p.overlap_ratio_of_cadastral >= 0.98
                     AND COALESCE(p.overlap_ratio_of_urban, 0) >= 0.98 THEN 'same'
                WHEN p.urban_area_m2 > p.cadastral_area_m2 * 1.02 THEN 'enlarged'
                ELSE 'reduced' END AS relation,
           round(CAST(greatest(0, 1 - pc.covered) * 100 AS numeric), 2) AS reduction_pct
    FROM parcel_links p
    JOIN per_cad pc ON pc.publish_version_id = p.publish_version_id
         AND pc.cadastral_parcel_id = p.cadastral_parcel_id
    JOIN per_up pu ON pu.publish_version_id = p.publish_version_id
         AND pu.urban_parcel_id = p.urban_parcel_id
    WHERE p.rank = 1
)
UPDATE parcel_links l
SET relation = c.relation, reduction_pct = c.reduction_pct
FROM cases c
WHERE c.publish_version_id = l.publish_version_id AND c.cadastral_parcel_id = l.cadastral_parcel_id
"""
BACKFILL_NONE = """
INSERT INTO parcel_links (municipality_id, publish_version_id, dataset_version,
                          cadastral_parcel_id, urban_parcel_id, cadastral_area_m2,
                          overlap_area_m2, overlap_ratio_of_cadastral, relation, rank)
SELECT v.municipality_id, v.id, v.label, c.id, NULL, c.area_m2, 0, 0, 'none', 1
FROM publish_versions v
JOIN cadastral_parcels c ON c.municipality_id = v.municipality_id AND c.retired_at IS NULL
WHERE v.archive_pruned_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM parcel_links l
                  WHERE l.publish_version_id = v.id AND l.cadastral_parcel_id = c.id)
"""


def upgrade() -> None:
    op.alter_column(
        TABLE,
        "overlap_m2",
        new_column_name="overlap_area_m2",
        comment="intersection area in the municipality's metric CRS",
        existing_type=sa.Float(precision=53),
        existing_nullable=False,
    )
    op.alter_column(
        TABLE,
        "overlap_fraction",
        new_column_name="overlap_ratio_of_cadastral",
        comment="overlap / cadastral parcel area (metric CRS)",
        existing_comment="overlap / cadastral area",
        existing_type=sa.Float(precision=53),
        existing_nullable=False,
    )
    op.add_column(
        TABLE,
        sa.Column(
            "dataset_version",
            sa.Text(),
            comment="label of the publish version the links were computed for",
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "cadastral_area_m2",
            sa.Float(precision=53),
            comment="the cadastre's area of the cadastral parcel",
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "urban_area_m2", sa.Float(precision=53), comment="the plan's area of the planned parcel"
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "overlap_ratio_of_urban",
            sa.Float(precision=53),
            comment="overlap / planned parcel area (metric CRS)",
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "relation", sa.Text(), comment="same | reduced | enlarged | split | merged | none"
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "reduction_pct",
            sa.Float(precision=53),
            comment="share of the cadastral parcel in no planned parcel (roads, public space), %",
        ),
    )
    op.alter_column(
        TABLE,
        "urban_parcel_id",
        nullable=True,
        comment="null = relation none: no planned parcel over the cadastral parcel",
        existing_type=sa.BigInteger(),
    )
    op.alter_column(
        TABLE,
        "area_delta_m2",
        nullable=True,
        existing_type=sa.Float(precision=53),
        existing_comment="planned area - cadastral area",
    )
    op.alter_column(
        TABLE,
        "rank",
        comment="1 = primary link (or the none row)",
        existing_comment="1 = primary link",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.add_column(
        "publish_versions",
        sa.Column(
            "links_summary",
            postgresql.JSONB(),
            comment="parcel links of the version (core.parcel_links): parcels per relation, links, "
            "average reduction %, rules, duration, computed_at",
        ),
    )

    op.execute(BACKFILL_COLUMNS)
    op.execute(BACKFILL_RELATIONS)
    op.execute(BACKFILL_NONE)

    op.alter_column(TABLE, "relation", nullable=False, existing_type=sa.Text())
    op.alter_column(
        TABLE, "cadastral_area_m2", nullable=False, existing_type=sa.Float(precision=53)
    )
    op.create_check_constraint("ck_parcel_links_relation", TABLE, RELATIONS)
    op.create_check_constraint("ck_parcel_links_none", TABLE, NONE_RULE)
    op.create_index(
        "uq_parcel_links_version_none",
        TABLE,
        ["publish_version_id", "cadastral_parcel_id"],
        unique=True,
        postgresql_where=sa.text("urban_parcel_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_parcel_links_version_none", table_name=TABLE)
    op.drop_constraint("ck_parcel_links_none", TABLE, type_="check")
    op.drop_constraint("ck_parcel_links_relation", TABLE, type_="check")
    op.execute("DELETE FROM parcel_links WHERE urban_parcel_id IS NULL")
    op.drop_column("publish_versions", "links_summary")
    op.alter_column(
        TABLE,
        "rank",
        comment="1 = primary link",
        existing_comment="1 = primary link (or the none row)",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        TABLE,
        "area_delta_m2",
        nullable=False,
        existing_type=sa.Float(precision=53),
        existing_comment="planned area - cadastral area",
    )
    op.alter_column(
        TABLE,
        "urban_parcel_id",
        nullable=False,
        comment=None,
        existing_comment="null = relation none: no planned parcel over the cadastral parcel",
        existing_type=sa.BigInteger(),
    )
    for column in (
        "reduction_pct",
        "relation",
        "overlap_ratio_of_urban",
        "urban_area_m2",
        "cadastral_area_m2",
        "dataset_version",
    ):
        op.drop_column(TABLE, column)
    op.alter_column(
        TABLE,
        "overlap_ratio_of_cadastral",
        new_column_name="overlap_fraction",
        comment="overlap / cadastral area",
        existing_comment="overlap / cadastral parcel area (metric CRS)",
        existing_type=sa.Float(precision=53),
        existing_nullable=False,
    )
    op.alter_column(
        TABLE,
        "overlap_area_m2",
        new_column_name="overlap_m2",
        comment=None,
        existing_comment="intersection area in the municipality's metric CRS",
        existing_type=sa.Float(precision=53),
        existing_nullable=False,
    )
