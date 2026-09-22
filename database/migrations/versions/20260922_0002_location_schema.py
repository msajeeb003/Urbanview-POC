"""location resolution schema: zones, planning documents, urban blocks, urban parcels,
cadastral parcels

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22

Geometry is stored as geometry(MultiPolygon, 4326) with GiST indexes named idx_<table>_<column>
(GeoAlchemy2's convention, so the models' spatial_index=True matches). Cadastral parcels and
planned urban parcels are separate tables: never merged.
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS_ENUM_NAME = "planning_document_status"
STATUS_VALUES = ("adopted", "in_progress", "superseded")


def multipolygon() -> geoalchemy2.Geometry:
    # spatial_index=False: the GiST indexes are created explicitly below.
    return geoalchemy2.Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False)


def created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    status_enum = postgresql.ENUM(*STATUS_VALUES, name=STATUS_ENUM_NAME, create_type=False)
    status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "zones",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("geom", multipolygon(), nullable=False),
        sa.Column("general_planning_summary", sa.Text()),
        sa.Column("dataset_version", sa.Text()),
        created_at(),
    )
    op.create_index("ix_zones_municipality_id", "zones", ["municipality_id"])
    op.create_index("idx_zones_geom", "zones", ["geom"], postgresql_using="gist")

    op.create_table(
        "planning_documents",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False, comment="DUP / PUP / PGR (profile)"),
        sa.Column("status", status_enum, nullable=False),
        sa.Column("source", sa.Text(), comment="e.g. eRegistri"),
        sa.Column("source_url", sa.Text()),
        sa.Column("coverage_geom", multipolygon(), nullable=False),
        sa.Column(
            "zone_id", sa.BigInteger(), sa.ForeignKey("zones.id", ondelete="SET NULL")
        ),
        sa.Column("dataset_version", sa.Text()),
        created_at(),
    )
    op.create_index(
        "ix_planning_documents_municipality_id", "planning_documents", ["municipality_id"]
    )
    op.create_index("ix_planning_documents_zone_id", "planning_documents", ["zone_id"])
    op.create_index(
        "ix_planning_documents_municipality_status",
        "planning_documents",
        ["municipality_id", "status"],
    )
    op.create_index(
        "idx_planning_documents_coverage_geom",
        "planning_documents",
        ["coverage_geom"],
        postgresql_using="gist",
    )

    op.create_table(
        "urban_blocks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("block_ref", sa.Text(), nullable=False),
        sa.Column("geom", multipolygon(), nullable=False),
        sa.Column(
            "zone_id", sa.BigInteger(), sa.ForeignKey("zones.id", ondelete="SET NULL")
        ),
        sa.Column("dataset_version", sa.Text()),
        created_at(),
    )
    op.create_index("ix_urban_blocks_municipality_id", "urban_blocks", ["municipality_id"])
    op.create_index("ix_urban_blocks_zone_id", "urban_blocks", ["zone_id"])
    op.create_index("idx_urban_blocks_geom", "urban_blocks", ["geom"], postgresql_using="gist")

    op.create_table(
        "urban_parcels",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("urban_parcel_number", sa.Text(), nullable=False),
        sa.Column("geom", multipolygon(), nullable=False),
        sa.Column("area_m2", sa.Float(precision=53), nullable=False),
        sa.Column(
            "block_id", sa.BigInteger(), sa.ForeignKey("urban_blocks.id", ondelete="SET NULL")
        ),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dataset_version", sa.Text()),
        created_at(),
        sa.UniqueConstraint(
            "document_id", "urban_parcel_number", name="uq_urban_parcels_document_number"
        ),
    )
    op.create_index("ix_urban_parcels_municipality_id", "urban_parcels", ["municipality_id"])
    op.create_index("ix_urban_parcels_block_id", "urban_parcels", ["block_id"])
    op.create_index("ix_urban_parcels_document_id", "urban_parcels", ["document_id"])
    op.create_index("idx_urban_parcels_geom", "urban_parcels", ["geom"], postgresql_using="gist")

    op.create_table(
        "cadastral_parcels",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("parcel_number", sa.Text(), nullable=False),
        sa.Column("sub_number", sa.Text()),
        sa.Column("ko_name", sa.Text(), nullable=False, comment="cadastral municipality (KO)"),
        sa.Column("street_address", sa.Text()),
        sa.Column("geom", multipolygon(), nullable=False),
        sa.Column("area_m2", sa.Float(precision=53), nullable=False),
        sa.Column(
            "public_ownership", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "restitution_or_legal_burden",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("dataset_version", sa.Text()),
        created_at(),
    )
    op.create_index(
        "ix_cadastral_parcels_municipality_id", "cadastral_parcels", ["municipality_id"]
    )
    op.create_index(
        "idx_cadastral_parcels_geom", "cadastral_parcels", ["geom"], postgresql_using="gist"
    )
    # Identity of a cadastral parcel: (KO, number, sub-number) within a municipality. The same
    # number recurs across KOs; case-insensitive KO; NULL sub-numbers compare as ''.
    op.create_index(
        "uq_cadastral_parcels_ko_number",
        "cadastral_parcels",
        [
            "municipality_id",
            sa.text("lower(ko_name)"),
            "parcel_number",
            sa.text("coalesce(sub_number, '')"),
        ],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("cadastral_parcels")
    op.drop_table("urban_parcels")
    op.drop_table("urban_blocks")
    op.drop_table("planning_documents")
    op.drop_table("zones")
    postgresql.ENUM(name=STATUS_ENUM_NAME).drop(op.get_bind(), checkfirst=True)
