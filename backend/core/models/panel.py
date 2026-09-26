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

    # The publish run behind the version (migration 0011).
    archive_key: Mapped[str | None] = mapped_column(
        Text, comment="PMTiles object key (private bucket)"
    )
    archive_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    archive_sha256: Mapped[str | None] = mapped_column(Text)
    layers: Mapped[list[Any] | None] = mapped_column(
        JSONB, comment="source layers in the archive with feature counts and zoom ranges"
    )
    counts: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="what the publish run copied / computed"
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    min_zoom: Mapped[int | None] = mapped_column(Integer)
    max_zoom: Mapped[int | None] = mapped_column(Integer)
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_jobs.id", ondelete="SET NULL")
    )
    previous_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("publish_versions.id", ondelete="SET NULL"),
        comment="the version that was current when this one was published",
    )
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_by: Mapped[str | None] = mapped_column(Text)
    archive_pruned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="retention removed the archive object; the version cannot be restored",
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


ASSUMPTION_RATES: tuple[str, ...] = ("land", "build", "design", "sale")
# Mirrors migration 0007 (Alembic does not compare CHECK constraints).
ASSUMPTION_BOUNDS_CHECK = " AND ".join(
    f"(({r}_rate_low_eur_m2 IS NULL AND {r}_rate_high_eur_m2 IS NULL) OR "
    f"({r}_rate_low_eur_m2 IS NOT NULL AND {r}_rate_high_eur_m2 IS NOT NULL AND "
    f"{r}_rate_low_eur_m2 > 0 AND {r}_rate_low_eur_m2 <= {r}_rate_eur_m2 AND "
    f"{r}_rate_eur_m2 <= {r}_rate_high_eur_m2))"
    for r in ASSUMPTION_RATES
)
ZONE_PARAMETER_VALUES_CHECK = (
    "(max_far IS NULL OR max_far >= 0) AND "
    "(max_site_coverage_pct IS NULL OR "
    "(max_site_coverage_pct >= 0 AND max_site_coverage_pct <= 100)) AND "
    "(max_height_m IS NULL OR max_height_m >= 0) AND (max_floors IS NULL OR max_floors >= 0) AND "
    "(source_page IS NULL OR source_page >= 1)"
)


class ZoneParameterSet(Base):
    """Typical planning values of a zone (admin-maintained, versioned): the zone panel's
    ``typical_parameters``. A parcel's own document values always take precedence."""

    __tablename__ = "zone_parameter_sets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    zone_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("zones.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    supersedes_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("zone_parameter_sets.id", ondelete="SET NULL")
    )
    land_use: Mapped[str | None] = mapped_column(Text)
    max_far: Mapped[float | None] = mapped_column(Float(53), comment="II, typical")
    max_site_coverage_pct: Mapped[float | None] = mapped_column(Float(53), comment="IZ %, typical")
    max_height_m: Mapped[float | None] = mapped_column(Float(53))
    max_floors: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    source_document_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("planning_documents.id", ondelete="SET NULL")
    )
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_note: Mapped[str | None] = mapped_column(Text)
    verified_on: Mapped[date | None] = mapped_column(Date, comment="expert verification date")
    verified_by: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by: Mapped[str | None] = mapped_column(Text)
    dataset_version: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(ZONE_PARAMETER_VALUES_CHECK, name="ck_zone_parameter_sets_values"),
        Index(
            "uq_zone_parameter_sets_current",
            "zone_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("ix_zone_parameter_sets_zone", "municipality_id", "zone_id", "version"),
    )


