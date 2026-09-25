"""review queue and append-only audit: extraction items carry their target (zone / block / urban
parcel / market data), the raw text snippet, the extractor's confidence and the reviewer's
corrected value; audit_log gains before / after / note and refuses UPDATE, DELETE and TRUNCATE

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-24

100% of AI-extracted planning information is reviewed by an expert before publication. The
review queue (``GET /v1/admin/review``) reads ``planning_parameter_extractions`` (STAGING); a
decision never overwrites the AI value: an amendment stores the corrected value alongside it.
``audit_log`` is append-only at the database level: a trigger raises on every UPDATE, DELETE and
TRUNCATE, whatever role the connection uses; its ``actor_user_id`` loses the cascading foreign
key for the same reason.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "planning_parameter_extractions"
AMENDED_CORRECTION = "reviewer's corrected value; the AI value stays"

APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % is not permitted', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;
"""
ROW_TRIGGER = """
CREATE TRIGGER audit_log_no_update_delete
BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
"""
TRUNCATE_TRIGGER = """
CREATE TRIGGER audit_log_no_truncate
BEFORE TRUNCATE ON audit_log
FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only();
"""


def upgrade() -> None:
    op.alter_column(TABLE, "field_key", nullable=True)
    op.add_column(
        TABLE,
        sa.Column(
            "entity_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'urban_parcel'"),
            comment="urban_parcel | zone | block | document | market_data",
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "zone_id", sa.BigInteger(), sa.ForeignKey("zones.id", ondelete="SET NULL"), nullable=True
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "block_id",
            sa.BigInteger(),
            sa.ForeignKey("urban_blocks.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "parameter_key",
            sa.Text(),
            nullable=True,
            comment="planning field key, or a market rate key for market_data",
        ),
    )
    op.execute(f"UPDATE {TABLE} SET parameter_key = field_key WHERE parameter_key IS NULL")
    op.alter_column(TABLE, "parameter_key", nullable=False)
    op.add_column(
        TABLE,
        sa.Column(
            "raw_text",
            sa.Text(),
            nullable=True,
            comment="the text the value was read from (snippet)",
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "confidence",
            sa.Float(precision=53),
            nullable=True,
            comment="extractor confidence 0..1",
        ),
    )
    op.add_column(
        TABLE, sa.Column("amended_value_text", sa.Text(), nullable=True, comment=AMENDED_CORRECTION)
    )
    op.add_column(
        TABLE,
        sa.Column(
            "amended_value_number",
            sa.Float(precision=53),
            nullable=True,
            comment=AMENDED_CORRECTION,
        ),
    )
    op.add_column(TABLE, sa.Column("amended_unit", sa.Text(), nullable=True))
    op.add_column(
        TABLE,
        sa.Column(
            "reviewed_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_planning_parameter_extractions_entity",
        TABLE,
        "entity_type IN ('urban_parcel', 'zone', 'block', 'document', 'market_data')",
    )
    op.create_check_constraint(
        "ck_planning_parameter_extractions_key",
        TABLE,
        "(entity_type = 'market_data') = (field_key IS NULL)",
    )
    op.create_check_constraint(
        "ck_planning_parameter_extractions_amended",
        TABLE,
        "review_state <> 'amended' OR num_nonnulls(amended_value_text, amended_value_number) = 1",
    )
    op.create_check_constraint(
        "ck_planning_parameter_extractions_confidence",
        TABLE,
        "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
    )
    op.create_index(
        "ix_planning_parameter_extractions_queue",
        TABLE,
        ["municipality_id", "review_state", "document_id"],
    )
    op.create_index(
        "ix_planning_parameter_extractions_page", TABLE, ["document_id", "source_page"]
    )

    op.add_column(
        "audit_log",
        sa.Column(
            "before",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="state before the action (entity-specific summary)",
        ),
    )
    op.add_column(
        "audit_log",
        sa.Column(
            "after",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="state after the action",
        ),
    )
    op.add_column(
        "audit_log",
        sa.Column("note", sa.Text(), nullable=True, comment="free text from the actor"),
    )
    # An append-only log cannot carry a cascading foreign key: deleting a staff user would have
    # to update its rows. The user id stays as a plain historical reference next to the subject.
    op.drop_constraint("audit_log_actor_user_id_fkey", "audit_log", type_="foreignkey")
    op.execute(APPEND_ONLY_FUNCTION)
    op.execute(ROW_TRIGGER)
    op.execute(TRUNCATE_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only()")
    op.create_foreign_key(
        "audit_log_actor_user_id_fkey",
        "audit_log",
        "staff_users",
        ["actor_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    for column in ("note", "after", "before"):
        op.drop_column("audit_log", column)

    op.drop_index("ix_planning_parameter_extractions_page", table_name=TABLE)
    op.drop_index("ix_planning_parameter_extractions_queue", table_name=TABLE)
    for name in (
        "ck_planning_parameter_extractions_confidence",
        "ck_planning_parameter_extractions_amended",
        "ck_planning_parameter_extractions_key",
        "ck_planning_parameter_extractions_entity",
    ):
        op.drop_constraint(name, TABLE, type_="check")
    for column in (
        "reviewed_by_user_id",
        "amended_unit",
        "amended_value_number",
        "amended_value_text",
        "confidence",
        "raw_text",
        "parameter_key",
        "block_id",
        "zone_id",
        "entity_type",
    ):
        op.drop_column(TABLE, column)
    op.alter_column(TABLE, "field_key", nullable=False)
