"""Publish pipeline tables (migration 0011).

- ``geometry_batches`` / ``staging_geometry``: what the GIS ingestion job produces, one batch per
  (file, layer). Features carry a natural ``feature_key`` and JSON ``properties`` whose keys are
  the layer's contract (``jobs.publish_layers``); the publish job upserts entity layers into
  their serving tables by natural key (stable ids) and copies the generic layers into
  ``layer_features`` for the new version.
  Since 0033 a staged batch carries its ``origin``, topology QA (``qa_status`` /
  ``qa_issues``, ``core.geometry_qa``) and a reviewer's decision (``review_state``): the publish
  job applies approved batches only (the pilot scope's ``staging.geometry_draft``).
- ``layer_features``: versioned generic map layers (planned land use, traffic network).
- ``parcel_links``: cadastral ↔ planned parcel overlaps per version (rank 1 = primary), the
  same thresholds as location resolution.
- the heatmap surfaces are ``choropleth_cells`` / ``choropleth_classes``
  (``core.models.choropleth``, migration 0027, which replaced 0011's ``heatmap_cells``).

Every row carries ``publish_version_id`` except the staging tables; the public API and the tile
export read the version flagged ``is_current``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from core.models.planning import gist_index


def any_geometry() -> Geometry:
    """Mixed geometry types (polygons for land use, lines for the traffic network)."""
    return Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False)


class GeometryBatch(Base):
    __tablename__ = "geometry_batches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("stored_files.id", ondelete="SET NULL")
    )
    layer_id: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="cadastral_parcels | cadastral_municipalities | urban_parcels | urban_blocks "
        "| zones | document_coverage | land_use | traffic_network",
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'staged'"),
        comment="staged | published | superseded | rejected",
    )
    feature_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    produced_by: Mapped[str | None] = mapped_column(Text, comment="job or principal")
    qa_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    published_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("publish_versions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # geometry review (0033): where it came from, what the QA found, what the reviewer decided
    origin: Mapped[str | None] = mapped_column(
        Text,
        comment="vector_pdf (drawing layers read from a vector plan PDF) | manual_qgis (drawn or "
        "redrawn in QGIS) | official_gis (an official GIS file: a supplied plan drawing, the "
        "cadastre)",
    )
    document_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("planning_documents.id", ondelete="SET NULL"),
        comment="the planning document a georeferenced or drawn batch belongs to",
    )
    dataset_version: Mapped[str | None] = mapped_column(
        Text, comment="label of the producing dataset (georeferencing, zone or cadastral import)"
    )
    qa_status: Mapped[str | None] = mapped_column(
        Text, comment="topology QA (core.geometry_qa): pass | warn | fail; null = not checked yet"
    )
    qa_issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="overlaps, gaps, area deviation, invalid geometry, the dataset's warnings",
    )
    review_state: Mapped[str | None] = mapped_column(
        Text,
        server_default=text("'pending_review'"),
        comment="pending_review | approved | rejected; null = published before geometry review "
        "(0033)",
    )
    reviewer: Mapped[str | None] = mapped_column(Text)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('staged', 'published', 'superseded', 'rejected')",
            name="ck_geometry_batches_status",
        ),
        CheckConstraint(
            "origin IN ('vector_pdf', 'manual_qgis', 'official_gis')",
            name="ck_geometry_batches_origin",
        ),
        CheckConstraint(
            "qa_status IN ('pass', 'warn', 'fail')", name="ck_geometry_batches_qa_status"
        ),
        CheckConstraint(
            "review_state IN ('pending_review', 'approved', 'rejected')",
            name="ck_geometry_batches_review_state",
        ),
        Index("ix_geometry_batches_layer_status", "municipality_id", "layer_id", "status"),
        Index(
            "ix_geometry_batches_review",
            "municipality_id",
            "review_state",
            postgresql_where=text("status = 'staged'"),
        ),
    )


class StagingGeometry(Base):
    __tablename__ = "staging_geometry"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    batch_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("geometry_batches.id", ondelete="CASCADE"), nullable=False
    )
    layer_id: Mapped[str] = mapped_column(Text, nullable=False)
    feature_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="natural key within the layer (e.g. 'Podgorica I|1042|' for a cadastral parcel)",
    )
    geom: Mapped[Any] = mapped_column(any_geometry(), nullable=False)
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("batch_id", "feature_key", name="uq_staging_geometry_batch_key"),
        gist_index("staging_geometry", "geom"),
    )


class LayerFeature(Base):
    __tablename__ = "layer_features"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    publish_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("publish_versions.id", ondelete="CASCADE"), nullable=False
    )
    layer_id: Mapped[str] = mapped_column(
        Text, nullable=False, comment="land_use | traffic_network"
    )
    feature_key: Mapped[str] = mapped_column(Text, nullable=False)
    geom: Mapped[Any] = mapped_column(any_geometry(), nullable=False)
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        UniqueConstraint(
            "publish_version_id", "layer_id", "feature_key", name="uq_layer_features_version_key"
        ),
        gist_index("layer_features", "geom"),
    )


LINK_RELATIONS = ("same", "reduced", "enlarged", "split", "merged", "none")


class ParcelLink(Base):
    """Cadastral <-> planned urban parcel correspondence of a publish version (core.parcel_links,
    BRD §2.2): one row per linked pair, one ``none`` row (no urban parcel) per cadastral parcel no
    planned parcel covers; every row of a cadastral parcel carries its relation."""

    __tablename__ = "parcel_links"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    publish_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("publish_versions.id", ondelete="CASCADE"), nullable=False
    )
    dataset_version: Mapped[str | None] = mapped_column(
        Text, comment="label of the publish version the links were computed for"
    )
    cadastral_parcel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cadastral_parcels.id", ondelete="CASCADE"), nullable=False
    )
    urban_parcel_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("urban_parcels.id", ondelete="CASCADE"),
        comment="null = relation none: no planned parcel over the cadastral parcel",
    )
    cadastral_area_m2: Mapped[float] = mapped_column(
        Float(53), nullable=False, comment="the cadastre's area of the cadastral parcel"
    )
    urban_area_m2: Mapped[float | None] = mapped_column(
        Float(53), comment="the plan's area of the planned parcel"
    )
    overlap_area_m2: Mapped[float] = mapped_column(
        Float(53), nullable=False, comment="intersection area in the municipality's metric CRS"
    )
    overlap_ratio_of_cadastral: Mapped[float] = mapped_column(
        Float(53), nullable=False, comment="overlap / cadastral parcel area (metric CRS)"
    )
    overlap_ratio_of_urban: Mapped[float | None] = mapped_column(
        Float(53), comment="overlap / planned parcel area (metric CRS)"
    )
    area_delta_m2: Mapped[float | None] = mapped_column(
        Float(53), comment="planned area - cadastral area"
    )
    relation: Mapped[str] = mapped_column(
        Text, nullable=False, comment="same | reduced | enlarged | split | merged | none"
    )
    reduction_pct: Mapped[float | None] = mapped_column(
        Float(53),
        comment="share of the cadastral parcel in no planned parcel (roads, public space), %",
    )
    rank: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="1 = primary link (or the none row)"
    )

    __table_args__ = (
        UniqueConstraint(
            "publish_version_id",
            "cadastral_parcel_id",
            "urban_parcel_id",
            name="uq_parcel_links_version_pair",
        ),
        Index("ix_parcel_links_urban", "publish_version_id", "urban_parcel_id"),
        Index(
            "uq_parcel_links_version_none",
            "publish_version_id",
            "cadastral_parcel_id",
            unique=True,
            postgresql_where=text("urban_parcel_id IS NULL"),
        ),
        CheckConstraint(
            "relation IN ('same', 'reduced', 'enlarged', 'split', 'merged', 'none')",
            name="ck_parcel_links_relation",
        ),
        CheckConstraint(
            "(urban_parcel_id IS NULL) = (relation = 'none')", name="ck_parcel_links_none"
        ),
    )
