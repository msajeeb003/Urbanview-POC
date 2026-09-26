"""Schemas of the market-data API: imports (``/v1/admin/market``), the market-input review queue
(``/v1/admin/review/market-inputs``) and the coverage table (one row per zone: the sale price per
m² low / expected / high with its source and date, and what is still missing)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MarketMetric = Literal["land_rate", "build_rate", "design_rate", "sale_rate"]
TableKind = Literal["statistics", "client_ranges"]
ImportKind = Literal["statistics", "client_ranges", "listings"]
ImportStatus = Literal["received", "normalised", "failed"]
MarketReviewStatus = Literal["pending", "approved", "amended", "rejected"]
RangeBasis = Literal["stated", "derived", "listings", "unavailable"]


def _not_future(value: date | None) -> date | None:
    if value is not None and value > datetime.now(UTC).date() + timedelta(days=1):
        raise ValueError("the date is in the future")
    return value


# --- imports --------------------------------------------------------------------------------------


class MarketImportIn(BaseModel):
    """Register an uploaded table (``POST /v1/admin/files`` with ``kind = market_data``)."""

    model_config = ConfigDict(extra="forbid")

    file_id: int = Field(gt=0, description="A stored file of kind market_data (.csv / .xlsx)")
    kind: TableKind = Field(
        description="statistics: official tables (Monstat); client_ranges: the client's sheet"
    )
    source: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Who published the figures; statistics default to the profile's source",
    )
    retrieved_on: date = Field(description="When the figures were retrieved or received")
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("retrieved_on")
    @classmethod
    def _not_in_the_future(cls, value: date | None) -> date | None:
        return _not_future(value)

    @model_validator(mode="after")
    def _source_for_client_ranges(self) -> MarketImportIn:
        if self.kind == "client_ranges" and not self.source:
            raise ValueError("source is required for client_ranges (who sent the sheet)")
        return self


class ListingsImportIn(BaseModel):
    """Asking prices pasted from a portal: one listing per line, ``location, EUR/m², date``."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=200, description="e.g. Realitica, Estitor")
    retrieved_on: date
    metric: Literal["sale_rate", "land_rate"] = Field(
        default="sale_rate",
        description="sale_rate: dwellings (EUR per m² of floor); land_rate: plots (per m² of land)",
    )
    listings: str = Field(min_length=1, max_length=200_000)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("retrieved_on")
    @classmethod
    def _not_in_the_future(cls, value: date | None) -> date | None:
        return _not_future(value)


class ItemCounts(BaseModel):
    pending: int = 0
    approved: int = 0
    amended: int = 0
    rejected: int = 0
    applied: int = Field(default=0, description="Approved or amended items that wrote a version")


class MarketImportOut(BaseModel):
    id: int
    kind: ImportKind
    source: str
    retrieved_on: date
    file_id: int | None = None
    filename: str | None = None
    sha256: str
    row_count: int
    status: ImportStatus
    normaliser: str | None = Field(
        default=None, description="rules, or rules+llm:<model>@<prompt version>"
    )
    error: str | None = None
    notes: str | None = None
    job_id: int | None = None
    created_by: str
    created_at: datetime
    normalised_at: datetime | None = None
    items: ItemCounts = Field(default_factory=ItemCounts)
    report: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Detail only: items made, every row / column left out with its reason, issues, the "
            "sheet mappings and the LLM's calls and tokens"
        ),
    )
    raw: dict[str, Any] | None = Field(default=None, description="Detail only: the file as read")


class JobRef(BaseModel):
    id: int
    status_url: str


class MarketImportAccepted(BaseModel):
    market_import: MarketImportOut
    created: bool = Field(description="false: the same content was imported before")
    job: JobRef | None = None


class MarketImportList(BaseModel):
    items: list[MarketImportOut]
    total: int


# --- review ---------------------------------------------------------------------------------------


class MarketRange(BaseModel):
    low: float | None = None
    expected: float
    high: float | None = None


class CurrentRate(BaseModel):
    """The zone's current value of the metric, for comparison."""

    assumptions_id: int
    version: int
    low: float | None = None
    expected: float
    high: float | None = None


