"""Effective-dated financial assumptions, a saleable share per version, engine proposals

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-05

Financial assumptions become effective-dated (``core.assumptions``): every version of a zone
applies from its ``effective_from`` (the municipality's local date), never before the day it was
saved, until a version with a later date takes effect, the newest version winning on the same day.
The panel therefore reads the version with the latest such date on or before today; a version
dated later is *scheduled* and needs no job to switch it on. ``effective_from`` becomes required:
rows without one (seeded or created before this revision) apply from the day they were created,
rows inserted without one from the UTC date (never the server's own time zone).
``is_current`` keeps marking the newest version of each zone (the head of its history).

``saleable_share`` is the zone's default saleable share of GFA for the version (0-1]; null keeps
the product's 0.70. Visitors still edit it on the panel.

``engine_proposals`` records formulas and data inputs staff propose for the calculation engine
(the admin console's "Add formula" / "Add data input"): audited, listed for the client's review,
never read by the engine, which changes only with a new formula version and validated fixtures.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EFFECTIVE_COMMENT = (
    "local date the version applies from; the panel reads the latest one on or before today, "
    "a later one is scheduled"
)
OLD_EFFECTIVE_COMMENT = "the date the version applies from, when stated"
CURRENT_COMMENT = "the newest version of the zone (head of its history), not what applies today"
# Rows inserted without a date (seeds, market imports that state none) apply from the UTC date:
# never later than the local date east of Greenwich, whatever the server's own time zone.
UTC_TODAY = "(now() AT TIME ZONE 'UTC')::date"


def upgrade() -> None:
    op.execute(
        "UPDATE financial_assumptions SET effective_from = (created_at AT TIME ZONE 'UTC')::date "
        "WHERE effective_from IS NULL"
    )
    op.alter_column(
        "financial_assumptions",
        "effective_from",
        existing_type=sa.Date(),
        nullable=False,
        server_default=sa.text(UTC_TODAY),
        comment=EFFECTIVE_COMMENT,
        existing_comment=OLD_EFFECTIVE_COMMENT,
    )
    op.alter_column(
        "financial_assumptions",
        "is_current",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        existing_server_default=sa.text("false"),
        comment=CURRENT_COMMENT,
    )
    op.add_column(
        "financial_assumptions",
        sa.Column(
            "saleable_share",
            sa.Float(precision=53),
            nullable=True,
            comment="default saleable share of GFA, 0-1; null = the product default 0.70",
        ),
    )
    op.create_check_constraint(
        "ck_financial_assumptions_saleable_share",
        "financial_assumptions",
        "saleable_share IS NULL OR (saleable_share > 0 AND saleable_share <= 1)",
    )

    op.create_table(
        "engine_proposals",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, comment="formula | data_input"),
        sa.Column(
            "name",
            sa.Text(),
            nullable=False,
            comment="the output (formula) or dataset (data input) name",
        ),
        sa.Column("expression", sa.Text(), nullable=True, comment="formula: the expression"),
        sa.Column(
            "source", sa.Text(), nullable=True, comment="formula: where its inputs come from"
        ),
        sa.Column("provides", sa.Text(), nullable=True, comment="data input: what it provides"),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'new'"),
            comment="new (formula) | pending (data input) | accepted | declined",
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False, comment="principal subject"),
        sa.Column(
            "created_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("kind IN ('formula', 'data_input')", name="ck_engine_proposals_kind"),
        sa.CheckConstraint(
            "status IN ('new', 'pending', 'accepted', 'declined')",
            name="ck_engine_proposals_status",
        ),
        sa.CheckConstraint(
            "(kind = 'formula' AND expression IS NOT NULL) OR "
            "(kind = 'data_input' AND provides IS NOT NULL)",
            name="ck_engine_proposals_content",
        ),
    )
    op.create_index(
        "ix_engine_proposals_kind",
        "engine_proposals",
        ["municipality_id", "kind", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_engine_proposals_kind", table_name="engine_proposals")
    op.drop_table("engine_proposals")
    op.drop_constraint(
        "ck_financial_assumptions_saleable_share", "financial_assumptions", type_="check"
    )
    op.drop_column("financial_assumptions", "saleable_share")
    op.alter_column(
        "financial_assumptions",
        "is_current",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        existing_server_default=sa.text("false"),
        comment=None,
        existing_comment=CURRENT_COMMENT,
    )
    op.alter_column(
        "financial_assumptions",
        "effective_from",
        existing_type=sa.Date(),
        nullable=True,
        server_default=None,
        comment=OLD_EFFECTIVE_COMMENT,
        existing_comment=EFFECTIVE_COMMENT,
    )
