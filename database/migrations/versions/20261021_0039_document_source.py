"""A document's source is a registry name, never an import's name

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-21

The zone import (``core.zones.staging``) wrote ``zone list <dataset label>`` as the ``source`` of
a planning document that has no eRegistri reference. ``source`` is public: the zone panel printed
"zone list podgorica-zones-20260927-1" where a registry name belongs (tester's report,
2026-10-02). The import now leaves ``source`` empty for such a document (the dataset stays in
``dataset_version``); this revision clears the rows already written.

A downgrade restores nothing: which import wrote a row is still in ``dataset_version``.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE planning_documents SET source = NULL WHERE source LIKE 'zone list %'")


def downgrade() -> None:
    pass
