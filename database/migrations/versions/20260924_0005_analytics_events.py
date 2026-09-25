"""analytics_events: client-side product events (the prototype is a validation instrument)

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-24

One row per event accepted by ``POST /v1/events``. Names are the 13 product events (CHECK), ids
are anonymous and client-generated, ``properties`` is a small flat object that never holds names,
emails or IPs (enforced at ingest). ``zone_id`` / ``parcel_id`` are copied out of ``properties``
for grouping (no foreign keys: zones and parcels may be re-seeded under the events). Indexed by
name + time (the dashboard's range queries), session and zone.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_NAMES = (
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


def upgrade() -> None:
    names = ", ".join(f"'{name}'" for name in EVENT_NAMES)
    op.create_table(
        "analytics_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, comment="one of the 13 product event names"),
        sa.Column(
            "session_id",
            sa.Text(),
            nullable=False,
            comment="anonymous, client-generated session id",
        ),
        sa.Column(
            "client_id",
            sa.Text(),
            nullable=True,
            comment="anonymous, client-generated persistent id (repeat usage); never personal",
        ),
        sa.Column(
            "event_id",
            sa.Text(),
            nullable=True,
            comment="client-generated id that de-duplicates retried batches",
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="client clock, stored in UTC",
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "properties",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="small flat object; never names, emails or IPs",
        ),
        sa.Column(
            "zone_id",
            sa.BigInteger(),
            nullable=True,
            comment="properties.zone_id copied for grouping (no FK: zones may be re-seeded)",
        ),
        sa.Column(
            "parcel_id",
            sa.BigInteger(),
            nullable=True,
            comment="properties.parcel_id (cadastral Parcel ID) copied for grouping",
        ),
        sa.CheckConstraint(f"name IN ({names})", name="ck_analytics_events_name"),
    )
    op.create_index(
        "ix_analytics_events_name_time",
        "analytics_events",
        ["municipality_id", "name", "occurred_at"],
    )
    op.create_index(
        "ix_analytics_events_session", "analytics_events", ["municipality_id", "session_id"]
    )
    op.create_index(
        "ix_analytics_events_zone",
        "analytics_events",
        ["municipality_id", "zone_id"],
        postgresql_where=sa.text("zone_id IS NOT NULL"),
    )
    op.create_index(
        "uq_analytics_events_event_id",
        "analytics_events",
        ["municipality_id", "event_id"],
        unique=True,
        postgresql_where=sa.text("event_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("analytics_events")
