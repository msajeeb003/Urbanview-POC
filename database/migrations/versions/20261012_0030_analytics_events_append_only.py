"""analytics_events: append-only at the database level

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-12

The pilot technical scope keeps the product events in an append-only table. The API only ever
inserts (``SqlAnalyticsRepository.insert_events``); from this revision the database refuses UPDATE,
DELETE and TRUNCATE on ``analytics_events`` for every role, as ``audit_log`` does (0008). A
downgrade drops the triggers first, so 0029's downgrade can still remove its rows. The ``name``
column comment stops counting the events (it said 13 since 0029 added the fourteenth).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION analytics_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'analytics_events is append-only: % is not permitted', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;
"""
ROW_TRIGGER = """
CREATE TRIGGER analytics_events_no_update_delete
BEFORE UPDATE OR DELETE ON analytics_events
FOR EACH ROW EXECUTE FUNCTION analytics_events_append_only();
"""
TRUNCATE_TRIGGER = """
CREATE TRIGGER analytics_events_no_truncate
BEFORE TRUNCATE ON analytics_events
FOR EACH STATEMENT EXECUTE FUNCTION analytics_events_append_only();
"""


NAME_COMMENT_BEFORE = "one of the 13 product event names"
NAME_COMMENT_AFTER = "one of the product event names (the API's enum)"


def _name_comment(comment: str, previous: str) -> None:
    op.alter_column(
        "analytics_events",
        "name",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=comment,
        existing_comment=previous,
    )


def upgrade() -> None:
    op.execute(APPEND_ONLY_FUNCTION)
    op.execute(ROW_TRIGGER)
    op.execute(TRUNCATE_TRIGGER)
    _name_comment(NAME_COMMENT_AFTER, NAME_COMMENT_BEFORE)


def downgrade() -> None:
    _name_comment(NAME_COMMENT_BEFORE, NAME_COMMENT_AFTER)
    op.execute("DROP TRIGGER IF EXISTS analytics_events_no_truncate ON analytics_events")
    op.execute("DROP TRIGGER IF EXISTS analytics_events_no_update_delete ON analytics_events")
    op.execute("DROP FUNCTION IF EXISTS analytics_events_append_only()")
