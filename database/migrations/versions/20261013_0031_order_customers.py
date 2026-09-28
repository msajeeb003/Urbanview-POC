"""orders: customers, the data version seen, payment_failed

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-13

The pilot technical scope's order model (§5): guest purchasers in their own table
(``public.customer``: e-mail, first name, phone, optional company name and company id), the order
pointing at its customer and at the dataset version the customer saw, and a status set that
includes ``payment_failed``.

- ``customers``: one row per e-mail address and municipality, created or refreshed by
  ``POST /v1/orders`` (the latest name and phone win; a company is kept until another is given).
  The order still carries the details typed on it, so an order reads the same after a later one.
  Backfilled from the existing orders the same way (the latest order of each address, its latest
  company).
- ``orders.customer_id`` (FK, SET NULL) and ``orders.publish_version_id`` (FK to
  ``publish_versions``, SET NULL: the version the panel was served from; backfilled from
  ``data_version`` = the version's label).
- ``payment_failed``: the transfer did not arrive (staff record it with "payment not received");
  the order can still be paid.
- The pilot's order form asks name, e-mail and phone (company name and PIB optional), so
  ``contact_person`` and ``registered_address`` are no longer collected: kept for older orders.

A downgrade puts ``payment_failed`` orders back to ``pending_payment`` and drops the new columns and
the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES_BEFORE = "'pending_payment', 'paid', 'in_progress', 'delivered', 'refunded'"
STATUSES_AFTER = (
    "'pending_payment', 'paid', 'payment_failed', 'in_progress', 'delivered', 'refunded'"
)
STATUS_COMMENT_BEFORE = "pending_payment | paid | in_progress | delivered | refunded"
STATUS_COMMENT_AFTER = (
    "pending_payment | paid | payment_failed | in_progress | delivered | refunded"
)
LEGACY = "no longer collected (0031); kept for older orders"

BACKFILL_CUSTOMERS = """
INSERT INTO customers (municipality_id, email, first_name, last_name, phone, company_name,
                       company_id, created_at, updated_at)
SELECT DISTINCT ON (municipality_id, email)
       municipality_id, email, first_name, last_name, telephone,
       (SELECT c.company_name FROM orders c
        WHERE c.municipality_id = o.municipality_id AND c.email = o.email
          AND c.company_name IS NOT NULL
        ORDER BY c.placed_at DESC, c.id DESC LIMIT 1),
       (SELECT c.tax_number FROM orders c
        WHERE c.municipality_id = o.municipality_id AND c.email = o.email
          AND c.tax_number IS NOT NULL
        ORDER BY c.placed_at DESC, c.id DESC LIMIT 1),
       min(placed_at) OVER (PARTITION BY municipality_id, email), placed_at
FROM orders o
ORDER BY municipality_id, email, placed_at DESC, id DESC
"""
LINK_CUSTOMERS = """
UPDATE orders o SET customer_id = c.id
FROM customers c
WHERE c.municipality_id = o.municipality_id AND c.email = o.email
"""
LINK_VERSIONS = """
UPDATE orders o SET publish_version_id = (
    SELECT v.id FROM publish_versions v
    WHERE v.municipality_id = o.municipality_id AND v.label = o.data_version
    ORDER BY v.id DESC LIMIT 1)
WHERE o.data_version IS NOT NULL
"""


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _status(check: str, comment: str, previous: str) -> None:
    op.drop_constraint("ck_orders_status", "orders", type_="check")
    op.create_check_constraint("ck_orders_status", "orders", f"status IN ({check})")
    op.alter_column(
        "orders",
        "status",
        existing_type=sa.Text(),
        existing_nullable=False,
        existing_server_default=sa.text("'pending_payment'"),
        comment=comment,
        existing_comment=previous,
    )


def _legacy_comments(contact: str | None, address: str | None, before: tuple) -> None:
    op.alter_column(
        "orders",
        "contact_person",
        existing_type=sa.Text(),
        existing_nullable=True,
        comment=contact,
        existing_comment=before[0],
    )
    op.alter_column(
        "orders",
        "registered_address",
        existing_type=sa.Text(),
        existing_nullable=True,
        comment=address,
        existing_comment=before[1],
    )


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "email",
            sa.Text(),
            nullable=False,
            comment="lower-case; one customer per address and municipality",
        ),
        sa.Column("first_name", sa.Text(), nullable=False),
        sa.Column("last_name", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=True),
        sa.Column("company_id", sa.Text(), nullable=True, comment="PIB (company id), optional"),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("uq_customers_email", "customers", ["municipality_id", "email"], unique=True)

    op.add_column(
        "orders",
        sa.Column(
            "customer_id",
            sa.BigInteger(),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
            comment="the guest purchaser",
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "publish_version_id",
            sa.BigInteger(),
            sa.ForeignKey("publish_versions.id", ondelete="SET NULL"),
            nullable=True,
            comment="the published data version the visitor saw",
        ),
    )
    op.create_index("ix_orders_customer", "orders", ["customer_id"])
    op.execute(BACKFILL_CUSTOMERS)
    op.execute(LINK_CUSTOMERS)
    op.execute(LINK_VERSIONS)

    _status(STATUSES_AFTER, STATUS_COMMENT_AFTER, STATUS_COMMENT_BEFORE)
    _legacy_comments(LEGACY, LEGACY, (None, "invoice address"))


def downgrade() -> None:
    _legacy_comments(None, "invoice address", (LEGACY, LEGACY))
    op.execute("UPDATE orders SET status = 'pending_payment' WHERE status = 'payment_failed'")
    _status(STATUSES_BEFORE, STATUS_COMMENT_BEFORE, STATUS_COMMENT_AFTER)
    op.drop_index("ix_orders_customer", table_name="orders")
    op.drop_column("orders", "publish_version_id")
    op.drop_column("orders", "customer_id")
    op.drop_index("uq_customers_email", table_name="customers")
    op.drop_table("customers")
