"""Location-resolution schema: zones, planning documents, urban blocks, planned urban parcels and
cadastral parcels.

Rules encoded here (CLAUDE.md):
- cadastral parcels (``cadastral_parcels``) and planned urban parcels (``urban_parcels``) are
  separate tables, never merged; ``cadastral_parcels.id`` is UrbanView's numeric Parcel ID;
- every table carries ``municipality_id``; geometry is EPSG:4326 MultiPolygon with a GiST index;
- ``planning_documents.status`` is UrbanView's vocabulary (adopted / in_progress / superseded) and
  only ``adopted`` documents define coverage;
- document ``type`` (DUP/PUP/PGR …) and KO names are municipality data validated against the
  municipality profile, so they are text, not database enums.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class PlanningDocumentStatus(StrEnum):
    adopted = "adopted"
    in_progress = "in_progress"
    superseded = "superseded"


def multipolygon() -> Geometry:
    # spatial_index=False: GeoAlchemy2 would create the GiST index only through DDL events, which
    # Alembic autogenerate cannot see. Each model declares its index explicitly (gist_index) with
    # GeoAlchemy2's naming, so `alembic check` compares models and database one to one.
    return Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False)


def gist_index(table: str, column: str) -> Index:
    return Index(f"idx_{table}_{column}", column, postgresql_using="gist")


class Zone(Base):
    """UrbanView's internal city division (~city quarter) grouping several planning documents."""

    __tablename__ = "zones"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    geom: Mapped[Any] = mapped_column(multipolygon(), nullable=False)
    general_planning_summary: Mapped[str | None] = mapped_column(Text)
    zone_type: Mapped[str | None] = mapped_column(
        Text,
        comment=(
            "Planning character for the map colour: res | com | mix | pub | grn; "
            "null = not classified"
        ),
    )
    # The zone dataset (migration 0021, core.zones): a stable slug drawn in QGIS with the client
    # and imported through staging; null on rows that predate it (the sample), matched by name.
    zone_key: Mapped[str | None] = mapped_column(
        Text, comment="stable slug from the zone dataset (data/zones); null on legacy rows"
    )
    notes: Mapped[str | None] = mapped_column(Text)
    no_adopted_plan: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        comment="the zone knowingly has no adopted planning document",
    )
    dataset_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        gist_index("zones", "geom"),
        CheckConstraint(
            "zone_type IS NULL OR zone_type IN ('res', 'com', 'mix', 'pub', 'grn')",
            name="ck_zones_zone_type",
        ),
        Index(
            "uq_zones_zone_key",
            "municipality_id",
            "zone_key",
            unique=True,
            postgresql_where=text("zone_key IS NOT NULL"),
        ),
    )


