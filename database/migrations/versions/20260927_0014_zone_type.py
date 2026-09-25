"""zones.zone_type: the zone's planning character, which colours it on the map

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-27

The public map fills each zone with its type colour (wireframe zone types: residential,
commercial, mixed use, public / institutional, green / recreation). The type is data, set with the
zone (seed, or the ``zone_type`` property of a staged ``zones`` feature from QGIS); null means not
classified yet and the map draws the zone in a neutral colour, never a guessed one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMMENT = (
    "Planning character for the map colour: res | com | mix | pub | grn; null = not classified"
)


def upgrade() -> None:
    op.add_column("zones", sa.Column("zone_type", sa.Text(), nullable=True, comment=COMMENT))
    op.create_check_constraint(
        "ck_zones_zone_type",
        "zones",
        "zone_type IS NULL OR zone_type IN ('res', 'com', 'mix', 'pub', 'grn')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_zones_zone_type", "zones", type_="check")
    op.drop_column("zones", "zone_type")
