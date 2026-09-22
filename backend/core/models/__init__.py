"""SQLAlchemy models. Import every model module here so Alembic autogenerate sees it.

Conventions (see CLAUDE.md):
- every domain table has ``municipality_id`` (text) matching ``municipalities/<id>.toml``;
- geometry is stored as ``geometry(MultiPolygon, 4326)`` with an explicit GiST ``Index`` named
  ``idx_<table>_<column>`` in ``__table_args__`` (see ``planning.gist_index``);
- cadastral parcels and planned urban parcels are separate tables, never merged;
- planning values are served from ``planning_parameter_values`` only; the review queue
  ``planning_parameter_extractions`` never reaches the public API (see ``panel``).
"""

from core.db import Base
from core.models.panel import (
    FinancialAssumption,
    PlanningField,
    PlanningParameterExtraction,
    PlanningParameterValue,
    PublishVersion,
    ReviewState,
)
from core.models.planning import (
    CadastralParcel,
    PlanningDocument,
    PlanningDocumentStatus,
    UrbanBlock,
    UrbanParcel,
    Zone,
)

__all__ = [
    "Base",
    "CadastralParcel",
    "FinancialAssumption",
    "PlanningDocument",
    "PlanningDocumentStatus",
    "PlanningField",
    "PlanningParameterExtraction",
    "PlanningParameterValue",
    "PublishVersion",
    "ReviewState",
    "UrbanBlock",
    "UrbanParcel",
    "Zone",
]