class PlanningDocument(Base):
    __tablename__ = "planning_documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False, comment="DUP / PUP / PGR (profile)")
    status: Mapped[PlanningDocumentStatus] = mapped_column(
        Enum(
            PlanningDocumentStatus,
            name="planning_document_status",
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    source: Mapped[str | None] = mapped_column(Text, comment="e.g. eRegistri")
    source_url: Mapped[str | None] = mapped_column(Text)
    # Nullable since migration 0006: a document is registered before its geometry job has run.
    coverage_geom: Mapped[Any | None] = mapped_column(multipolygon(), nullable=True)
    zone_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("zones.id", ondelete="SET NULL"), index=True
    )
    # Amendments are linked by this column only (set at ingestion), never by coverage
    # intersection. Seed rows are ordered so an amended document precedes its amendment.
    amends_document_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("planning_documents.id", ondelete="SET NULL"),
        index=True,
        comment="explicit amendment link set at ingestion (never by coverage)",
    )
    # Source viewer (migration 0004): set by the ingestion job. Existence of a page is decided
    # from these columns, never by probing the bucket.
    file_key: Mapped[str | None] = mapped_column(
        Text, comment="object key of the stored PDF in the private bucket; null = not stored"
    )
    page_count: Mapped[int | None] = mapped_column(
        Integer, comment="pages in the stored PDF (set at ingestion)"
    )
    page_images_rendered: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        comment="page images at <municipality>/planning-documents/<id>/pages/NNNN.png",
    )
    # Admin pipeline (migration 0006): registration, versions, the coverage switch.
    file_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("stored_files.id", ondelete="SET NULL"),
        comment="the uploaded PDF this version was registered against",
    )
    lineage_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("planning_documents.id", ondelete="SET NULL"),
        comment="first version of this document; null on legacy rows = itself",
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    is_current_version: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    licence_note: Mapped[str | None] = mapped_column(
        Text, comment="licence / permission to use the document"
    )
    registered_by: Mapped[str | None] = mapped_column(Text)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    coverage_live: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        comment="the coverage area takes part in location resolution (admin switch)",
    )
    coverage_live_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    coverage_live_changed_by: Mapped[str | None] = mapped_column(Text)
    dataset_version: Mapped[str | None] = mapped_column(Text)
    adopted_on: Mapped[date | None] = mapped_column(
        Date, comment="Adoption date (official gazette), when known"
    )
    eregistri_reference: Mapped[str | None] = mapped_column(
        Text, comment="eRegistri document id (lamp.gov.me/PlanningDocument/Details/<id>)"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_planning_documents_municipality_status", "municipality_id", "status"),
        gist_index("planning_documents", "coverage_geom"),
        CheckConstraint(
            "page_count IS NULL OR page_count > 0", name="ck_planning_documents_page_count"
        ),
        Index("ix_planning_documents_file_id", "file_id"),
        Index("ix_planning_documents_lineage_id", "lineage_id"),
        Index(
            "ix_planning_documents_eregistri_reference", "municipality_id", "eregistri_reference"
        ),
        Index(
            "uq_planning_documents_current_version",
            "lineage_id",
            unique=True,
            postgresql_where=text("is_current_version AND lineage_id IS NOT NULL"),
        ),
    )


class UrbanBlock(Base):
    __tablename__ = "urban_blocks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    block_ref: Mapped[str] = mapped_column(Text, nullable=False)
    geom: Mapped[Any] = mapped_column(multipolygon(), nullable=False)
    zone_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("zones.id", ondelete="SET NULL"), index=True
    )
    dataset_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (gist_index("urban_blocks", "geom"),)


class UrbanParcel(Base):
    """Planned urban parcel from an adopted plan. The calculation basis when it exists."""

    __tablename__ = "urban_parcels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    urban_parcel_number: Mapped[str] = mapped_column(Text, nullable=False)
    geom: Mapped[Any] = mapped_column(multipolygon(), nullable=False)
    area_m2: Mapped[float] = mapped_column(Float(53), nullable=False)
    block_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("urban_blocks.id", ondelete="SET NULL"), index=True
    )
    document_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("planning_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dataset_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "document_id", "urban_parcel_number", name="uq_urban_parcels_document_number"
        ),
        # (id, document_id) as a key so planning_parameter_values' composite FK can pin a
        # parcel-level value to the parcel's own document.
        UniqueConstraint("id", "document_id", name="uq_urban_parcels_id_document"),
        gist_index("urban_parcels", "geom"),
    )


class CadastralParcel(Base):
    """Cadastral parcel as recorded by the cadastre today. ``id`` is UrbanView's Parcel ID."""

    __tablename__ = "cadastral_parcels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    parcel_number: Mapped[str] = mapped_column(Text, nullable=False)
    sub_number: Mapped[str | None] = mapped_column(Text)
    ko_name: Mapped[str] = mapped_column(
        Text, nullable=False, comment="cadastral municipality (KO)"
    )
    street_address: Mapped[str | None] = mapped_column(Text)
    geom: Mapped[Any] = mapped_column(multipolygon(), nullable=False)
    area_m2: Mapped[float] = mapped_column(Float(53), nullable=False)
    public_ownership: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    restitution_or_legal_burden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    dataset_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        gist_index("cadastral_parcels", "geom"),
        # The same parcel number recurs across cadastral municipalities, so (KO, number,
        # sub-number) is the identity of a cadastral parcel within a municipality. Case-insensitive
        # on the KO name; NULL sub-numbers compare as ''. Expressions are written the way
        # PostgreSQL reflects them so autogenerate sees no difference.
        Index(
            "uq_cadastral_parcels_ko_number",
            "municipality_id",
            text("lower(ko_name)"),
            "parcel_number",
            text("COALESCE(sub_number, ''::text)"),
            unique=True,
        ),
    )