class FinancialAssumption(Base):
    """Market inputs per zone, versioned (the current row is what the panel reads). The
    municipality-wide row (``zone_id`` null) holds the range factors single-figure market
    imports are widened with; it never supplies a zone's figures (migration 0019)."""

    __tablename__ = "financial_assumptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    zone_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("zones.id", ondelete="CASCADE"),
        comment=(
            "null = municipality-wide row: range factors for market imports, never a zone's figures"
        ),
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
    # Version history (migration 0007): the current row is what the panel reads.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    supersedes_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("financial_assumptions.id", ondelete="SET NULL"),
        comment="the previous version of this zone's assumptions",
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by: Mapped[str | None] = mapped_column(Text)
    # Optional absolute bounds per rate (checked low <= rate <= high); null = use the factors.
    land_rate_low_eur_m2: Mapped[float | None] = mapped_column(
        Float(53), comment="absolute low bound of the land rate; null = use the factor"
    )
    land_rate_high_eur_m2: Mapped[float | None] = mapped_column(
        Float(53), comment="absolute high bound of the land rate; null = use the factor"
    )
    build_rate_low_eur_m2: Mapped[float | None] = mapped_column(
        Float(53), comment="absolute low bound of the build rate; null = use the factor"
    )
    build_rate_high_eur_m2: Mapped[float | None] = mapped_column(
        Float(53), comment="absolute high bound of the build rate; null = use the factor"
    )
    design_rate_low_eur_m2: Mapped[float | None] = mapped_column(
        Float(53), comment="absolute low bound of the design rate; null = use the factor"
    )
    design_rate_high_eur_m2: Mapped[float | None] = mapped_column(
        Float(53), comment="absolute high bound of the design rate; null = use the factor"
    )
    sale_rate_low_eur_m2: Mapped[float | None] = mapped_column(
        Float(53), comment="absolute low bound of the sale rate; null = use the factor"
    )
    sale_rate_high_eur_m2: Mapped[float | None] = mapped_column(
        Float(53), comment="absolute high bound of the sale rate; null = use the factor"
    )
    source: Mapped[str | None] = mapped_column(Text, comment="e.g. Realitica, Estitor, Monstat")
    source_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    # Market-data imports (migration 0019): when the version applies from, and per rate where
    # it came from ({rate: {source, source_date, market_data_id?, range_basis?}}).
    effective_from: Mapped[date | None] = mapped_column(
        Date, comment="the date the version applies from, when stated"
    )
    rate_sources: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="per rate: source, source date, the market input that set it"
    )
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
        CheckConstraint(ASSUMPTION_BOUNDS_CHECK, name="ck_financial_assumptions_bounds"),
        Index("ix_financial_assumptions_history", "municipality_id", "zone_id", "version"),
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
    block_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("urban_blocks.id", ondelete="CASCADE"), comment="block-level value"
    )
    zone_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("zones.id", ondelete="CASCADE"), comment="zone-level value"
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
        CheckConstraint(
            "num_nonnulls(urban_parcel_id, block_id, zone_id) <= 1",
            name="ck_planning_parameter_values_one_scope",
        ),
        # A parcel-level row can only cite the parcel's own document.
        ForeignKeyConstraint(
            ["urban_parcel_id", "document_id"],
            ["urban_parcels.id", "urban_parcels.document_id"],
            name="fk_planning_parameter_values_parcel_document",
            ondelete="CASCADE",
        ),
        # One value per scope and field within a publish version (migration 0011): the serving
        # set of every version is complete, so a rollback is a pointer flip.
        Index(
            "uq_planning_parameter_values_parcel",
            "publish_version_id",
            "urban_parcel_id",
            "field_key",
            unique=True,
            postgresql_where=text("urban_parcel_id IS NOT NULL"),
        ),
        Index(
            "uq_planning_parameter_values_block",
            "publish_version_id",
            "block_id",
            "field_key",
            unique=True,
            postgresql_where=text("block_id IS NOT NULL"),
        ),
        Index(
            "uq_planning_parameter_values_zone",
            "publish_version_id",
            "zone_id",
            "field_key",
            unique=True,
            postgresql_where=text("zone_id IS NOT NULL"),
        ),
        Index(
            "uq_planning_parameter_values_document",
            "publish_version_id",
            "document_id",
            "field_key",
            unique=True,
            postgresql_where=text(
                "urban_parcel_id IS NULL AND block_id IS NULL AND zone_id IS NULL"
            ),
        ),
    )


