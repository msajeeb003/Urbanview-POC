"""choropleth_cells / choropleth_classes: the heatmap surfaces, one row per layer and cell

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-09

The map's heatmaps (BRD §2.1) come from ``core.choropleth``: per publish version, one row per layer
(coverage | far | height | gfa per urban block, sale_price per zone) and cell with its value, its
legend band, unit, label, where it comes from (planning values or the assumptions version) and the
version labels; the classes each layer's bands and legend use are stored next to them
(``choropleth_classes``), so the legend the API serves is what the tiles were built with. They
replace the wide ``heatmap_cells`` of 0011 (dropped; the next publish or ``refresh_heatmaps`` job
fills the new tables). ``refresh_heatmaps`` becomes a job type: it recomputes the cells and rebuilds
the current version's tiles when another assumptions version applies.
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LAYERS = "layer IN ('coverage', 'far', 'height', 'gfa', 'sale_price')"
JOB_TYPES_BEFORE = (
    "'extract_document', 'preprocess_file', 'process_geometry', 'publish_approved', "
    "'send_email', 'import_market_data'"
)
JOB_TYPES_AFTER = JOB_TYPES_BEFORE + ", 'refresh_heatmaps'"
TYPE_COMMENT_BEFORE = (
    "extract_document | preprocess_file | process_geometry | publish_approved | send_email"
    " | import_market_data"
)
TYPE_COMMENT_AFTER = TYPE_COMMENT_BEFORE + " | refresh_heatmaps"


def _geometry() -> geoalchemy2.Geometry:
    return geoalchemy2.Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False)


def upgrade() -> None:
    op.create_table(
        "choropleth_cells",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "publish_version_id",
            sa.BigInteger(),
            sa.ForeignKey("publish_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "layer", sa.Text(), nullable=False, comment="coverage | far | height | gfa | sale_price"
        ),
        sa.Column("cell_type", sa.Text(), nullable=False, comment="block | zone"),
        sa.Column("cell_id", sa.BigInteger(), nullable=False, comment="urban_blocks.id | zones.id"),
        sa.Column("cell_ref", sa.Text(), comment="block ref | zone name"),
        sa.Column("geom", _geometry(), nullable=False),
        sa.Column("value", sa.Float(precision=53), nullable=False),
        sa.Column(
            "value_low",
            sa.Float(precision=53),
            comment="sale_price: low sale rate (absolute bound, else expected x low factor)",
        ),
        sa.Column(
            "value_high",
            sa.Float(precision=53),
            comment="sale_price: high sale rate (absolute bound, else expected x high factor)",
        ),
        sa.Column(
            "value_band",
            sa.Integer(),
            nullable=False,
            comment="the cell's legend row: number of breaks <= value; sale_price: 0 = not "
            "saleable, then 1 + that number",
        ),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), comment="height: the floor notation as the plan prints it"),
        sa.Column(
            "parcel_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="planned parcels the value comes from",
        ),
        sa.Column("source_kind", sa.Text(), nullable=False, comment="planning | assumptions"),
        sa.Column("dataset_version", sa.Text(), comment="label of the publish version"),
        sa.Column(
            "assumptions_id",
            sa.BigInteger(),
            sa.ForeignKey("financial_assumptions.id", ondelete="SET NULL"),
            comment="sale_price: the financial_assumptions version the value comes from",
        ),
        sa.Column(
            "assumptions_version",
            sa.Text(),
            comment="sale_price: that version's number in the zone's history",
        ),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "publish_version_id", "layer", "cell_id", name="uq_choropleth_cells_version_layer_cell"
        ),
        sa.CheckConstraint(LAYERS, name="ck_choropleth_cells_layer"),
        sa.CheckConstraint("cell_type IN ('block', 'zone')", name="ck_choropleth_cells_type"),
        sa.CheckConstraint(
            "source_kind IN ('planning', 'assumptions')", name="ck_choropleth_cells_source"
        ),
    )
    op.create_index(
        "idx_choropleth_cells_geom", "choropleth_cells", ["geom"], postgresql_using="gist"
    )
    op.create_table(
        "choropleth_classes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "publish_version_id",
            sa.BigInteger(),
            sa.ForeignKey("publish_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("layer", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False, comment="quantile | fixed"),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column(
            "breaks",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="class starts after the first class, ascending: v is in class = number of "
            "breaks <= v",
        ),
        sa.Column("min", sa.Float(precision=53)),
        sa.Column("max", sa.Float(precision=53)),
        sa.Column("mean", sa.Float(precision=53)),
        sa.Column(
            "count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="cells with a value",
        ),
        sa.Column(
            "null_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="blocks / zones without a value: drawn as not covered",
        ),
        sa.Column(
            "zero_class",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="0 is its own class first (sale price: not saleable)",
        ),
        sa.Column("decimals", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "publish_version_id", "layer", name="uq_choropleth_classes_version_layer"
        ),
        sa.CheckConstraint(LAYERS, name="ck_choropleth_classes_layer"),
        sa.CheckConstraint("method IN ('quantile', 'fixed')", name="ck_choropleth_classes_method"),
    )
    op.drop_index("idx_heatmap_cells_geom", table_name="heatmap_cells")
    op.drop_table("heatmap_cells")
    op.drop_constraint("ck_pipeline_jobs_type", "pipeline_jobs", type_="check")
    op.create_check_constraint(
        "ck_pipeline_jobs_type", "pipeline_jobs", f"type IN ({JOB_TYPES_AFTER})"
    )
    op.alter_column(
        "pipeline_jobs",
        "type",
        comment=TYPE_COMMENT_AFTER,
        existing_comment=TYPE_COMMENT_BEFORE,
        existing_type=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.execute("DELETE FROM pipeline_jobs WHERE type = 'refresh_heatmaps'")
    op.alter_column(
        "pipeline_jobs",
        "type",
        comment=TYPE_COMMENT_BEFORE,
        existing_comment=TYPE_COMMENT_AFTER,
        existing_type=sa.Text(),
        existing_nullable=False,
    )
    op.drop_constraint("ck_pipeline_jobs_type", "pipeline_jobs", type_="check")
    op.create_check_constraint(
        "ck_pipeline_jobs_type", "pipeline_jobs", f"type IN ({JOB_TYPES_BEFORE})"
    )
    op.create_table(
        "heatmap_cells",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "publish_version_id",
            sa.BigInteger(),
            sa.ForeignKey("publish_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cell_type", sa.Text(), nullable=False, comment="block | zone"),
        sa.Column("cell_id", sa.BigInteger(), nullable=False, comment="urban_blocks.id | zones.id"),
        sa.Column("cell_ref", sa.Text(), nullable=True, comment="block ref | zone name"),
        sa.Column("geom", _geometry(), nullable=False),
        sa.Column("parcel_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "stated_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="parcels with at least one heatmap parameter",
        ),
        sa.Column("max_site_coverage_pct", sa.Float(precision=53), nullable=True),
        sa.Column("max_height_m", sa.Float(precision=53), nullable=True),
        sa.Column("max_far", sa.Float(precision=53), nullable=True),
        sa.Column("max_gfa_m2", sa.Float(precision=53), nullable=True),
        sa.Column("saleable_area_m2", sa.Float(precision=53), nullable=True),
        sa.Column("sale_rate_eur_m2", sa.Float(precision=53), nullable=True),
        sa.Column("market_value_eur", sa.Float(precision=53), nullable=True),
        sa.Column(
            "price_band",
            sa.Integer(),
            nullable=True,
            comment="1 (lowest) .. 3 (highest) tercile of the zone sale rates",
        ),
        sa.Column(
            "sale_rate_low_eur_m2",
            sa.Float(precision=53),
            nullable=True,
            comment="Low sale rate €/m²: absolute bound, else expected × low factor",
        ),
        sa.Column(
            "sale_rate_high_eur_m2",
            sa.Float(precision=53),
            nullable=True,
            comment="High sale rate €/m²: absolute bound, else expected × high factor",
        ),
        sa.CheckConstraint("cell_type IN ('block', 'zone')", name="ck_heatmap_cells_type"),
        sa.UniqueConstraint(
            "publish_version_id", "cell_type", "cell_id", name="uq_heatmap_cells_version_cell"
        ),
    )
    op.create_index("idx_heatmap_cells_geom", "heatmap_cells", ["geom"], postgresql_using="gist")
    op.drop_table("choropleth_classes")
    op.drop_index("idx_choropleth_cells_geom", table_name="choropleth_cells")
    op.drop_table("choropleth_cells")
