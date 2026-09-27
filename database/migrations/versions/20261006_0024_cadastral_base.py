"""The cadastral base: versioned imports, KO table, retired parcels, unknown ownership flags

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-06

The cadastral base loader (``core.cadastre``) imports an export of the cadastre (UZN geoportal or
eMapa, only where bulk access is confirmed) with ogr2ogr. One import is a ``cadastral_datasets``
row with its provenance, validation report and diff against the previous version; the parcels and
the KO boundaries are staged as ``staging_geometry`` batches and reach the serving tables through
the publish job, which upserts parcels by (KO, number, sub-number) and retires the parcels of the
imported KOs that the new export no longer contains (``retired_at``): nothing is deleted, so
earlier links and orders keep resolving, and nothing retired is served.

``cadastral_municipalities`` is the served KO table (name, code, boundary, parcel count) behind the
KO + number lookup. It is backfilled here from the parcels already loaded (boundaries derived from
them).

``public_ownership`` / ``restitution_or_legal_burden`` become nullable without a default: null means
the flags were not loaded (they come only from a confirmed bulk eKatastar extract and are never
derived). Existing values are kept.
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FLAG_COMMENT = "null = not loaded (set only from a confirmed bulk eKatastar extract, never derived)"
LAYER_COMMENT = (
    "cadastral_parcels | cadastral_municipalities | urban_parcels | urban_blocks | zones "
    "| document_coverage | land_use | traffic_network"
)
OLD_LAYER_COMMENT = (
    "cadastral_parcels | urban_parcels | urban_blocks | zones | document_coverage "
    "| land_use | traffic_network"
)
FLAGS = ("public_ownership", "restitution_or_legal_burden")


def _multipolygon() -> geoalchemy2.Geometry:
    return geoalchemy2.Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False)


def upgrade() -> None:
    op.add_column(
        "cadastral_parcels",
        sa.Column(
            "ko_code",
            sa.Text(),
            comment="the KO's code as the cadastre writes it; null when the source has none",
        ),
    )
    op.add_column(
        "cadastral_parcels",
        sa.Column(
            "retired_at",
            sa.DateTime(timezone=True),
            comment="a newer import of the KO no longer contains the parcel: kept, not served",
        ),
    )
    op.add_column(
        "cadastral_parcels",
        sa.Column(
            "retired_dataset_version",
            sa.Text(),
            comment="the cadastral dataset whose publish retired the parcel",
        ),
    )
    for column in FLAGS:
        op.alter_column(
            "cadastral_parcels",
            column,
            existing_type=sa.Boolean(),
            nullable=True,
            server_default=None,
            existing_server_default=sa.text("false"),
            comment=FLAG_COMMENT,
        )
    op.alter_column(
        "geometry_batches",
        "layer_id",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=LAYER_COMMENT,
        existing_comment=OLD_LAYER_COMMENT,
    )

    op.create_table(
        "cadastral_datasets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("dataset_version", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'staged'"),
            comment="staged | invalid | published | superseded",
        ),
        sa.Column(
            "source_id",
            sa.Text(),
            nullable=False,
            comment="adapter: uzn_geoportal | emapa (core.cadastre.adapters)",
        ),
        sa.Column("source_name", sa.Text()),
        sa.Column("source_url", sa.Text()),
        sa.Column("method", sa.Text(), nullable=False, comment="file | wfs"),
        sa.Column(
            "retrieved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="when the export was obtained",
        ),
        sa.Column(
            "access_basis",
            sa.Text(),
            comment="the agreement or written permission that confirms bulk access",
        ),
        sa.Column("licence_note", sa.Text()),
        sa.Column("file_name", sa.Text()),
        sa.Column("file_sha256", sa.Text()),
        sa.Column("file_size", sa.BigInteger()),
        sa.Column(
            "file_key", sa.Text(), comment="copy of the export in the private bucket, when stored"
        ),
        sa.Column(
            "source_crs",
            sa.Text(),
            comment="the export's coordinate reference system (kept as metadata)",
        ),
        sa.Column(
            "transform",
            sa.Text(),
            comment="coordinate operation used to reach EPSG:4326 (null = PROJ default)",
        ),
        sa.Column(
            "parcels_batch_id",
            sa.BigInteger(),
            sa.ForeignKey("geometry_batches.id", ondelete="SET NULL"),
            comment="the staging_geometry batch (layer cadastral_parcels)",
        ),
        sa.Column(
            "ko_batch_id",
            sa.BigInteger(),
            sa.ForeignKey("geometry_batches.id", ondelete="SET NULL"),
            comment="the staging_geometry batch (layer cadastral_municipalities)",
        ),
        sa.Column(
            "previous_dataset_id",
            sa.BigInteger(),
            sa.ForeignKey("cadastral_datasets.id", ondelete="SET NULL"),
            comment="the version the diff compares with",
        ),
        sa.Column("parcel_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("ko_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "ownership",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="whether the ownership / legal-burden flags were loaded, and from what",
        ),
        sa.Column(
            "validation",
            postgresql.JSONB(),
            comment="the validation report (a dataset with errors is never staged)",
        ),
        sa.Column(
            "diff",
            postgresql.JSONB(),
            comment="added / removed / changed parcels against the previous version",
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
            "municipality_id", "dataset_version", name="uq_cadastral_datasets_version"
        ),
        sa.CheckConstraint(
            "status IN ('staged', 'invalid', 'published', 'superseded')",
            name="ck_cadastral_datasets_status",
        ),
        sa.CheckConstraint("method IN ('file', 'wfs')", name="ck_cadastral_datasets_method"),
    )
    op.create_index(
        "ix_cadastral_datasets_municipality_status",
        "cadastral_datasets",
        ["municipality_id", "status"],
    )

    op.create_table(
        "cadastral_municipalities",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("ko_name", sa.Text(), nullable=False),
        sa.Column("ko_code", sa.Text()),
        sa.Column("geom", _multipolygon(), nullable=False),
        sa.Column("parcel_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "boundary_source",
            sa.Text(),
            nullable=False,
            comment="delivered | derived_from_parcels",
        ),
        sa.Column("dataset_version", sa.Text()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "boundary_source IN ('delivered', 'derived_from_parcels')",
            name="ck_cadastral_municipalities_boundary_source",
        ),
    )
    op.create_index(
        "idx_cadastral_municipalities_geom",
        "cadastral_municipalities",
        ["geom"],
        postgresql_using="gist",
    )
    op.create_index(
        "uq_cadastral_municipalities_name",
        "cadastral_municipalities",
        ["municipality_id", sa.text("lower(ko_name)")],
        unique=True,
    )
    # the KOs of the parcels already loaded, their boundaries derived from the parcels
    op.execute(
        """
        INSERT INTO cadastral_municipalities (municipality_id, ko_name, ko_code, geom,
                                              parcel_count, boundary_source, dataset_version)
        SELECT c.municipality_id, min(c.ko_name), min(c.ko_code),
               ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Union(c.geom)), 3)), count(*),
               'derived_from_parcels', max(c.dataset_version)
        FROM cadastral_parcels c
        GROUP BY c.municipality_id, lower(c.ko_name)
        """
    )


def downgrade() -> None:
    op.drop_index("uq_cadastral_municipalities_name", table_name="cadastral_municipalities")
    op.drop_index(
        "idx_cadastral_municipalities_geom",
        table_name="cadastral_municipalities",
        postgresql_using="gist",
    )
    op.drop_table("cadastral_municipalities")
    op.drop_index("ix_cadastral_datasets_municipality_status", table_name="cadastral_datasets")
    op.drop_table("cadastral_datasets")
    op.alter_column(
        "geometry_batches",
        "layer_id",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=OLD_LAYER_COMMENT,
        existing_comment=LAYER_COMMENT,
    )
    for column in FLAGS:
        op.execute(f"UPDATE cadastral_parcels SET {column} = false WHERE {column} IS NULL")
        op.alter_column(
            "cadastral_parcels",
            column,
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=None,
            existing_comment=FLAG_COMMENT,
        )
    op.drop_column("cadastral_parcels", "retired_dataset_version")
    op.drop_column("cadastral_parcels", "retired_at")
    op.drop_column("cadastral_parcels", "ko_code")