class MarketItemOut(BaseModel):
    id: int
    item_type: Literal["market_input"] = "market_input"
    import_id: int
    import_kind: ImportKind
    zone_id: int
    zone_name: str
    metric: MarketMetric
    currency: str
    unit: str
    imported: MarketRange = Field(description="As normalised from the source")
    amended: MarketRange | None = Field(default=None, description="The reviewer's correction")
    effective: MarketRange = Field(description="What approving writes: amended, else imported")
    range_basis: RangeBasis
    source: str
    source_date: date | None = None
    effective_from: date | None = Field(default=None, description="Set on approval")
    confidence: float | None = None
    notes: str | None = None
    flags: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(description="The source row and cells the figure was read from")
    mapping: dict[str, Any] | None = None
    normaliser: str
    status: MarketReviewStatus
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    applied_assumption_id: int | None = Field(
        default=None, description="The assumptions version this item wrote (closed)"
    )
    waiting_for: list[MarketMetric] | None = Field(
        default=None,
        description=(
            "Approved but not applied: the zone has no assumptions yet and these metrics still "
            "need an approved item before its first version is written"
        ),
    )
    current: CurrentRate | None = None
    created_at: datetime


class MarketItemPage(BaseModel):
    items: list[MarketItemOut]
    total: int
    counts: ItemCounts


class MarketApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effective_from: date | None = Field(
        default=None, description="The date the figures apply from; default today"
    )
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("effective_from")
    @classmethod
    def _not_in_the_future(cls, value: date | None) -> date | None:
        return _not_future(value)


class MarketAmendIn(BaseModel):
    """A corrected figure (EUR per m²): low and high both or neither; neither = the configured
    range factors of the zone widen it (refused when none are configured)."""

    model_config = ConfigDict(extra="forbid")

    expected: float = Field(gt=0, le=1_000_000)
    low: float | None = Field(default=None, gt=0, le=1_000_000)
    high: float | None = Field(default=None, gt=0, le=1_000_000)
    effective_from: date | None = None
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("effective_from")
    @classmethod
    def _not_in_the_future(cls, value: date | None) -> date | None:
        return _not_future(value)

    @model_validator(mode="after")
    def _bounds(self) -> MarketAmendIn:
        if (self.low is None) != (self.high is None):
            raise ValueError("give both low and high, or neither")
        if self.low is not None and self.high is not None:
            if not self.low <= self.expected <= self.high:
                raise ValueError("bounds must satisfy low ≤ expected ≤ high")
        return self


class MarketRejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=2000, description="Why the figure is rejected")


# --- coverage -------------------------------------------------------------------------------------


class CoverageRate(BaseModel):
    metric: MarketMetric
    status: Literal["current", "approved_waiting", "pending", "missing"] = Field(
        description=(
            "current: in the zone's current assumptions; approved_waiting: approved, waiting for "
            "the other metrics of the zone's first version; pending: awaiting review; missing"
        )
    )
    low: float | None = None
    expected: float | None = None
    high: float | None = None
    source: str | None = None
    source_date: date | None = None
    market_data_id: int | None = Field(
        default=None, description="The reviewed market input that set the current value"
    )
    pending_item_ids: list[int] = Field(default_factory=list)
    approved_item_ids: list[int] = Field(default_factory=list)


class ZoneCoverage(BaseModel):
    zone_id: int
    zone_name: str
    zone_type: str | None = None
    market_data: bool = Field(
        description="The zone has current assumptions: the panel shows its figures"
    )
    assumptions_id: int | None = None
    assumptions_version: int | None = None
    effective_from: date | None = None
    sale_price_eur_m2: MarketRange | None = Field(
        default=None, description="The current sale price per m² (null: not covered)"
    )
    sale_price_source: str | None = None
    sale_price_source_date: date | None = None
    sale_price_reviewed: bool = Field(
        description="The current sale price was set by an approved market input"
    )
    rates: list[CoverageRate]


class MarketCoverage(BaseModel):
    zones: list[ZoneCoverage]
    zones_total: int
    zones_with_market_data: int
    zones_with_reviewed_sale_price: int
    zones_without_market_data: list[str]
    pending_items: int
