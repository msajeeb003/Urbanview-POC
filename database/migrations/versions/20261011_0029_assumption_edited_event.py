"""analytics_events: the assumption_edited event

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-11

The public map records when a visitor edits one of the three Group 2 assumptions (construction
cost, sale price, saleable share): one ``assumption_edited`` event once the slider settles. The
event list of BRD §6.2 has no such event; the POC check of Group 2 asks for it. The name check of
``analytics_events`` takes the fourteenth name; a downgrade deletes those rows first.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMES_BEFORE = (
    "map_loaded",
    "search_performed",
    "parcel_selected",
    "layer_toggled",
    "panel_viewed",
    "financials_viewed",
    "source_reference_opened",
    "order_started",
    "checkout_completed",
    "return_visit",
    "sessions_per_user",
    "market_data_interest",
    "ai_interest",
)
NAMES_AFTER = (*NAMES_BEFORE, "assumption_edited")


def _names(names: Sequence[str]) -> str:
    return "name IN (" + ", ".join(f"'{name}'" for name in names) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_analytics_events_name", "analytics_events", type_="check")
    op.create_check_constraint("ck_analytics_events_name", "analytics_events", _names(NAMES_AFTER))


def downgrade() -> None:
    op.execute("DELETE FROM analytics_events WHERE name = 'assumption_edited'")
    op.drop_constraint("ck_analytics_events_name", "analytics_events", type_="check")
    op.create_check_constraint("ck_analytics_events_name", "analytics_events", _names(NAMES_BEFORE))
