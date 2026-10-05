"""An order says where it is paid from

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-23

The beneficiary has two sets of bank details (the client, 2026-10-05): the domestic account
number for a customer paying from a bank in the country, the IBAN and SWIFT / BIC for one paying
from abroad. The order form asks ("Paying from") and ``orders.payment_origin`` keeps the answer,
so the confirmation, the order page and the payment e-mail show that set only.

Null for the orders placed before this revision: they are shown the domestic details.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "payment_origin",
            sa.Text(),
            nullable=True,
            comment=(
                "domestic | international: where the customer pays from; decides which bank "
                "details the order shows"
            ),
        ),
    )
    op.create_check_constraint(
        "ck_orders_payment_origin", "orders", "payment_origin IN ('domestic', 'international')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_orders_payment_origin", "orders", type_="check")
    op.drop_column("orders", "payment_origin")
