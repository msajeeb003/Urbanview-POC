"""planning_documents.adopted_on: the date a plan was adopted, when known

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-28

The zone panel lists each planning document with its status and adoption date. The date comes
from the registry (official gazette) and is entered when staff register the document; it stays
null until someone enters it, and the panel then shows no date rather than a guess.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "planning_documents",
        sa.Column(
            "adopted_on",
            sa.Date(),
            nullable=True,
            comment="Adoption date (official gazette), when known",
        ),
    )


def downgrade() -> None:
    op.drop_column("planning_documents", "adopted_on")
