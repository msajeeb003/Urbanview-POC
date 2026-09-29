"""POC scope: drop the tables and columns of features the POC plan does not fund

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-17

The conformance pass against the 220 h POC plan (2026-09-29) removed the calculation-engine
proposals ("+ Add formula" / "+ Add data input"), the zone parameter sets (the Planning rules
editor and the zone panel's typical parameters) and the publish rollback endpoint. Their storage
goes with them:

- table ``engine_proposals`` (0023);
- table ``zone_parameter_sets`` (0007);
- columns ``publish_versions.rolled_back_at`` / ``rolled_back_by`` (0011).

Earlier publish versions keep their values, links, cells and archive (retention) for a manual
pointer flip by an operator. A downgrade recreates the tables (empty) and the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ZONE_PARAMETER_VALUES_CHECK = (
    "(max_far IS NULL OR max_far >= 0) AND "
    "(max_site_coverage_pct IS NULL OR "
    "(max_site_coverage_pct >= 0 AND max_site_coverage_pct <= 100)) AND "
    "(max_height_m IS NULL OR max_height_m >= 0) AND (max_floors IS NULL OR max_floors >= 0) AND "
    "(source_page IS NULL OR source_page >= 1)"
)


def upgrade() -> None:
    op.drop_index("ix_engine_proposals_kind", table_name="engine_proposals")
    op.drop_table("engine_proposals")
    op.drop_index("ix_zone_parameter_sets_zone", table_name="zone_parameter_sets")
    op.drop_index("uq_zone_parameter_sets_current", table_name="zone_parameter_sets")
    op.drop_table("zone_parameter_sets")
    op.drop_column("publish_versions", "rolled_back_by")
    op.drop_column("publish_versions", "rolled_back_at")


def downgrade() -> None:
    op.add_column(
        "publish_versions", sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("publish_versions", sa.Column("rolled_back_by", sa.Text(), nullable=True))

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
        sa.CheckConstraint(ZONE_PARAMETER_VALUES_CHECK, name="ck_zone_parameter_sets_values"),
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
