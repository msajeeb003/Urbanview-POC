"""Information-panel schema (docs/specs/panel-payload.md section 2).

Serving vs staging (CLAUDE.md "AI and review"):
- ``planning_parameter_values`` is the SERVING table: approved, published values only, every row
  cites a page of its document and the publish version that put it on the map;
- ``planning_parameter_extractions`` is the STAGING table (review queue): AI/manual extractions
  start ``pending_review`` and reach the map only when the publish job copies approved rows into
  the serving table. The public API never reads it;
- ``publish_versions`` gives the panel its ``data_version``; ``financial_assumptions`` holds the
  admin-published market inputs per zone; ``planning_fields`` is the product-wide field
  dictionary (seeded by migration 0003, never by the seed loader).

Every table except ``planning_fields`` carries ``municipality_id``, ``dataset_version`` and
``created_at`` like the location tables (``core.models.planning``). Comments mirror the migration
because ``alembic check`` compares them.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
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


class ReviewState(StrEnum):
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"
    amended = "amended"


class PublishVersion(Base):
    """One publish act: the serving rows carrying its id are the published set; the current row
    names the panel's ``data_version``."""

    __tablename__ = "publish_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    label: Mapped[str] = mapped_column(Text, nullable=False, comment="e.g. 2026-09-22.1")
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_by: Mapped[str | None] = mapped_column(Text)
    formula_version: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'poc-1'")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    dataset_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("municipality_id", "label", name="uq_publish_versions_label"),
        Index(
            "uq_publish_versions_current",
            "municipality_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )


class PlanningField(Base):
    """Field dictionary (product-wide): the 11 Group 1 fields plus the two computed ones."""

    __tablename__ = "planning_fields"

    key: Mapped[str] = mapped_column(Text, primary_key=True, comment="snake_case")
    field_group: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    label_en: Mapped[str] = mapped_column(Text, nullable=False)
    label_me: Mapped[str] = mapped_column(
        Text, nullable=False, comment="provisional (client to confirm)"
    )
    abbreviation: Mapped[str | None] = mapped_column(Text, comment="IZ / II / BGP")
    unit: Mapped[str | None] = mapped_column(Text, comment="% / m / m²")
    value_type: Mapped[str] = mapped_column(Text, nullable=False, comment="text | number")
    computed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        comment="computed by the engine, never stored",
    )
    formula: Mapped[str | None] = mapped_column(Text, comment="human-readable, for computed fields")


class FinancialAssumption(Base):
    """Current admin market inputs per zone (``zone_id`` null = municipality-wide default)."""

    __tablename__ = "financial_assumptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    zone_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("zones.id", ondelete="CASCADE"),
        comment="null = municipality-wide default",
    )
    land_rate_eur_m2: Mapped[float] = mapped_column(
        Float(53), nullable=False, comment="land value per m² of parcel area"
    )
    build_rate_eur_m2: Mapped[float] = mapped_column(
        Float(53), nullable=False, comment="construction cost per m² GFA"
    )
    design_rate_eur_m2: Mapped[float] = mapped_column(
        Float(53), nullable=False, comment="design & documentation per m² GFA"
    )
    sale_rate_eur_m2: Mapped[float] = mapped_column(
        Float(53), nullable=False, comment="selling price per m² saleable area"
    )
    range_low_factor: Mapped[float] = mapped_column(
        Float(53), nullable=False, server_default=text("0.86")
    )
    range_high_factor: Mapped[float] = mapped_column(
        Float(53), nullable=False, server_default=text("1.15")
    )
    source: Mapped[str | None] = mapped_column(Text, comment="e.g. Realitica, Estitor, Monstat")
    source_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_by: Mapped[str | None] = mapped_column(Text)
    dataset_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "range_low_factor <= 1 AND range_high_factor >= 1 AND land_rate_eur_m2 > 0 AND "
            "build_rate_eur_m2 > 0 AND design_rate_eur_m2 > 0 AND sale_rate_eur_m2 > 0",
            name="ck_financial_assumptions_values",
        ),
        Index(
            "uq_financial_assumptions_current_zone",
            "municipality_id",
            "zone_id",
            unique=True,
            postgresql_where=text("is_current AND zone_id IS NOT NULL"),
        ),
        Index(
            "uq_financial_assumptions_current_default",
            "municipality_id",
            unique=True,
            postgresql_where=text("is_current AND zone_id IS NULL"),
        ),
    )


class PlanningParameterValue(Base):
    """SERVING: approved, published planning values. ``urban_parcel_id`` null = document-level
    value (applies to parcels under the document without a parcel-level value, and to
    cadastral-basis panels)."""

    __tablename__ = "planning_parameter_values"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("planning_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="the document the value is cited from",
    )
    # No separate index: the partial unique index uq_planning_parameter_values_parcel serves
    # parcel lookups.
    urban_parcel_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("urban_parcels.id", ondelete="CASCADE"),
        index=False,
        comment="null = document-level value",
    )
    field_key: Mapped[str] = mapped_column(Text, ForeignKey("planning_fields.key"), nullable=False)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_number: Mapped[float | None] = mapped_column(Float(53))
    unit: Mapped[str | None] = mapped_column(Text, comment="override of the dictionary unit")
    source_page: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="1-based page in the source PDF"
    )
    source_bbox: Mapped[Any | None] = mapped_column(
        JSONB, comment="[x0, y0, x1, y1] in PDF points, origin bottom-left"
    )
    source_note: Mapped[str | None] = mapped_column(Text, comment="e.g. table 3 – UP 12")
    publish_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("publish_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    dataset_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(value_text, value_number) = 1",
            name="ck_planning_parameter_values_one_value",
        ),
        # A parcel-level row can only cite the parcel's own document.
        ForeignKeyConstraint(
            ["urban_parcel_id", "document_id"],
            ["urban_parcels.id", "urban_parcels.document_id"],
            name="fk_planning_parameter_values_parcel_document",
            ondelete="CASCADE",
        ),
        Index(
            "uq_planning_parameter_values_parcel",
            "urban_parcel_id",
            "field_key",
            unique=True,
            postgresql_where=text("urban_parcel_id IS NOT NULL"),
        ),
        Index(
            "uq_planning_parameter_values_document",
            "document_id",
            "field_key",
            unique=True,
            postgresql_where=text("urban_parcel_id IS NULL"),
        ),
    )


class PlanningParameterExtraction(Base):
    """STAGING: the review queue. Never read by the public API; the publish job copies approved
    rows into ``planning_parameter_values``."""

    __tablename__ = "planning_parameter_extractions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("planning_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    urban_parcel_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("urban_parcels.id", ondelete="CASCADE")
    )
    field_key: Mapped[str] = mapped_column(Text, ForeignKey("planning_fields.key"), nullable=False)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_number: Mapped[float | None] = mapped_column(Float(53))
    unit: Mapped[str | None] = mapped_column(Text)
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_bbox: Mapped[Any | None] = mapped_column(JSONB)
    source_note: Mapped[str | None] = mapped_column(Text)
    extracted_by: Mapped[str] = mapped_column(Text, nullable=False, comment="llm:<model> or manual")
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    review_state: Mapped[ReviewState] = mapped_column(
        Enum(
            ReviewState,
            name="review_state",
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        server_default=text("'pending_review'"),
    )
    reviewer: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    published_value_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("planning_parameter_values.id", ondelete="SET NULL")
    )
    dataset_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_planning_parameter_extractions_review", "municipality_id", "review_state"),
    )
