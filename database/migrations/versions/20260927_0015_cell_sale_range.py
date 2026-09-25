"""heatmap_cells: low / high sale rate next to the expected one

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-27

The public map's sale-price choropleth lets the visitor pick the low, expected or high sale rate
of each zone. The publish job writes all three per cell from the zone's current market row: the
row's absolute bounds when it has them, else expected × the row's range factors (the same bounds
the feasibility engine uses).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "heatmap_cells",
        sa.Column(
            "sale_rate_low_eur_m2",
            sa.Float(precision=53),
            nullable=True,
            comment="Low sale rate €/m²: absolute bound, else expected × low factor",
        ),
    )
    op.add_column(
        "heatmap_cells",
        sa.Column(
            "sale_rate_high_eur_m2",
            sa.Float(precision=53),
            nullable=True,
            comment="High sale rate €/m²: absolute bound, else expected × high factor",
        ),
    )


def downgrade() -> None:
    op.drop_column("heatmap_cells", "sale_rate_high_eur_m2")
    op.drop_column("heatmap_cells", "sale_rate_low_eur_m2")
