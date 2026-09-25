"""admin configuration: financial assumption versions with absolute bounds, zone parameter sets

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-24

``financial_assumptions`` becomes a version history per zone (``version``, ``supersedes_id``,
``retired_at`` / ``retired_by``; ``is_current`` marks the row the panel reads) and gains optional
absolute low / high bounds per rate (checked low ≤ rate ≤ high, both or neither) next to the
multiplier factors. ``zone_parameter_sets`` holds the typical planning values of a zone (land
use, FAR, coverage, height, floors) with a source document reference and a verification date,
versioned the same way; the current row is the zone panel's ``typical_parameters``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RATES = ("land", "build", "design", "sale")
BOUNDS_CHECK = " AND ".join(
    f"(({r}_rate_low_eur_m2 IS NULL AND {r}_rate_high_eur_m2 IS NULL) OR "
    f"({r}_rate_low_eur_m2 IS NOT NULL AND {r}_rate_high_eur_m2 IS NOT NULL AND "
    f"{r}_rate_low_eur_m2 > 0 AND {r}_rate_low_eur_m2 <= {r}_rate_eur_m2 AND "
    f"{r}_rate_eur_m2 <= {r}_rate_high_eur_m2))"
    for r in RATES
)
VALUES_CHECK = (
    "(max_far IS NULL OR max_far >= 0) AND "
    "(max_site_coverage_pct IS NULL OR (max_site_coverage_pct >= 0 AND max_site_coverage_pct <= 100)) AND "
    "(max_height_m IS NULL OR max_height_m >= 0) AND (max_floors IS NULL OR max_floors >= 0) AND "
    "(source_page IS NULL OR source_page >= 1)"
)


def upgrade() -> None:
    table = "financial_assumptions"
    op.add_column(
        table, sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1"))
    )
    op.add_column(
        table,
        sa.Column(
            "supersedes_id",
            sa.BigInteger(),
            sa.ForeignKey("financial_assumptions.id", ondelete="SET NULL"),
            nullable=True,
            comment="the previous version of this zone's assumptions",
        ),
    )
    op.add_column(table, sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(table, sa.Column("retired_by", sa.Text(), nullable=True))
    for rate in RATES:
        for side in ("low", "high"):
            op.add_column(
                table,
                sa.Column(
                    f"{rate}_rate_{side}_eur_m2",
                    sa.Float(precision=53),
                    nullable=True,
                    comment=f"absolute {side} bound of the {rate} rate; null = use the factor",
                ),
            )
    op.create_check_constraint("ck_financial_assumptions_bounds", table, BOUNDS_CHECK)
    op.create_index(
        "ix_financial_assumptions_history", table, ["municipality_id", "zone_id", "version"]
    )

    op.create_table(
        "zone_parameter_sets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "zone_id",
            sa.BigInteger(),
            sa.ForeignKey("zones.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "supersedes_id",
            sa.BigInteger(),
            sa.ForeignKey("zone_parameter_sets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("land_use", sa.Text(), nullable=True),
        sa.Column("max_far", sa.Float(precision=53), nullable=True, comment="II, typical"),
        sa.Column(
            "max_site_coverage_pct", sa.Float(precision=53), nullable=True, comment="IZ %, typical"
        ),
        sa.Column("max_height_m", sa.Float(precision=53), nullable=True),
        sa.Column("max_floors", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "source_document_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_page", sa.Integer(), nullable=True),
        sa.Column("source_note", sa.Text(), nullable=True),
        sa.Column("verified_on", sa.Date(), nullable=True, comment="expert verification date"),
        sa.Column("verified_by", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by", sa.Text(), nullable=True),
        sa.Column("dataset_version", sa.Text(), nullable=True),
        sa.CheckConstraint(VALUES_CHECK, name="ck_zone_parameter_sets_values"),
    )
    op.create_index(
        "uq_zone_parameter_sets_current",
        "zone_parameter_sets",
        ["zone_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_index(
        "ix_zone_parameter_sets_zone",
        "zone_parameter_sets",
        ["municipality_id", "zone_id", "version"],
    )


def downgrade() -> None:
    op.drop_table("zone_parameter_sets")
    table = "financial_assumptions"
    op.drop_index("ix_financial_assumptions_history", table_name=table)
    op.drop_constraint("ck_financial_assumptions_bounds", table, type_="check")
    for rate in RATES:
        for side in ("low", "high"):
            op.drop_column(table, f"{rate}_rate_{side}_eur_m2")
    for column in ("retired_by", "retired_at", "supersedes_id", "version"):
        op.drop_column(table, column)
