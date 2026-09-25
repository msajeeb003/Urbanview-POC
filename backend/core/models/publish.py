"""Publish pipeline tables (migration 0011).

- ``geometry_batches`` / ``staging_geometry``: what the GIS ingestion job produces, one batch per
  (file, layer). Features carry a natural ``feature_key`` and JSON ``properties`` whose keys are
  the layer's contract (``jobs.publish_layers``); the publish job upserts entity layers into
  their serving tables by natural key (stable ids) and copies the generic layers into
  ``layer_features`` for the new version.
- ``layer_features``: versioned generic map layers (planned land use, traffic network).
- ``parcel_links``: cadastral ↔ planned parcel overlaps per version (rank 1 = primary), the
  same thresholds as location resolution.
- ``heatmap_cells``: block and zone aggregates of the published parameters and the current
  market assumptions (the choropleth / heatmap layers).

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
from core.models.planning import gist_index, multipolygon


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
        comment="cadastral_parcels | urban_parcels | urban_blocks | zones | document_coverage "
        "| land_use | traffic_network",
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

    __table_args__ = (
        CheckConstraint(
            "status IN ('staged', 'published', 'superseded', 'rejected')",
            name="ck_geometry_batches_status",
        ),
        Index("ix_geometry_batches_layer_status", "municipality_id", "layer_id", "status"),
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


class ParcelLink(Base):
    __tablename__ = "parcel_links"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    publish_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("publish_versions.id", ondelete="CASCADE"), nullable=False
    )
    cadastral_parcel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cadastral_parcels.id", ondelete="CASCADE"), nullable=False
    )
    urban_parcel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("urban_parcels.id", ondelete="CASCADE"), nullable=False
    )
    overlap_m2: Mapped[float] = mapped_column(Float(53), nullable=False)
    overlap_fraction: Mapped[float] = mapped_column(
        Float(53), nullable=False, comment="overlap / cadastral area"
    )
    area_delta_m2: Mapped[float] = mapped_column(
        Float(53), nullable=False, comment="planned area - cadastral area"
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False, comment="1 = primary link")

    __table_args__ = (
        UniqueConstraint(
            "publish_version_id",
            "cadastral_parcel_id",
            "urban_parcel_id",
            name="uq_parcel_links_version_pair",
        ),
        Index("ix_parcel_links_urban", "publish_version_id", "urban_parcel_id"),
    )


class HeatmapCell(Base):
    __tablename__ = "heatmap_cells"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    publish_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("publish_versions.id", ondelete="CASCADE"), nullable=False
    )
    cell_type: Mapped[str] = mapped_column(Text, nullable=False, comment="block | zone")
    cell_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="urban_blocks.id | zones.id"
    )
    cell_ref: Mapped[str | None] = mapped_column(Text, comment="block ref | zone name")
    geom: Mapped[Any] = mapped_column(multipolygon(), nullable=False)
    parcel_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    stated_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="parcels with at least one heatmap parameter",
    )
    max_site_coverage_pct: Mapped[float | None] = mapped_column(Float(53))
    max_height_m: Mapped[float | None] = mapped_column(Float(53))
    max_far: Mapped[float | None] = mapped_column(Float(53))
    max_gfa_m2: Mapped[float | None] = mapped_column(Float(53))
    saleable_area_m2: Mapped[float | None] = mapped_column(Float(53))
    sale_rate_eur_m2: Mapped[float | None] = mapped_column(Float(53))
    sale_rate_low_eur_m2: Mapped[float | None] = mapped_column(
        Float(53),
        comment="Low sale rate €/m²: absolute bound, else expected × low factor",
    )
    sale_rate_high_eur_m2: Mapped[float | None] = mapped_column(
        Float(53),
        comment="High sale rate €/m²: absolute bound, else expected × high factor",
    )
    market_value_eur: Mapped[float | None] = mapped_column(Float(53))
    price_band: Mapped[int | None] = mapped_column(
        Integer, comment="1 (lowest) .. 3 (highest) tercile of the zone sale rates"
    )

    __table_args__ = (
        CheckConstraint("cell_type IN ('block', 'zone')", name="ck_heatmap_cells_type"),
        UniqueConstraint(
            "publish_version_id", "cell_type", "cell_id", name="uq_heatmap_cells_version_cell"
        ),
        gist_index("heatmap_cells", "geom"),
    )
