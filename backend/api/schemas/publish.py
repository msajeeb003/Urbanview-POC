"""Schemas of the publish API (``/v1/admin/publish``) and the public tiles pointer
(``GET /v1/tiles/current``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from api.schemas.admin import JobOut


class PublishRequest(BaseModel):
    label: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
        description="Data version label; default `YYYY-MM-DD.n`",
    )
    notes: str | None = Field(default=None, max_length=2000)


class PublishBlocker(BaseModel):
    document_id: int
    document_name: str
    pending: int


class LayerInfo(BaseModel):
    id: str = Field(description="Source-layer name inside the archive")
    geometry_type: str
    min_zoom: int
    max_zoom: int
    features: int


class PublishVersionOut(BaseModel):
    id: int
    label: str
    is_current: bool
    published_at: datetime
    published_by: str | None = None
    formula_version: str
    notes: str | None = None
    previous_version_id: int | None = None
    job_id: int | None = None
    archive_key: str | None = Field(default=None, description="null: no archive (seed) or pruned")
    archive_size_bytes: int | None = None
    archive_sha256: str | None = None
    archive_url: str | None = Field(default=None, description="Signed, short-lived")
    archive_pruned_at: datetime | None = None
    layers: list[LayerInfo] = Field(default_factory=list)
    counts: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int | None = None
    min_zoom: int | None = None
    max_zoom: int | None = None
    rolled_back_at: datetime | None = None
    rolled_back_by: str | None = None


class PublishStatus(BaseModel):
    current: PublishVersionOut | None = Field(description="What the public map serves")
    versions: list[PublishVersionOut] = Field(description="Newest first, the current included")
    active_job: JobOut | None = Field(description="A queued / running publish with its progress")
    last_job: JobOut | None = Field(description="The most recent publish job")
    can_publish: bool
    blockers: list[PublishBlocker] = Field(description="Documents with items pending review")
    keep_versions: int = Field(description="Archives kept for rollback (retention)")


class RollbackRequest(BaseModel):
    version_id: int | None = Field(
        default=None, gt=0, description="Default: the version before the current one"
    )


class MetricClasses(BaseModel):
    method: str = Field(description="quantile | fixed (the municipality profile's bands)")
    unit: str | None = None
    breaks: list[float] = Field(
        description="Ascending class starts after the first class: a value v is in class = the "
        "number of breaks <= v"
    )
    min: float | None = None
    max: float | None = None
    count: int = Field(description="Cells with a value")
    null_count: int = Field(description="Cells without a value: drawn as no data")
    zero_class: bool = Field(description="0 is its own class first (sale rate: not saleable)")


class CellClasses(BaseModel):
    """Choropleth classes of the current version's cells, per tile layer and metric."""

    block_cells: dict[str, MetricClasses] = Field(default_factory=dict)
    zone_cells: dict[str, MetricClasses] = Field(default_factory=dict)


class TilesCurrent(BaseModel):
    status: str = Field(description="published | unpublished")
    version_id: int | None = None
    data_version: str = Field(description="Label of the current version, or `unpublished`")
    published_at: datetime | None = None
    archive_url: str | None = Field(
        default=None, description="Signed URL of the PMTiles archive (HTTP range requests)"
    )
    expires_at: datetime | None = None
    layers: list[LayerInfo] = Field(default_factory=list)
    min_zoom: int | None = None
    max_zoom: int | None = None
    cell_classes: CellClasses | None = Field(
        default=None,
        description="Classes of the block / zone cells: the map colours and legend share them",
    )
