"""publish pipeline: version-scoped serving values (block / zone scope), staging geometry and
batches, layer features, parcel links, heatmap cells, archive columns on publish_versions, job
progress

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-25

The publish job (``jobs/publish_pipeline.py``) writes a complete serving set per
``publish_versions`` row: ``planning_parameter_values`` (now unique per version, with optional
block / zone scope), ``layer_features`` (planned land use, traffic network), ``parcel_links``
(cadastral ↔ planned parcel overlaps) and ``heatmap_cells`` (block / zone aggregates), plus one
PMTiles archive per version in the private bucket (``archive_key``). The public API reads the
version flagged ``is_current``; a rollback flips that flag. ``staging_geometry`` /
``geometry_batches`` are what the GIS ingestion job fills and the publish job consumes.
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _geometry(kind: str) -> geoalchemy2.Geometry:
    return geoalchemy2.Geometry(geometry_type=kind, srid=4326, spatial_index=False)


def upgrade() -> None:
    # --- publish_versions: the archive and the run behind each version -------------------------
    op.add_column(
        "publish_versions",
        sa.Column(
            "archive_key", sa.Text(), nullable=True, comment="PMTiles object key (private bucket)"
        ),
    )
    op.add_column(
        "publish_versions", sa.Column("archive_size_bytes", sa.BigInteger(), nullable=True)
    )
    op.add_column("publish_versions", sa.Column("archive_sha256", sa.Text(), nullable=True))
    op.add_column(
        "publish_versions",
        sa.Column(
            "layers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="source layers in the archive with feature counts and zoom ranges",
        ),
    )
    op.add_column(
        "publish_versions",
        sa.Column(
            "counts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="what the publish run copied / computed",
        ),
    )
    op.add_column("publish_versions", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.add_column("publish_versions", sa.Column("min_zoom", sa.Integer(), nullable=True))
    op.add_column("publish_versions", sa.Column("max_zoom", sa.Integer(), nullable=True))
    op.add_column(
        "publish_versions",
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey("pipeline_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "publish_versions",
        sa.Column(
            "previous_version_id",
            sa.BigInteger(),
            sa.ForeignKey("publish_versions.id", ondelete="SET NULL"),
            nullable=True,
            comment="the version that was current when this one was published",
        ),
    )
    op.add_column(
        "publish_versions", sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("publish_versions", sa.Column("rolled_back_by", sa.Text(), nullable=True))
    op.add_column(
        "publish_versions",
        sa.Column(
            "archive_pruned_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="retention removed the archive object; the version cannot be restored",
        ),
    )

    # --- planning_parameter_values: unique per version, block / zone scope ---------------------
    op.add_column(
        "planning_parameter_values",
        sa.Column(
            "block_id",
            sa.BigInteger(),
            sa.ForeignKey("urban_blocks.id", ondelete="CASCADE"),
            nullable=True,
            comment="block-level value",
        ),
    )
    op.add_column(
        "planning_parameter_values",
        sa.Column(
            "zone_id",
            sa.BigInteger(),
            sa.ForeignKey("zones.id", ondelete="CASCADE"),
            nullable=True,
            comment="zone-level value",
        ),
    )
    op.create_check_constraint(
        "ck_planning_parameter_values_one_scope",
        "planning_parameter_values",
        "num_nonnulls(urban_parcel_id, block_id, zone_id) <= 1",
    )
    op.drop_index("uq_planning_parameter_values_parcel", table_name="planning_parameter_values")
    op.drop_index("uq_planning_parameter_values_document", table_name="planning_parameter_values")
    op.create_index(
        "uq_planning_parameter_values_parcel",
        "planning_parameter_values",
        ["publish_version_id", "urban_parcel_id", "field_key"],
        unique=True,
        postgresql_where=sa.text("urban_parcel_id IS NOT NULL"),
    )
    op.create_index(
        "uq_planning_parameter_values_block",
        "planning_parameter_values",
        ["publish_version_id", "block_id", "field_key"],
        unique=True,
        postgresql_where=sa.text("block_id IS NOT NULL"),
    )
    op.create_index(
        "uq_planning_parameter_values_zone",
        "planning_parameter_values",
        ["publish_version_id", "zone_id", "field_key"],
        unique=True,
        postgresql_where=sa.text("zone_id IS NOT NULL"),
    )
    op.create_index(
        "uq_planning_parameter_values_document",
        "planning_parameter_values",
        ["publish_version_id", "document_id", "field_key"],
        unique=True,
        postgresql_where=sa.text(
            "urban_parcel_id IS NULL AND block_id IS NULL AND zone_id IS NULL"
        ),
    )

    # --- pipeline_jobs.progress --------------------------------------------------------------
    op.add_column(
        "pipeline_jobs",
        sa.Column(
            "progress",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="per-step progress written by the running task",
        ),
    )

    # --- staging geometry: what the GIS ingestion job produces -------------------------------
    op.create_table(
        "geometry_batches",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "file_id",
            sa.BigInteger(),
            sa.ForeignKey("stored_files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "layer_id",
            sa.Text(),
            nullable=False,
            comment="cadastral_parcels | urban_parcels | urban_blocks | zones | document_coverage "
            "| land_use | traffic_network",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'staged'"),
            comment="staged | published | superseded | rejected",
        ),
        sa.Column("feature_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("produced_by", sa.Text(), nullable=True, comment="job or principal"),
        sa.Column("qa_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "published_version_id",
            sa.BigInteger(),
            sa.ForeignKey("publish_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('staged', 'published', 'superseded', 'rejected')",
            name="ck_geometry_batches_status",
        ),
    )
    op.create_index(
        "ix_geometry_batches_layer_status",
        "geometry_batches",
        ["municipality_id", "layer_id", "status"],
    )
    op.create_table(
        "staging_geometry",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "batch_id",
            sa.BigInteger(),
            sa.ForeignKey("geometry_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("layer_id", sa.Text(), nullable=False),
        sa.Column(
            "feature_key",
            sa.Text(),
            nullable=False,
            comment="natural key within the layer (e.g. 'Podgorica I|1042|' for a cadastral parcel)",
        ),
        sa.Column("geom", _geometry("GEOMETRY"), nullable=False),
        sa.Column(
            "properties",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("batch_id", "feature_key", name="uq_staging_geometry_batch_key"),
    )
    op.create_index(
        "idx_staging_geometry_geom", "staging_geometry", ["geom"], postgresql_using="gist"
    )

    # --- versioned serving layers, links and cells -------------------------------------------
    op.create_table(
        "layer_features",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "publish_version_id",
            sa.BigInteger(),
            sa.ForeignKey("publish_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("layer_id", sa.Text(), nullable=False, comment="land_use | traffic_network"),
        sa.Column("feature_key", sa.Text(), nullable=False),
        sa.Column("geom", _geometry("GEOMETRY"), nullable=False),
        sa.Column(
            "properties",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.UniqueConstraint(
            "publish_version_id", "layer_id", "feature_key", name="uq_layer_features_version_key"
        ),
    )
    op.create_index("idx_layer_features_geom", "layer_features", ["geom"], postgresql_using="gist")
    op.create_table(
        "parcel_links",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "publish_version_id",
            sa.BigInteger(),
            sa.ForeignKey("publish_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "cadastral_parcel_id",
            sa.BigInteger(),
            sa.ForeignKey("cadastral_parcels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "urban_parcel_id",
            sa.BigInteger(),
            sa.ForeignKey("urban_parcels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("overlap_m2", sa.Float(precision=53), nullable=False),
        sa.Column(
            "overlap_fraction",
            sa.Float(precision=53),
            nullable=False,
            comment="overlap / cadastral area",
        ),
        sa.Column(
            "area_delta_m2",
            sa.Float(precision=53),
            nullable=False,
            comment="planned area - cadastral area",
        ),
        sa.Column("rank", sa.Integer(), nullable=False, comment="1 = primary link"),
        sa.UniqueConstraint(
            "publish_version_id",
            "cadastral_parcel_id",
            "urban_parcel_id",
            name="uq_parcel_links_version_pair",
        ),
    )
    op.create_index(
        "ix_parcel_links_urban", "parcel_links", ["publish_version_id", "urban_parcel_id"]
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
        sa.Column("geom", _geometry("MULTIPOLYGON"), nullable=False),
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
        sa.CheckConstraint("cell_type IN ('block', 'zone')", name="ck_heatmap_cells_type"),
        sa.UniqueConstraint(
            "publish_version_id", "cell_type", "cell_id", name="uq_heatmap_cells_version_cell"
        ),
    )
    op.create_index("idx_heatmap_cells_geom", "heatmap_cells", ["geom"], postgresql_using="gist")


def downgrade() -> None:
    op.drop_index("idx_heatmap_cells_geom", table_name="heatmap_cells")
    op.drop_table("heatmap_cells")
    op.drop_index("ix_parcel_links_urban", table_name="parcel_links")
    op.drop_table("parcel_links")
    op.drop_index("idx_layer_features_geom", table_name="layer_features")
    op.drop_table("layer_features")
    op.drop_index("idx_staging_geometry_geom", table_name="staging_geometry")
    op.drop_table("staging_geometry")
    op.drop_index("ix_geometry_batches_layer_status", table_name="geometry_batches")
    op.drop_table("geometry_batches")
    op.drop_column("pipeline_jobs", "progress")
    op.drop_index("uq_planning_parameter_values_document", table_name="planning_parameter_values")
    op.drop_index("uq_planning_parameter_values_zone", table_name="planning_parameter_values")
    op.drop_index("uq_planning_parameter_values_block", table_name="planning_parameter_values")
    op.drop_index("uq_planning_parameter_values_parcel", table_name="planning_parameter_values")
    # rows of non-current versions would collide in the version-less indexes
    op.execute(
        """
        DELETE FROM planning_parameter_values v
        WHERE block_id IS NOT NULL OR zone_id IS NOT NULL
           OR NOT EXISTS (SELECT 1 FROM publish_versions p
                          WHERE p.id = v.publish_version_id AND p.is_current)
        """
    )
    op.create_index(
        "uq_planning_parameter_values_parcel",
        "planning_parameter_values",
        ["urban_parcel_id", "field_key"],
        unique=True,
        postgresql_where=sa.text("urban_parcel_id IS NOT NULL"),
    )
    op.create_index(
        "uq_planning_parameter_values_document",
        "planning_parameter_values",
        ["document_id", "field_key"],
        unique=True,
        postgresql_where=sa.text("urban_parcel_id IS NULL"),
    )
    op.drop_constraint(
        "ck_planning_parameter_values_one_scope", "planning_parameter_values", type_="check"
    )
    op.drop_column("planning_parameter_values", "zone_id")
    op.drop_column("planning_parameter_values", "block_id")
    for column in (
        "archive_pruned_at",
        "rolled_back_by",
        "rolled_back_at",
        "previous_version_id",
        "job_id",
        "max_zoom",
        "min_zoom",
        "duration_ms",
        "counts",
        "layers",
        "archive_sha256",
        "archive_size_bytes",
        "archive_key",
    ):
        op.drop_column("publish_versions", column)
