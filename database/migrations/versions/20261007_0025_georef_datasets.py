"""georef_datasets: georeferenced plan geometry with its transform and residuals

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-07

``core.gis.georef`` fits a Helmert or affine transform from a planning document's sheets (control
points in PDF points) to the plan's projected CRS, applies it to every extracted layer, reprojects
to EPSG:4326 with GDAL, snaps planned parcels and blocks to the cadastral base and stages the
result for the publish job. One row per run records the CRS, the transform with every residual,
the RMSE overall and per sheet (shown on the document in the admin console), the snapping, the
validation and the staged batches.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "georef_datasets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_documents.id"),
            nullable=False,
        ),
        sa.Column("dataset_version", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'staged'"),
            comment="staged | invalid | published | superseded",
        ),
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'extraction'"),
            comment="extraction (vector sheets) | manual_redraw (scanned sheets redrawn in QGIS)",
        ),
        sa.Column("crs", sa.Text(), nullable=False, comment="the plan's projected CRS"),
        sa.Column("method", sa.Text(), nullable=False, comment="helmert | affine"),
        sa.Column(
            "transform",
            postgresql.JSONB(),
            nullable=False,
            comment="parameters, per-sheet page -> CRS parameters, residuals, control-point hash",
        ),
        sa.Column("rmse_m", sa.Float(precision=53), nullable=False),
        sa.Column("max_residual_m", sa.Float(precision=53)),
        sa.Column("points_used", sa.Integer(), nullable=False),
        sa.Column(
            "sheets",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="per sheet: sheet, page, points, rmse_m",
        ),
        sa.Column(
            "snap",
            postgresql.JSONB(),
            comment="snapping to the cadastral base: tolerance, vertices moved, near misses",
        ),
        sa.Column("validation", postgresql.JSONB()),
        sa.Column(
            "batches",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="staged layer -> geometry_batches.id",
        ),
        sa.Column(
            "output_sha256",
            sa.Text(),
            comment="digest of the georeferenced features: a re-run must reproduce it",
        ),
        sa.Column(
            "gpkg_key",
            sa.Text(),
            comment="the georeferenced GeoPackage in the private bucket, when stored",
        ),
        sa.Column("imported_by", sa.Text()),
        sa.Column(
            "published_version_id",
            sa.BigInteger(),
            sa.ForeignKey("publish_versions.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "municipality_id", "dataset_version", name="uq_georef_datasets_version"
        ),
        sa.CheckConstraint(
            "status IN ('staged', 'invalid', 'published', 'superseded')",
            name="ck_georef_datasets_status",
        ),
        sa.CheckConstraint(
            "source IN ('extraction', 'manual_redraw')", name="ck_georef_datasets_source"
        ),
        sa.CheckConstraint("method IN ('helmert', 'affine')", name="ck_georef_datasets_method"),
    )
    op.create_index("ix_georef_datasets_document", "georef_datasets", ["document_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_georef_datasets_document", table_name="georef_datasets")
    op.drop_table("georef_datasets")
