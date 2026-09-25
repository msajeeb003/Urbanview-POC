"""Analytics events (migration 0005): the prototype's validation instrument.

One row per client event accepted by ``POST /v1/events``. Everything identifying is anonymous
and client-generated (``session_id``, optional persistent ``client_id``); ``properties`` is a
small flat object that the ingest schema keeps free of names, emails and IPs. ``zone_id`` and
``parcel_id`` are copied out of ``properties`` so the dashboard can group without JSON scans; they
carry no foreign keys because zones and parcels may be re-seeded under historical events.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

EVENT_NAMES: tuple[str, ...] = (
    "map_loaded",
    "search_performed",
    "parcel_selected",
    "layer_toggled",
    "panel_viewed",
    "financials_viewed",
    "source_reference_opened",
    "order_started",
    "checkout_completed",
    "return_visit",
    "sessions_per_user",
    "market_data_interest",
    "ai_interest",
)


class AnalyticsEventRecord(Base):
    __tablename__ = "analytics_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(
        Text, nullable=False, comment="one of the 13 product event names"
    )
    session_id: Mapped[str] = mapped_column(
        Text, nullable=False, comment="anonymous, client-generated session id"
    )
    client_id: Mapped[str | None] = mapped_column(
        Text, comment="anonymous, client-generated persistent id (repeat usage); never personal"
    )
    event_id: Mapped[str | None] = mapped_column(
        Text, comment="client-generated id that de-duplicates retried batches"
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="client clock, stored in UTC"
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="small flat object; never names, emails or IPs",
    )
    zone_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="properties.zone_id copied for grouping (no FK: zones may be re-seeded)"
    )
    parcel_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="properties.parcel_id (cadastral Parcel ID) copied for grouping"
    )

    __table_args__ = (
        CheckConstraint(
            "name IN (" + ", ".join(f"'{name}'" for name in EVENT_NAMES) + ")",
            name="ck_analytics_events_name",
        ),
        Index("ix_analytics_events_name_time", "municipality_id", "name", "occurred_at"),
        Index("ix_analytics_events_session", "municipality_id", "session_id"),
        Index(
            "ix_analytics_events_zone",
            "municipality_id",
            "zone_id",
            postgresql_where=text("zone_id IS NOT NULL"),
        ),
        Index(
            "uq_analytics_events_event_id",
            "municipality_id",
            "event_id",
            unique=True,
            postgresql_where=text("event_id IS NOT NULL"),
        ),
    )
