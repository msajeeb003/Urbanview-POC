"""An order remembers the language the map was in

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-22

E-mails are written in one language (product owner, 2026-10-02): the one the app was in when the
order was placed or the staff sign-in link was asked for, no longer Montenegrin and English in
one message. ``orders.language`` keeps the order's, so the e-mail that brings the report days
later is in the customer's language too. A sign-in link's language travels in its job payload.

Null for the orders placed before this revision: their e-mails take ``MAIL_DEFAULT_LANGUAGE``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "language",
            sa.Text(),
            nullable=True,
            comment=(
                "en | me: the language the map was in at the order; its e-mails are written in it"
            ),
        ),
    )
    op.create_check_constraint("ck_orders_language", "orders", "language IN ('en', 'me')")


def downgrade() -> None:
    op.drop_constraint("ck_orders_language", "orders", type_="check")
    op.drop_column("orders", "language")
