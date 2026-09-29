"""Drop what no funded row needs: listing imports, the review queue's market rows, the first
order form's columns, the PostGIS image's extra extensions

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-19

The API-surface and schema conformance pass (2026-09-30) against the 220 h POC plan:

- ``market_imports.kind`` / ``market_data.range_basis`` lose ``listings`` with the pasted-listings
  import: the plan funds Monstat and the client's ranges, portal listings are the pilot's. Listing
  imports (and, by cascade, their inputs) are deleted; the server had none when checked.
- ``planning_parameter_extractions``: entity type ``market_data`` (the review queue's market rows
  of 0008) goes: market inputs have their own queue since 0019 and nothing writes such items, so
  every item names a planning field. Such items are deleted; the server had none.
- ``orders.contact_person`` / ``registered_address``: the first order form's fields, refused since
  0031 and empty on every order of the server, are dropped.
- The PostGIS image creates ``postgis_tiger_geocoder`` (a US address geocoder with its ``tiger`` /
  ``tiger_data`` tables), ``postgis_topology`` (the ``topology`` tables) and ``fuzzystrmatch`` in
  its database. No migration created them and nothing uses them (address search is a Photon /
  Nominatim proxy), so they are dropped where present; ``postgis`` (0001) is the one extension.

A downgrade restores the CHECKs, the comments and the two columns (empty). It does not re-create
the image's extensions, which no migration ever created.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column, type, nullable, comment after, comment before)
COMMENTS = (
    (
        "market_imports",
        "kind",
        sa.Text(),
        False,
        "statistics | client_ranges",
        "statistics | client_ranges | listings",
    ),
    (
        "market_imports",
        "source",
        sa.Text(),
        False,
        "who published the figures: Monstat or the client",
        "who published the figures: Monstat, the client, a listings portal",
    ),
    (
        "market_imports",
        "file_id",
        sa.BigInteger(),
        True,
        "the uploaded table",
        "the uploaded table; null for pasted listings",
    ),
    (
        "market_imports",
        "sha256",
        sa.Text(),
        False,
        "checksum of the file",
        "checksum of the file, or of the pasted listings",
    ),
    (
        "market_data",
        "range_basis",
        sa.Text(),
        False,
        "stated | derived (configured range factors) | unavailable",
        "stated | derived (configured range factors) | listings | unavailable",
    ),
    (
        "planning_parameter_extractions",
        "entity_type",
        sa.Text(),
        False,
        "urban_parcel | zone | block | document",
        "urban_parcel | zone | block | document | market_data",
    ),
    (
        "planning_parameter_extractions",
        "parameter_key",
        sa.Text(),
        False,
        "planning field key",
        "planning field key, or a market rate key for market_data",
    ),
)
# (table, name, condition after, condition before)
CHECKS = (
    (
        "market_imports",
        "ck_market_imports_kind",
        "kind IN ('statistics', 'client_ranges')",
        "kind IN ('statistics', 'client_ranges', 'listings')",
    ),
    (
        "market_data",
        "ck_market_data_range_basis",
        "range_basis IN ('stated', 'derived', 'unavailable')",
        "range_basis IN ('stated', 'derived', 'listings', 'unavailable')",
    ),
    (
        "planning_parameter_extractions",
        "ck_planning_parameter_extractions_entity",
        "entity_type IN ('urban_parcel', 'zone', 'block', 'document')",
        "entity_type IN ('urban_parcel', 'zone', 'block', 'document', 'market_data')",
    ),
    (
        "planning_parameter_extractions",
        "ck_planning_parameter_extractions_key",
        "field_key IS NOT NULL",
        "(entity_type = 'market_data') = (field_key IS NULL)",
    ),
)
LEGACY_COMMENT = "no longer collected (0031); kept for older orders"
# dependants first: the tiger geocoder needs fuzzystrmatch
IMAGE_EXTENSIONS = ("postgis_tiger_geocoder", "postgis_topology", "fuzzystrmatch")


def _apply(after: bool) -> None:
    for table, name, condition_after, condition_before in CHECKS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, condition_after if after else condition_before)
    for table, column, type_, nullable, comment_after, comment_before in COMMENTS:
        op.alter_column(
            table,
            column,
            existing_type=type_,
            existing_nullable=nullable,
            comment=comment_after if after else comment_before,
            existing_comment=comment_before if after else comment_after,
        )


def upgrade() -> None:
    op.execute("DELETE FROM market_imports WHERE kind = 'listings'")  # cascades to market_data
    op.execute("DELETE FROM market_data WHERE range_basis = 'listings'")
    op.execute("DELETE FROM planning_parameter_extractions WHERE entity_type = 'market_data'")
    _apply(after=True)
    op.drop_column("orders", "registered_address")
    op.drop_column("orders", "contact_person")
    for name in IMAGE_EXTENSIONS:
        op.execute(f"DROP EXTENSION IF EXISTS {name}")


def downgrade() -> None:
    op.add_column(
        "orders", sa.Column("contact_person", sa.Text(), nullable=True, comment=LEGACY_COMMENT)
    )
    op.add_column(
        "orders", sa.Column("registered_address", sa.Text(), nullable=True, comment=LEGACY_COMMENT)
    )
    _apply(after=False)
