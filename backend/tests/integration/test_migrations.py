"""Schema checks against the migrated PostGIS database."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import text

from tests.integration.conftest import alembic_config

pytestmark = pytest.mark.integration


def test_models_match_migrations(postgis_url):
    """`alembic check`: autogenerate must find no difference between models and the database."""
    command.check(alembic_config(postgis_url))


async def test_geometry_columns_are_4326_multipolygons_with_gist_indexes(pg_conn):
    rows = (
        await pg_conn.execute(
            text(
                "SELECT f_table_name, f_geometry_column, srid, type FROM geometry_columns "
                "WHERE f_table_schema = 'public'"
            )
        )
    ).all()
    assert {tuple(r) for r in rows} == {
        ("cadastral_parcels", "geom", 4326, "MULTIPOLYGON"),
        ("heatmap_cells", "geom", 4326, "MULTIPOLYGON"),
        # generic map layers hold polygons (land use) and lines (traffic network)
        ("layer_features", "geom", 4326, "GEOMETRY"),
        ("planning_documents", "coverage_geom", 4326, "MULTIPOLYGON"),
        ("staging_geometry", "geom", 4326, "GEOMETRY"),
        ("urban_blocks", "geom", 4326, "MULTIPOLYGON"),
        ("urban_parcels", "geom", 4326, "MULTIPOLYGON"),
        ("zones", "geom", 4326, "MULTIPOLYGON"),
    }
    gist = (
        await pg_conn.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND indexdef ILIKE '%USING gist%'"
            )
        )
    ).scalars()
    assert {
        "idx_cadastral_parcels_geom",
        "idx_planning_documents_coverage_geom",
        "idx_urban_blocks_geom",
        "idx_urban_parcels_geom",
        "idx_zones_geom",
    } <= set(gist)


async def test_document_status_enum(pg_conn):
    labels = (
        await pg_conn.execute(
            text(
                "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'planning_document_status' ORDER BY enumsortorder"
            )
        )
    ).scalars()
    assert list(labels) == ["adopted", "in_progress", "superseded"]
