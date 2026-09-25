"""expert-analysis orders (guest checkout by bank transfer), e-mail log, expert_report files

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-24

``orders`` holds the purchaser's form (the only personal data the platform stores), the ordered
location, the server-side price and turnaround, and a snapshot of the panel the visitor saw
(``snapshot``, ``data_version``, ``market_version``), so the expert works from exactly what was
shown even after a later publish. Status flow ``pending_payment → paid → in_progress →
delivered`` plus ``refunded`` from paid / in_progress, every change audited. ``email_log`` records
every transactional e-mail attempt. ``stored_files.kind`` gains ``expert_report``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORDER_STATUSES = ("pending_payment", "paid", "in_progress", "delivered", "refunded")


def _ts(name: str, **kwargs):
    return sa.Column(name, sa.DateTime(timezone=True), **kwargs)


def upgrade() -> None:
    op.drop_constraint("ck_stored_files_kind", "stored_files", type_="check")
    op.create_check_constraint(
        "ck_stored_files_kind",
        "stored_files",
        "kind IN ('planning_document', 'gis', 'cadastral_extract', 'expert_report')",
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False, comment="human-readable, unique"),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending_payment'"),
            comment="pending_payment | paid | in_progress | delivered | refunded",
        ),
        sa.Column("purchaser_type", sa.Text(), nullable=False, comment="individual | legal_entity"),
        sa.Column("first_name", sa.Text(), nullable=False),
        sa.Column("last_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False, comment="lower-case"),
        sa.Column("telephone", sa.Text(), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=True),
        sa.Column("tax_number", sa.Text(), nullable=True, comment="PIB / VAT number"),
        sa.Column("contact_person", sa.Text(), nullable=True),
        sa.Column("registered_address", sa.Text(), nullable=True, comment="invoice address"),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("parcel_type", sa.Text(), nullable=False, comment="cadastral | urban"),
        sa.Column("parcel_id", sa.BigInteger(), nullable=False, comment="id of that type"),
        sa.Column("cadastral_parcel_id", sa.BigInteger(), nullable=True),
        sa.Column("urban_parcel_id", sa.BigInteger(), nullable=True),
        sa.Column("parcel_label", sa.Text(), nullable=False, comment="as shown to the visitor"),
        sa.Column("document_name", sa.Text(), nullable=True),
        sa.Column("zone_id", sa.BigInteger(), nullable=True),
        sa.Column("zone_name", sa.Text(), nullable=True),
        sa.Column("basis_area_m2", sa.Float(precision=53), nullable=True),
        sa.Column("calculation_basis", sa.Text(), nullable=True),
        sa.Column("price_eur", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default=sa.text("'EUR'")),
        sa.Column(
            "pricing_tier",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="the configured tier that priced the order",
        ),
        sa.Column("turnaround_business_days", sa.Integer(), nullable=False),
        sa.Column("expected_by", sa.Date(), nullable=False),
        sa.Column(
            "assumption_edits",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="the visitor's edited assumptions",
        ),
        sa.Column(
            "snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="the panel payload the visitor saw at order time",
        ),
        sa.Column("data_version", sa.Text(), nullable=True),
        sa.Column("market_version_id", sa.BigInteger(), nullable=True),
        sa.Column("market_version", sa.Integer(), nullable=True),
        sa.Column("formula_version", sa.Text(), nullable=True),
        sa.Column(
            "assignee_user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _ts("paid_at", nullable=True),
        sa.Column("payment_amount_eur", sa.Numeric(10, 2), nullable=True),
        sa.Column("payment_reference", sa.Text(), nullable=True),
        sa.Column("payment_received_on", sa.Date(), nullable=True),
        _ts("delivered_at", nullable=True),
        sa.Column(
            "report_file_id",
            sa.BigInteger(),
            sa.ForeignKey("stored_files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _ts("refunded_at", nullable=True),
        _ts("status_changed_at", nullable=False, server_default=sa.func.now()),
        _ts("placed_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending_payment', 'paid', 'in_progress', 'delivered', 'refunded')",
            name="ck_orders_status",
        ),
        sa.CheckConstraint(
            "purchaser_type IN ('individual', 'legal_entity')", name="ck_orders_purchaser_type"
        ),
        sa.CheckConstraint("parcel_type IN ('cadastral', 'urban')", name="ck_orders_parcel_type"),
        sa.CheckConstraint("price_eur >= 0", name="ck_orders_price"),
    )
    op.create_index("uq_orders_reference", "orders", ["reference"], unique=True)
    op.create_index("ix_orders_status", "orders", ["municipality_id", "status", "placed_at"])
    op.create_index("ix_orders_assignee", "orders", ["municipality_id", "assignee_user_id"])
    op.create_index("ix_orders_email", "orders", ["municipality_id", "email", "placed_at"])

    op.create_table(
        "email_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "order_id",
            sa.BigInteger(),
            sa.ForeignKey("orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("to_email", sa.Text(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False, comment="payment_instructions | report_delivered | …"),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, comment="sent | suppressed | failed"),
        sa.Column("error", sa.Text(), nullable=True),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('sent', 'suppressed', 'failed')", name="ck_email_log_status"
        ),
    )
    op.create_index("ix_email_log_time", "email_log", ["municipality_id", "created_at"])
    op.create_index("ix_email_log_order", "email_log", ["order_id"])


def downgrade() -> None:
    op.drop_table("email_log")
    op.drop_table("orders")
    op.drop_constraint("ck_stored_files_kind", "stored_files", type_="check")
    op.create_check_constraint(
        "ck_stored_files_kind",
        "stored_files",
        "kind IN ('planning_document', 'gis', 'cadastral_extract')",
    )
