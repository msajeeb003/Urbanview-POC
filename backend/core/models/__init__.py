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
from core.models.admin import (
    AuditLogEntry,
    PipelineJob,
    StaffLoginToken,
    StaffSession,
    StaffUser,
    StoredFile,
)
from core.models.analytics import AnalyticsEventRecord
from core.models.extraction import ExtractionRun, ExtractionRunChunk
from core.models.market import MarketDataItem, MarketImport
from core.models.orders import EmailLogEntry, Order
from core.models.panel import (
    FinancialAssumption,
    PlanningField,
    PlanningParameterExtraction,
    PlanningParameterValue,
    PlanningValueGap,
    PublishVersion,
    ReviewState,
    ZoneParameterSet,
)
from core.models.planning import (
    CadastralParcel,
    PlanningDocument,
    PlanningDocumentStatus,
    UrbanBlock,
    UrbanParcel,
    Zone,
)
from core.models.publish import (
    GeometryBatch,
    HeatmapCell,
    LayerFeature,
    ParcelLink,
    StagingGeometry,
)

__all__ = [
    "AnalyticsEventRecord",
    "AuditLogEntry",
    "Base",
    "CadastralParcel",
    "EmailLogEntry",
    "ExtractionRun",
    "ExtractionRunChunk",
    "FinancialAssumption",
    "GeometryBatch",
    "HeatmapCell",
    "LayerFeature",
    "MarketDataItem",
    "MarketImport",
    "Order",
    "ParcelLink",
    "PlanningDocument",
    "PlanningDocumentStatus",
    "PlanningField",
    "PlanningParameterExtraction",
    "PipelineJob",
    "PlanningParameterValue",
    "PlanningValueGap",
    "PublishVersion",
    "ReviewState",
    "StaffLoginToken",
    "StaffSession",
    "StaffUser",
    "StagingGeometry",
    "StoredFile",
    "UrbanBlock",
    "UrbanParcel",
    "Zone",
    "ZoneParameterSet",
]
