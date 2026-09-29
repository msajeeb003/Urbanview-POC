"""publish versions are numbered, review items name the version that published them, published
values refuse UPDATE

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-16

The A4 check of the publish pipeline (pilot technical scope: ``serving.dataset_version.version_no``
unique per municipality, ``extraction_item.published_version_id``, serving rows never updated in
place after publish):

- ``publish_versions.version_no``: 1, 2, 3 … per municipality in publish order, assigned by the
  publish job; backfilled in id order.
- ``planning_parameter_extractions.published_version_id``: the version whose publish served the
  item, set with ``published_value_id``; backfilled from the value row.
- ``planning_parameter_values`` refuses UPDATE (a trigger, like ``audit_log``'s): a published value
  is never changed in place; the next version carries its own rows.

A downgrade drops the trigger and the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VERSION_NO_COMMENT = "1, 2, 3 … per municipality in publish order"
PUBLISHED_VERSION_COMMENT = "the publish version that served the item (with published_value_id)"

BACKFILL = (
    """
    UPDATE publish_versions p SET version_no = n.version_no
    FROM (SELECT id, row_number() OVER (PARTITION BY municipality_id ORDER BY id) AS version_no
          FROM publish_versions) n
    WHERE n.id = p.id
    """,
    """
    UPDATE planning_parameter_extractions e SET published_version_id = v.publish_version_id
    FROM planning_parameter_values v
    WHERE v.id = e.published_value_id
    """,
)
IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION planning_parameter_values_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'planning_parameter_values are immutable once published: % is not permitted',
        TG_OP USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;
"""
UPDATE_TRIGGER = """
CREATE TRIGGER planning_parameter_values_no_update
BEFORE UPDATE ON planning_parameter_values
FOR EACH ROW EXECUTE FUNCTION planning_parameter_values_immutable();
"""


def upgrade() -> None:
    op.add_column(
        "publish_versions",
        sa.Column("version_no", sa.Integer(), nullable=True, comment=VERSION_NO_COMMENT),
    )
    op.add_column(
        "planning_parameter_extractions",
        sa.Column(
            "published_version_id",
            sa.BigInteger(),
            sa.ForeignKey("publish_versions.id", ondelete="SET NULL"),
            nullable=True,
            comment=PUBLISHED_VERSION_COMMENT,
        ),
    )
    for statement in BACKFILL:
        op.execute(statement)
    op.alter_column(
        "publish_versions",
        "version_no",
        existing_type=sa.Integer(),
        nullable=False,
        existing_comment=VERSION_NO_COMMENT,
    )
    op.create_unique_constraint(
        "uq_publish_versions_version_no", "publish_versions", ["municipality_id", "version_no"]
    )
    op.execute(IMMUTABLE_FUNCTION)
    op.execute(UPDATE_TRIGGER)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS planning_parameter_values_no_update ON planning_parameter_values"
    )
    op.execute("DROP FUNCTION IF EXISTS planning_parameter_values_immutable()")
    op.drop_constraint("uq_publish_versions_version_no", "publish_versions", type_="unique")
    op.drop_column("planning_parameter_extractions", "published_version_id")
    op.drop_column("publish_versions", "version_no")
