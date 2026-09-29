"""Georeferenced plan geometry (migration 0025, ``core.gis.georef``).

One ``georef_datasets`` row per georeferencing run of a planning document: the plan's projected
CRS, the fitted transform (Helmert or affine, with every control point's residual and the per-sheet
page -> CRS parameters), the RMSE overall and per sheet (the residual report the admin console
shows on the document), the snapping to the cadastral base, the validation, and the staged batches
it produced. The batches go through the publish job like any staged geometry; nothing is served
from here. Rows are never deleted: a newer run of the document supersedes the staged one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

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


class GeorefDataset(Base):
    __tablename__ = "georef_datasets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("planning_documents.id"), nullable=False
    )
    dataset_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'staged'"),
        comment="staged | invalid | published | superseded",
    )
    source: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'extraction'"),
        comment=(
            "extraction (vector sheets) | manual_redraw (scanned sheets redrawn in QGIS)"
            " | gis_file (a GIS drawing in its own CRS: the geometry job)"
        ),
    )
    crs: Mapped[str] = mapped_column(Text, nullable=False, comment="the plan's projected CRS")
    method: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="helmert | affine | native (a GIS file in its own CRS, no fit)",
    )
    transform: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment="parameters, per-sheet page -> CRS parameters, residuals, control-point hash",
    )
    rmse_m: Mapped[float | None] = mapped_column(
        Float(53), comment="null for a native GIS file (no fit)"
    )
    max_residual_m: Mapped[float | None] = mapped_column(Float(53))
    points_used: Mapped[int] = mapped_column(Integer, nullable=False)
    sheets: Mapped[list[Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="per sheet: sheet, page, points, rmse_m",
    )
    snap: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="snapping to the cadastral base: tolerance, vertices moved, near misses"
    )
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    batches: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="staged layer -> geometry_batches.id",
    )
    output_sha256: Mapped[str | None] = mapped_column(
        Text, comment="digest of the georeferenced features: a re-run must reproduce it"
    )
    gpkg_key: Mapped[str | None] = mapped_column(
        Text, comment="the georeferenced GeoPackage in the private bucket, when stored"
    )
    imported_by: Mapped[str | None] = mapped_column(Text)
    published_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("publish_versions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("municipality_id", "dataset_version", name="uq_georef_datasets_version"),
        CheckConstraint(
            "status IN ('staged', 'invalid', 'published', 'superseded')",
            name="ck_georef_datasets_status",
        ),
        CheckConstraint(
            "source IN ('extraction', 'manual_redraw', 'gis_file')",
            name="ck_georef_datasets_source",
        ),
        CheckConstraint(
            "method IN ('helmert', 'affine', 'native')", name="ck_georef_datasets_method"
        ),
        Index("ix_georef_datasets_document", "document_id", "id"),
    )
