"""documents: short code; geometry from GIS drawings; the zone import job

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-14

The A1 check of the admin Documents and files screen (pilot technical scope §5 / §8.2):

- ``planning_documents.short_code``: the pilot's ``serving.planning_document.short_code``, a short
  reference staff use for a plan ("DUP-NG12"); optional, carried to a new version unless it is
  given again, editable with ``PATCH /v1/admin/documents/{id}``.
- ``georef_datasets``: the geometry job now stages a document's GIS drawing (a QGIS redraw or an
  official GIS file in its own CRS, pilot origins ``manual_qgis`` / ``official_gis``): source
  ``gis_file``, method ``native`` (no control-point fit: the file carries its CRS), so its
  ``rmse_m`` is null.
- ``pipeline_jobs.type`` ``import_zones``: ``POST /v1/admin/zones/import`` validates and stages a
  zone GeoPackage from QGIS on the worker (the pilot's zones import; no zone editor).

A downgrade drops the column, deletes ``import_zones`` jobs, and removes ``gis_file`` / ``native``
datasets (their staged batches stay, superseded).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_TYPES_BEFORE = (
    "'extract_document', 'preprocess_file', 'process_geometry', 'publish_approved', "
    "'send_email', 'import_market_data', 'refresh_heatmaps', 'ai_check'"
)
JOB_TYPES_AFTER = JOB_TYPES_BEFORE + ", 'import_zones'"
TYPE_COMMENT_BEFORE = (
    "extract_document | preprocess_file | process_geometry | publish_approved | send_email"
    " | import_market_data | refresh_heatmaps | ai_check"
)
TYPE_COMMENT_AFTER = TYPE_COMMENT_BEFORE + " | import_zones"
SOURCES_BEFORE = "'extraction', 'manual_redraw'"
SOURCES_AFTER = SOURCES_BEFORE + ", 'gis_file'"
METHODS_BEFORE = "'helmert', 'affine'"
METHODS_AFTER = METHODS_BEFORE + ", 'native'"
SOURCE_COMMENT_BEFORE = (
    "extraction (vector sheets) | manual_redraw (scanned sheets redrawn in QGIS)"
)
SOURCE_COMMENT_AFTER = (
    SOURCE_COMMENT_BEFORE + " | gis_file (a GIS drawing in its own CRS: the geometry job)"
)
METHOD_COMMENT_BEFORE = "helmert | affine"
METHOD_COMMENT_AFTER = "helmert | affine | native (a GIS file in its own CRS, no fit)"


def _checks(sources: str, methods: str) -> None:
    op.drop_constraint("ck_georef_datasets_source", "georef_datasets", type_="check")
    op.create_check_constraint(
        "ck_georef_datasets_source", "georef_datasets", f"source IN ({sources})"
    )
    op.drop_constraint("ck_georef_datasets_method", "georef_datasets", type_="check")
    op.create_check_constraint(
        "ck_georef_datasets_method", "georef_datasets", f"method IN ({methods})"
    )


def upgrade() -> None:
    op.add_column(
        "planning_documents",
        sa.Column(
            "short_code",
            sa.Text(),
            nullable=True,
            comment="short reference staff use for the plan (pilot short_code), e.g. DUP-NG12",
        ),
    )
    _checks(SOURCES_AFTER, METHODS_AFTER)
    op.alter_column(
        "georef_datasets",
        "source",
        existing_type=sa.Text(),
        existing_nullable=False,
        existing_server_default=sa.text("'extraction'"),
        comment=SOURCE_COMMENT_AFTER,
        existing_comment=SOURCE_COMMENT_BEFORE,
    )
    op.alter_column(
        "georef_datasets",
        "method",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=METHOD_COMMENT_AFTER,
        existing_comment=METHOD_COMMENT_BEFORE,
    )
    op.alter_column(
        "georef_datasets",
        "rmse_m",
        existing_type=sa.Float(precision=53),
        nullable=True,
        comment="null for a native GIS file (no fit)",
    )
    op.drop_constraint("ck_pipeline_jobs_type", "pipeline_jobs", type_="check")
    op.create_check_constraint(
        "ck_pipeline_jobs_type", "pipeline_jobs", f"type IN ({JOB_TYPES_AFTER})"
    )
    op.alter_column(
        "pipeline_jobs",
        "type",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=TYPE_COMMENT_AFTER,
        existing_comment=TYPE_COMMENT_BEFORE,
    )


def downgrade() -> None:
    op.execute("DELETE FROM pipeline_jobs WHERE type = 'import_zones'")
    op.drop_constraint("ck_pipeline_jobs_type", "pipeline_jobs", type_="check")
    op.create_check_constraint(
        "ck_pipeline_jobs_type", "pipeline_jobs", f"type IN ({JOB_TYPES_BEFORE})"
    )
    op.alter_column(
        "pipeline_jobs",
        "type",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=TYPE_COMMENT_BEFORE,
        existing_comment=TYPE_COMMENT_AFTER,
    )
    op.execute(
        "DELETE FROM georef_datasets WHERE source = 'gis_file' OR method = 'native' "
        "OR rmse_m IS NULL"
    )
    op.alter_column(
        "georef_datasets",
        "rmse_m",
        existing_type=sa.Float(precision=53),
        nullable=False,
        comment=None,
        existing_comment="null for a native GIS file (no fit)",
    )
    op.alter_column(
        "georef_datasets",
        "method",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=METHOD_COMMENT_BEFORE,
        existing_comment=METHOD_COMMENT_AFTER,
    )
    op.alter_column(
        "georef_datasets",
        "source",
        existing_type=sa.Text(),
        existing_nullable=False,
        existing_server_default=sa.text("'extraction'"),
        comment=SOURCE_COMMENT_BEFORE,
        existing_comment=SOURCE_COMMENT_AFTER,
    )
    _checks(SOURCES_BEFORE, METHODS_BEFORE)
    op.drop_column("planning_documents", "short_code")
