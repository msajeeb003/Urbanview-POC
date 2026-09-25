"""Expert-analysis orders and the e-mail log (migration 0009).

``orders`` is the one table holding personal data (the purchaser's form). The ordered location,
price, turnaround and the panel snapshot are copied in at order time so the expert works from
what the visitor saw. No foreign keys to parcels: the order outlives re-seeds and republishes.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

ORDER_STATUSES: tuple[str, ...] = (
    "pending_payment",
    "paid",
    "in_progress",
    "delivered",
    "refunded",
)


def _ts(**kwargs: Any) -> Mapped[Any]:
    return mapped_column(DateTime(timezone=True), **kwargs)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    reference: Mapped[str] = mapped_column(Text, nullable=False, comment="human-readable, unique")
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'pending_payment'"),
        comment="pending_payment | paid | in_progress | delivered | refunded",
    )
    purchaser_type: Mapped[str] = mapped_column(
        Text, nullable=False, comment="individual | legal_entity"
    )
    first_name: Mapped[str] = mapped_column(Text, nullable=False)
    last_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False, comment="lower-case")
    telephone: Mapped[str] = mapped_column(Text, nullable=False)
    company_name: Mapped[str | None] = mapped_column(Text)
    tax_number: Mapped[str | None] = mapped_column(Text, comment="PIB / VAT number")
    contact_person: Mapped[str | None] = mapped_column(Text)
    registered_address: Mapped[str | None] = mapped_column(Text, comment="invoice address")
    message: Mapped[str | None] = mapped_column(Text)
    parcel_type: Mapped[str] = mapped_column(Text, nullable=False, comment="cadastral | urban")
    parcel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="id of that type")
    cadastral_parcel_id: Mapped[int | None] = mapped_column(BigInteger)
    urban_parcel_id: Mapped[int | None] = mapped_column(BigInteger)
    parcel_label: Mapped[str] = mapped_column(
        Text, nullable=False, comment="as shown to the visitor"
    )
    document_name: Mapped[str | None] = mapped_column(Text)
    zone_id: Mapped[int | None] = mapped_column(BigInteger)
    zone_name: Mapped[str | None] = mapped_column(Text)
    basis_area_m2: Mapped[float | None] = mapped_column(Float(53))
    calculation_basis: Mapped[str | None] = mapped_column(Text)
    price_eur: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'EUR'"))
    pricing_tier: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="the configured tier that priced the order"
    )
    turnaround_business_days: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_by: Mapped[date] = mapped_column(Date, nullable=False)
    assumption_edits: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="the visitor's edited assumptions",
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="the panel payload the visitor saw at order time"
    )
    data_version: Mapped[str | None] = mapped_column(Text)
    market_version_id: Mapped[int | None] = mapped_column(BigInteger)
    market_version: Mapped[int | None] = mapped_column(Integer)
    formula_version: Mapped[str | None] = mapped_column(Text)
    assignee_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("staff_users.id", ondelete="SET NULL")
    )
    paid_at: Mapped[datetime | None] = _ts(nullable=True)
    payment_amount_eur: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    payment_reference: Mapped[str | None] = mapped_column(Text)
    payment_received_on: Mapped[date | None] = mapped_column(Date)
    delivered_at: Mapped[datetime | None] = _ts(nullable=True)
    report_file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("stored_files.id", ondelete="SET NULL")
    )
    refunded_at: Mapped[datetime | None] = _ts(nullable=True)
    status_changed_at: Mapped[datetime] = _ts(nullable=False, server_default=func.now())
    placed_at: Mapped[datetime] = _ts(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = _ts(nullable=False, server_default=func.now())
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_payment', 'paid', 'in_progress', 'delivered', 'refunded')",
            name="ck_orders_status",
        ),
        CheckConstraint(
            "purchaser_type IN ('individual', 'legal_entity')", name="ck_orders_purchaser_type"
        ),
        CheckConstraint("parcel_type IN ('cadastral', 'urban')", name="ck_orders_parcel_type"),
        CheckConstraint("price_eur >= 0", name="ck_orders_price"),
        Index("uq_orders_reference", "reference", unique=True),
        Index("ix_orders_status", "municipality_id", "status", "placed_at"),
        Index("ix_orders_assignee", "municipality_id", "assignee_user_id"),
        Index("ix_orders_email", "municipality_id", "email", "placed_at"),
    )


class EmailLogEntry(Base):
    """One transactional e-mail: queued by the API, sent (or not) by the ``send_email`` job.
    Bodies are never stored; the subject, the recipient, the order / user, the provider's
    message id and the outcome are."""

    __tablename__ = "email_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="SET NULL")
    )
    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("staff_users.id", ondelete="SET NULL"),
        comment="staff recipient (magic links)",
    )
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_jobs.id", ondelete="SET NULL")
    )
    to_email: Mapped[str] = mapped_column(Text, nullable=False)
    template: Mapped[str] = mapped_column(
        Text, nullable=False, comment="payment_instructions | order_delivered | magic_link"
    )
    subject: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, comment="queued | sent | suppressed | failed | bounced"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    provider_message_id: Mapped[str | None] = mapped_column(
        Text, comment="the id the SMTP provider reported, else our Message-ID"
    )
    provider_response: Mapped[str | None] = mapped_column(Text, comment="the DATA reply")
    error: Mapped[str | None] = mapped_column(Text)
    suppressed_reason: Mapped[str | None] = mapped_column(Text)
    bounce_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(nullable=False, server_default=func.now())
    sent_at: Mapped[datetime | None] = _ts(nullable=True)
    bounced_at: Mapped[datetime | None] = _ts(nullable=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'sent', 'suppressed', 'failed', 'bounced')",
            name="ck_email_log_status",
        ),
        Index("ix_email_log_time", "municipality_id", "created_at"),
        Index("ix_email_log_order", "order_id"),
        Index("ix_email_log_status", "municipality_id", "status"),
        Index("ix_email_log_user", "user_id"),
    )
