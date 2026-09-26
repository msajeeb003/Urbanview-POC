"""zone_datasets / staging_zone_documents: the zones drawn with the client and their documents

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-03

Zones are UrbanView's own city divisions (roughly city quarters), drawn in QGIS with the client and
imported by ``python -m core.zones import`` (``data/zones/<municipality>/``). One import is a
``zone_datasets`` row with a ``dataset_version``: the polygons go to ``staging_geometry`` as a
``zones`` batch (EPSG:4326), the planning-document list to ``staging_zone_documents``. The publish
job upserts zones by the new stable ``zones.zone_key`` (legacy rows without a key are matched by
name once and take the key) and applies the documents to ``planning_documents``, matching ones
registered through the admin API by ``eregistri_reference``, source URL or name.

``zones.notes`` and ``zones.no_adopted_plan`` (a zone knowingly without an adopted document) come
with the dataset; ``planning_documents.eregistri_reference`` is the registry's document id.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "zones",
        sa.Column(
            "zone_key",
            sa.Text(),
            comment="stable slug from the zone dataset (data/zones); null on legacy rows",
        ),
    )
    op.add_column("zones", sa.Column("notes", sa.Text()))
    op.add_column(
        "zones",
        sa.Column(
            "no_adopted_plan",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="the zone knowingly has no adopted planning document",
        ),
    )
    op.create_index(
        "uq_zones_zone_key",
        "zones",
        ["municipality_id", "zone_key"],
        unique=True,
        postgresql_where=sa.text("zone_key IS NOT NULL"),
    )
    op.add_column(
        "planning_documents",
        sa.Column(
            "eregistri_reference",
            sa.Text(),
            comment="eRegistri document id (lamp.gov.me/PlanningDocument/Details/<id>)",
        ),
    )
    op.create_index(
        "ix_planning_documents_eregistri_reference",
        "planning_documents",
        ["municipality_id", "eregistri_reference"],
    )

    op.create_table(
        "zone_datasets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("dataset_version", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'staged'"),
            comment="staged | published | superseded | rejected",
        ),
        sa.Column(
            "zones_batch_id",
            sa.BigInteger(),
            sa.ForeignKey("geometry_batches.id", ondelete="SET NULL"),
            comment="the staging_geometry batch (layer zones) holding the polygons",
        ),
        sa.Column(
            "editing_crs",
            sa.Integer(),
            comment="EPSG code the zones were drawn in (reprojected to 4326 on import)",
        ),
        sa.Column("zone_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("document_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "sources",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="input files with their sha256",
        ),
        sa.Column(
            "validation",
            postgresql.JSONB(),
            comment="the validation report (errors are refused before staging)",
        ),
        sa.Column(
            "report", postgresql.JSONB(), comment="zones with document and parcel counts at import"
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
        sa.UniqueConstraint("municipality_id", "dataset_version", name="uq_zone_datasets_version"),
        sa.CheckConstraint(
            "status IN ('staged', 'published', 'superseded', 'rejected')",
            name="ck_zone_datasets_status",
        ),
    )
    op.create_index(
        "ix_zone_datasets_municipality_status", "zone_datasets", ["municipality_id", "status"]
    )

    op.create_table(
        "staging_zone_documents",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "dataset_id",
            sa.BigInteger(),
            sa.ForeignKey("zone_datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "row_number",
            sa.Integer(),
            nullable=False,
            comment="1-based row of the document list",
        ),
        sa.Column("zone_key", sa.Text(), nullable=False),
        sa.Column("document_name", sa.Text(), nullable=False),
        sa.Column("document_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("eregistri_reference", sa.Text()),
        sa.Column("source_url", sa.Text()),
        sa.Column("adoption_date", sa.Date()),
        sa.Column("notes", sa.Text()),
        sa.Column("poc_coverage", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "review",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="review aids carried from the list (listing, registry facts, match)",
        ),
        sa.Column(
            "matched_document_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_documents.id", ondelete="SET NULL"),
            comment="the registered planning document this row updates (null = a new document)",
        ),
        sa.Column("match_method", sa.Text(), comment="eregistri | source_url | name | new"),
        sa.Column(
            "applied_document_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_documents.id", ondelete="SET NULL"),
            comment="set by the publish job: the planning document the row became",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("dataset_id", "row_number", name="uq_staging_zone_documents_row"),
        sa.CheckConstraint(
            "status IN ('adopted', 'in_progress', 'superseded')",
            name="ck_staging_zone_documents_status",
        ),
    )
    op.create_index("ix_staging_zone_documents_dataset", "staging_zone_documents", ["dataset_id"])


def downgrade() -> None:
    op.drop_index("ix_staging_zone_documents_dataset", table_name="staging_zone_documents")
    op.drop_table("staging_zone_documents")
    op.drop_index("ix_zone_datasets_municipality_status", table_name="zone_datasets")
    op.drop_table("zone_datasets")
    op.drop_index("ix_planning_documents_eregistri_reference", table_name="planning_documents")
    op.drop_column("planning_documents", "eregistri_reference")
    op.drop_index("uq_zones_zone_key", table_name="zones")
    op.drop_column("zones", "no_adopted_plan")
    op.drop_column("zones", "notes")
    op.drop_column("zones", "zone_key")
