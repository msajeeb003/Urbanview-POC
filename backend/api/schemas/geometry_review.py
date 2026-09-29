"""Schemas for the geometry review (``/v1/admin/geometry``): the staged geometry batches the pilot
technical scope calls ``staging.geometry_draft`` (origin vector_pdf / manual_qgis / official_gis,
``qa_status`` / ``qa_issues``: overlaps, gaps, area deviation, ``review_status``), reviewed like
the extracted values before the publish job may apply them."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api.schemas.admin import GeoreferenceOut

GeometryOrigin = Literal["vector_pdf", "manual_qgis", "official_gis"]
GeometryReviewStatus = Literal["pending", "approved", "rejected"]
QaStatus = Literal["pass", "warn", "fail"]


class QaIssueOut(BaseModel):
    code: str = Field(
        description=(
            "invalid_geometry | empty_geometry (errors), overlap | gap | area_deviation "
            "(warnings), or the producing dataset's warning as <kind>.<code>"
        )
    )
    severity: Literal["error", "warning"]
    message: str = Field(description="One sentence for the console (English)")
    count: int
    features: list[str] = Field(
        default_factory=list, description="What to look at: feature keys, pairs, parcel lines"
    )
    keys: list[str] = Field(default_factory=list, description="The feature keys concerned")
    locations: list[list[float]] = Field(
        default_factory=list, description="[lng, lat, m²] of each gap listed"
    )
    area_m2: float | None = None


class GeometryDocumentRef(BaseModel):
    id: int
    name: str
    short_code: str | None = None
    type: str | None = None


class GeometryDatasetRef(BaseModel):
    kind: Literal["georef", "zones", "cadastre"]
    version: str
    status: str


class GeometryDraft(BaseModel):
    """One staged geometry batch: one layer of one producing run (a georeferenced plan or GIS
    drawing, a zone import, a cadastral import)."""

    id: int
    layer_id: str
    layer_label: str
    origin: GeometryOrigin | None = Field(
        default=None, description="Null for a batch staged before geometry review (0033)"
    )
    status: Literal["staged", "published", "superseded", "rejected"] = Field(
        description="The batch's life: staged (waiting for a publish), published, superseded by a "
        "newer run, rejected (final: stage the geometry again)"
    )
    review_status: GeometryReviewStatus | None = Field(
        default=None, description="Null = published before geometry review"
    )
    feature_count: int
    document: GeometryDocumentRef | None = None
    dataset: GeometryDatasetRef | None = None
    dataset_version: str | None = Field(
        default=None, description="The producing run's label as the batch records it"
    )
    produced_by: str | None = None
    created_at: datetime
    qa_status: QaStatus | None = Field(
        default=None, description="Null = not checked yet (checked when approved)"
    )
    qa_issues: list[QaIssueOut] = Field(default_factory=list)
    bbox: list[float] | None = Field(default=None, description="[west, south, east, north]")
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    published_version_id: int | None = None
    published_at: datetime | None = None
    georeference: GeoreferenceOut | None = Field(
        default=None, description="The georeferencing run that staged it: fit, snapping, checks"
    )
    can_approve: bool
    approve_blocker: Literal["qa_failed", "published", "superseded", "rejected"] | None = Field(
        default=None, description="Why it cannot be approved now"
    )
    can_reject: bool


class GeometryCounts(BaseModel):
    pending: int = Field(description="Staged, waiting for a decision (they block publishing)")
    approved: int = Field(description="Staged and approved: the next publish applies them")
    rejected: int
    failing: int = Field(description="Pending batches whose QA fails: reject and stage again")


class GeometryPage(BaseModel):
    items: list[GeometryDraft]
    total: int
    limit: int
    offset: int
    counts: GeometryCounts


class GeometryFeatures(BaseModel):
    """The batch's features for the review preview: simplified geometry, a label, the issue codes
    that name them; plus the gap locations of its QA."""

    batch_id: int
    layer_id: str
    bbox: list[float] | None = None
    total: int
    truncated: bool
    features: dict[str, Any] = Field(description="GeoJSON FeatureCollection (EPSG:4326)")
    gaps: list[list[float]] = Field(default_factory=list, description="[lng, lat, m²]")


def _trim(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _trim_to_none(value: Any) -> Any:
    return (value.strip() or None) if isinstance(value, str) else value


class GeometryApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)

    _note = field_validator("note", mode="before")(_trim_to_none)


class GeometryRejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=2000, description="The reason (not blank)")

    _note = field_validator("note", mode="before")(_trim)


class GeometryBulkApproveIn(BaseModel):
    """Approve the pending batches of one producing dataset, one document, or given ids."""

    model_config = ConfigDict(extra="forbid")

    batch_ids: list[int] | None = Field(default=None, max_length=200)
    dataset_version: str | None = Field(default=None, min_length=1, max_length=120)
    document_id: int | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=2000)

    _note = field_validator("note", mode="before")(_trim_to_none)

    @model_validator(mode="after")
    def _a_selector(self) -> GeometryBulkApproveIn:
        if not self.batch_ids and self.dataset_version is None and self.document_id is None:
            raise ValueError("give batch_ids, a dataset_version or a document_id")
        if self.batch_ids is not None and any(i <= 0 for i in self.batch_ids):
            raise ValueError("batch ids are positive integers")
        return self


class GeometryBulkSkipped(BaseModel):
    id: int
    reason: Literal["not_found", "not_pending", "qa_failed", "not_open"]


class GeometryBulkResult(BaseModel):
    approved: list[int]
    skipped: list[GeometryBulkSkipped]


class GeometryBlocker(BaseModel):
    """A staged batch still waiting for a decision: publishing waits for it."""

    batch_id: int
    layer_id: str
    layer_label: str
    document_id: int | None = None
    document_name: str | None = None
    dataset_version: str | None = None
    qa_status: QaStatus | None = None