class PlanningValueGap(Base):
    """SERVING (migration 0013): a planning field whose extracted value the expert rejected and
    nothing replaced, per publish version and scope (like ``planning_parameter_values``). Written by
    the publish job so the public panel can say ``rejected`` without reading the review queue."""

    __tablename__ = "planning_value_gaps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    publish_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("publish_versions.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("planning_documents.id", ondelete="CASCADE"),
        nullable=False,
        comment="the document the rejected value was extracted from",
    )
    urban_parcel_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("urban_parcels.id", ondelete="CASCADE")
    )
    block_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("urban_blocks.id", ondelete="CASCADE")
    )
    zone_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("zones.id", ondelete="CASCADE")
    )
    field_key: Mapped[str] = mapped_column(Text, ForeignKey("planning_fields.key"), nullable=False)
    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="rejected: the expert rejected the extracted value and nothing replaced it",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("reason IN ('rejected')", name="ck_planning_value_gaps_reason"),
        CheckConstraint(
            "num_nonnulls(urban_parcel_id, block_id, zone_id) <= 1",
            name="ck_planning_value_gaps_one_scope",
        ),
        Index(
            "uq_planning_value_gaps_parcel",
            "publish_version_id",
            "urban_parcel_id",
            "field_key",
            unique=True,
            postgresql_where=text("urban_parcel_id IS NOT NULL"),
        ),
        Index(
            "uq_planning_value_gaps_block",
            "publish_version_id",
            "block_id",
            "field_key",
            unique=True,
            postgresql_where=text("block_id IS NOT NULL"),
        ),
        Index(
            "uq_planning_value_gaps_zone",
            "publish_version_id",
            "zone_id",
            "field_key",
            unique=True,
            postgresql_where=text("zone_id IS NOT NULL"),
        ),
        Index(
            "uq_planning_value_gaps_document",
            "publish_version_id",
            "document_id",
            "field_key",
            unique=True,
            postgresql_where=text(
                "urban_parcel_id IS NULL AND block_id IS NULL AND zone_id IS NULL"
            ),
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
    # Review queue (migration 0008): the target beyond the urban parcel, the parameter key for
    # market data rows (no planning field), the snippet, the confidence, the reviewer's correction.
    entity_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'urban_parcel'"),
        comment="urban_parcel | zone | block | document | market_data",
    )
    zone_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("zones.id", ondelete="SET NULL")
    )
    block_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("urban_blocks.id", ondelete="SET NULL")
    )
    field_key: Mapped[str | None] = mapped_column(
        Text, ForeignKey("planning_fields.key"), nullable=True
    )
    parameter_key: Mapped[str] = mapped_column(
        Text, nullable=False, comment="planning field key, or a market rate key for market_data"
    )
    value_text: Mapped[str | None] = mapped_column(Text)
    value_number: Mapped[float | None] = mapped_column(Float(53))
    unit: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(
        Text, comment="the text the value was read from (snippet)"
    )
    confidence: Mapped[float | None] = mapped_column(Float(53), comment="extractor confidence 0..1")
    amended_value_text: Mapped[str | None] = mapped_column(
        Text, comment="reviewer's corrected value; the AI value stays"
    )
    amended_value_number: Mapped[float | None] = mapped_column(
        Float(53), comment="reviewer's corrected value; the AI value stays"
    )
    amended_unit: Mapped[str | None] = mapped_column(Text)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
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
    # The extraction contract (migration 0017, core.extraction): the leaf as produced, readable
    # after the schema or the prompts change; the flags and method the queue shows and filters.
    schema_version: Mapped[str | None] = mapped_column(
        Text, comment="extraction contract version of payload; null = manual or seeded row"
    )
    prompt_version: Mapped[str | None] = mapped_column(
        Text, comment="prompt set that produced the item"
    )
    extraction_method: Mapped[str | None] = mapped_column(Text, comment="text | table | ocr")
    flags: Mapped[list[Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="validator flags for the reviewer (low_confidence, out_of_range ...)",
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="the extraction item as produced (core.extraction.read_payload)"
    )
    job_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("pipeline_jobs.id", ondelete="SET NULL"),
        comment="the extraction job that produced the item",
    )
    # Extraction runs (migration 0020, jobs.extraction_runner): the run that wrote the item, the
    # target as printed when no geometry matches it, the previous run's item for the same target
    # and field, and superseding instead of deleting.
    run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        comment="the extraction run that wrote the item; null = manual or seeded",
    )
    target_label: Mapped[str | None] = mapped_column(
        Text,
        comment="the parcel number / block label as printed (kept when no geometry matches)",
    )
    target_key: Mapped[str | None] = mapped_column(
        Text, comment="normalised target: parcel_key, block_key or 'document'"
    )
    previous_item_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("planning_parameter_extractions.id", ondelete="SET NULL"),
        comment="the previous run's item for the same target and field",
    )
    change: Mapped[str | None] = mapped_column(
        Text, comment="new | same | changed against previous_item_id"
    )
    superseded_by_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        comment="the run (or decision) that replaced this open item; never deleted",
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_planning_parameter_extractions_review", "municipality_id", "review_state"),
        Index("ix_planning_parameter_extractions_run", "run_id"),
        Index("ix_planning_parameter_extractions_target", "document_id", "target_key", "field_key"),
        CheckConstraint(
            "change IS NULL OR change IN ('new', 'same', 'changed')",
            name="ck_planning_parameter_extractions_change",
        ),
        Index(
            "ix_planning_parameter_extractions_queue",
            "municipality_id",
            "review_state",
            "document_id",
        ),
        Index("ix_planning_parameter_extractions_page", "document_id", "source_page"),
        CheckConstraint(
            "entity_type IN ('urban_parcel', 'zone', 'block', 'document', 'market_data')",
            name="ck_planning_parameter_extractions_entity",
        ),
        CheckConstraint(
            "(entity_type = 'market_data') = (field_key IS NULL)",
            name="ck_planning_parameter_extractions_key",
        ),
        CheckConstraint(
            "review_state <> 'amended' OR "
            "num_nonnulls(amended_value_text, amended_value_number) = 1",
            name="ck_planning_parameter_extractions_amended",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_planning_parameter_extractions_confidence",
        ),
        CheckConstraint(
            "extraction_method IS NULL OR extraction_method IN ('text', 'table', 'ocr')",
            name="ck_planning_parameter_extractions_method",
        ),
        CheckConstraint(
            "payload IS NULL OR schema_version IS NOT NULL",
            name="ck_planning_parameter_extractions_payload_version",
        ),
    )
