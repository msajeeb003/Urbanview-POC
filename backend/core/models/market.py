"""Market-data imports (migration 0019, ``core.market``).

``market_imports`` keeps every import as it arrived (source, retrieval date, file checksum, the
table as read); ``market_data`` holds the normalised zone-level inputs per metric (low / expected
/ high in EUR per m²) waiting for review. Only an approved or amended row writes a
``financial_assumptions`` version (``applied_assumption_id``); the panel never reads this table.
Comments and CHECKs mirror the migration because ``alembic check`` compares comments.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from core.models.panel import ReviewState

IMPORT_KINDS: tuple[str, ...] = ("statistics", "client_ranges", "listings")
IMPORT_STATUSES: tuple[str, ...] = ("received", "normalised", "failed")
MARKET_METRICS: tuple[str, ...] = ("land_rate", "build_rate", "design_rate", "sale_rate")
RANGE_BASES: tuple[str, ...] = ("stated", "derived", "listings", "unavailable")


class MarketImport(Base):
    __tablename__ = "market_imports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(
        Text, nullable=False, comment="statistics | client_ranges | listings"
    )
    source: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="who published the figures: Monstat, the client, a listings portal",
    )
    retrieved_on: Mapped[date] = mapped_column(
        Date, nullable=False, comment="when the figures were retrieved or received"
    )
    file_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("stored_files.id", ondelete="SET NULL"),
        comment="the uploaded table; null for pasted listings",
    )
    filename: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(
        Text, nullable=False, comment="checksum of the file, or of the pasted listings"
    )
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="the table as read, cells untouched"
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'received'"),
        comment="received | normalised | failed",
    )
    normaliser: Mapped[str | None] = mapped_column(
        Text, comment="rules, or llm:<model> with the prompt version"
    )
    report: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="normalisation report: rows mapped, rows not mapped and why, issues"
    )
    error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_jobs.id", ondelete="SET NULL")
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    normalised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "kind IN ('statistics', 'client_ranges', 'listings')", name="ck_market_imports_kind"
        ),
        CheckConstraint(
            "status IN ('received', 'normalised', 'failed')", name="ck_market_imports_status"
        ),
        Index("uq_market_imports_checksum", "municipality_id", "kind", "sha256", unique=True),
        Index("ix_market_imports_time", "municipality_id", "created_at"),
    )


class MarketDataItem(Base):
    """One normalised market input for one zone and metric, as imported and as reviewed."""

    __tablename__ = "market_data"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    import_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("market_imports.id", ondelete="CASCADE"), nullable=False
    )
    zone_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("zones.id", ondelete="CASCADE"), nullable=False
    )
    metric: Mapped[str] = mapped_column(
        Text, nullable=False, comment="land_rate | build_rate | design_rate | sale_rate"
    )
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'EUR'"))
    unit: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'EUR/m²'"))
    low: Mapped[float | None] = mapped_column(Float(53), comment="null when no range could be read")
    expected: Mapped[float] = mapped_column(Float(53), nullable=False)
    high: Mapped[float | None] = mapped_column(
        Float(53), comment="null when no range could be read"
    )
    range_basis: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="stated | derived (configured range factors) | listings | unavailable",
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_date: Mapped[date | None] = mapped_column(
        Date, comment="the date or period end the figure refers to"
    )
    effective_from: Mapped[date | None] = mapped_column(
        Date, comment="set on approval: applies from this date"
    )
    confidence: Mapped[float | None] = mapped_column(Float(53))
    notes: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[list[Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="what the reviewer should look at (municipality_level, range_derived ...)",
    )
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="the source row and cells the figure was read from"
    )
    mapping: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        comment="how the source geography became the zone (method, printed name, confidence)",
    )
    normaliser: Mapped[str] = mapped_column(Text, nullable=False, comment="rules or llm:<model>")
    review_status: Mapped[ReviewState] = mapped_column(
        Enum(
            ReviewState,
            name="review_state",
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        server_default=text("'pending_review'"),
    )
    amended_low: Mapped[float | None] = mapped_column(Float(53))
    amended_expected: Mapped[float | None] = mapped_column(Float(53))
    amended_high: Mapped[float | None] = mapped_column(Float(53))
    reviewer: Mapped[str | None] = mapped_column(Text)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    applied_assumption_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("financial_assumptions.id", ondelete="SET NULL"),
        comment="the assumptions version this item wrote; the item is then closed",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "metric IN ('land_rate', 'build_rate', 'design_rate', 'sale_rate')",
            name="ck_market_data_metric",
        ),
        CheckConstraint(
            "range_basis IN ('stated', 'derived', 'listings', 'unavailable')",
            name="ck_market_data_range_basis",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_market_data_confidence",
        ),
        CheckConstraint(
            "review_status <> 'amended' OR amended_expected IS NOT NULL",
            name="ck_market_data_amended",
        ),
        Index("ix_market_data_queue", "municipality_id", "review_status", "zone_id"),
        Index("ix_market_data_import", "import_id"),
    )
