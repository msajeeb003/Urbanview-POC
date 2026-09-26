"""The zone dataset in staging (migration 0021, ``core.zones``).

A zone dataset is one import of the zones drawn in QGIS with the client plus their planning-document
list (``data/zones/<municipality>/``): the zone polygons go to ``staging_geometry`` as one
``zones`` batch (reprojected to EPSG:4326), the documents to ``staging_zone_documents``, and a
``zone_datasets`` row ties them to a ``dataset_version`` with the validation report, the source
files' checksums and the parcel counts. Nothing is served from here: the publish job upserts the
zones by ``zone_key`` and applies the documents (matching planning documents already registered
through the admin API by eRegistri id, source URL or name instead of duplicating them). A newer
import supersedes an older staged dataset; published datasets stay as history.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
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

DATASET_STATUSES = ("staged", "published", "superseded", "rejected")
MATCH_METHODS = ("eregistri", "source_url", "name", "new")


class ZoneDataset(Base):
    __tablename__ = "zone_datasets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    dataset_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'staged'"),
        comment="staged | published | superseded | rejected",
    )
    zones_batch_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("geometry_batches.id", ondelete="SET NULL"),
        comment="the staging_geometry batch (layer zones) holding the polygons",
    )
    editing_crs: Mapped[int | None] = mapped_column(
        Integer, comment="EPSG code the zones were drawn in (reprojected to 4326 on import)"
    )
    zone_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    document_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sources: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="input files with their sha256",
    )
    validation: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="the validation report (errors are refused before staging)"
    )
    report: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="zones with document and parcel counts at import"
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
        UniqueConstraint("municipality_id", "dataset_version", name="uq_zone_datasets_version"),
        CheckConstraint(
            "status IN ('staged', 'published', 'superseded', 'rejected')",
            name="ck_zone_datasets_status",
        ),
        Index("ix_zone_datasets_municipality_status", "municipality_id", "status"),
    )


class StagingZoneDocument(Base):
    __tablename__ = "staging_zone_documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    dataset_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("zone_datasets.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="1-based row of the document list"
    )
    zone_key: Mapped[str] = mapped_column(Text, nullable=False)
    document_name: Mapped[str] = mapped_column(Text, nullable=False)
    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    eregistri_reference: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    adoption_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    poc_coverage: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    review: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="review aids carried from the list (listing, registry facts, match)",
    )
    matched_document_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("planning_documents.id", ondelete="SET NULL"),
        comment="the registered planning document this row updates (null = a new document)",
    )
    match_method: Mapped[str | None] = mapped_column(
        Text, comment="eregistri | source_url | name | new"
    )
    applied_document_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("planning_documents.id", ondelete="SET NULL"),
        comment="set by the publish job: the planning document the row became",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("dataset_id", "row_number", name="uq_staging_zone_documents_row"),
        CheckConstraint(
            "status IN ('adopted', 'in_progress', 'superseded')",
            name="ck_staging_zone_documents_status",
        ),
        Index("ix_staging_zone_documents_dataset", "dataset_id"),
    )
