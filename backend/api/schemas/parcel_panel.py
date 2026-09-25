"""Display-shaped panels: ``GET /v1/parcels/{id}/panel`` and ``GET /v1/zones/{id}/panel``
(contract ``docs/specs/panel-payload.md`` section 13).

Everything the panel shows for a parcel in one response, in display order, with bilingual labels
from the field dictionary and ``panel_text`` so the frontend hard-codes none: the header (KO,
parcel number, zone, documents with status, cadastral vs planned area and which one is the
calculation basis and why), Group 1 (the 11 planning fields, each stated with a source reference
or null with a reason, never a default), the market sale price range, the effective assumptions and
Group 2 (the 7 financial figures as low / expected / high from the shared engine) plus the engine
inputs the browser recalculates from when a visitor edits an assumption.

Numbers are raw JSON numbers (areas one decimal, ``_pct`` 0–100, ``_share`` 0–1, money as the
engine rounds it); the client formats them per language.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from api.schemas.locate import LatLng
from api.schemas.panel import (
    AssumptionsVersion,
    BlockRef,
    DocumentCounts,
    DocumentRef,
    ZoneRef,
    ZoneTypicalParameters,
)

BasisReason = Literal["planned_parcel", "split", "no_planned_parcel", "not_covered", "unpublished"]
GapReason = Literal["not_in_document", "rejected", "unpublished"]
ValueScope = Literal["parcel", "block", "zone", "document"]


# --- header ---------------------------------------------------------------------------------------


class HeaderDocument(DocumentRef):
    role: Literal["governing", "basis", "amendment"] = Field(
        description="governing: the adopted document covering the parcel; basis: the planned "
        "parcel's document when it differs; amendment: an in-progress amendment of the governing "
        "document"
    )


class ParcelFlag(BaseModel):
    key: Literal["public_ownership", "restitution_or_legal_burden"]
    value: bool = Field(description="false = not flagged in the cadastral extract")
    label_en: str
    label_me: str


class ParcelAreas(BaseModel):
    cadastral_m2: float
    planned_m2: float | None = Field(
        default=None, description="Area of the primary planned urban parcel (the basis)"
    )
    linked_planned_total_m2: float | None = Field(
        default=None, description="Sum over every linked planned parcel (split parcels)"
    )
    planned_stated_m2: float | None = Field(
        default=None, description="The plan's own planned_parcel_area_m2 value when stated"
    )
    delta_m2: float | None = Field(default=None, description="planned − cadastral")
    delta_pct: float | None = Field(default=None, description="(planned − cadastral) / cadastral")
    mismatch: bool = Field(description="Planned and cadastral area differ (always surfaced)")
    note_en: str | None = None
    note_me: str | None = None


class LinkedPlannedParcel(BaseModel):
    urban_parcel_id: int
    urban_parcel_number: str
    document: DocumentRef
    urban_block: BlockRef | None = None
    area_m2: float
    overlap_m2: float
    overlap_pct: float = Field(description="Share of the cadastral parcel covered, 0–100")
    area_delta_m2: float = Field(description="planned − cadastral")
    rank: int = Field(description="1 = the calculation basis (largest overlap)")
    primary: bool


class CalculationBasisView(BaseModel):
    basis: Literal["urban", "cadastral"]
    area_m2: float = Field(description="The area every calculation uses")
    reason: BasisReason
    explanation_en: str
    explanation_me: str
    split: bool = Field(description="The cadastral parcel lies in more than one planned parcel")
    links: list[LinkedPlannedParcel] = Field(
        default_factory=list, description="From parcel_links of the current version, rank order"
    )
    links_source: Literal["parcel_links"] = "parcel_links"


class ParcelHeader(BaseModel):
    parcel_id: int = Field(description="UrbanView's Parcel ID (cadastral_parcels.id)")
    ko: str = Field(description="Cadastral municipality (KO)")
    parcel_number: str
    sub_number: str | None = None
    title: str = Field(description='"KO {ko}, {number}[/{sub}]"')
    street_address: str | None = None
    zone: ZoneRef | None = None
    urban_block: BlockRef | None = None
    documents: list[HeaderDocument] = Field(default_factory=list)
    flags: list[ParcelFlag]
    areas: ParcelAreas
    calculation_basis: CalculationBasisView


# --- group 1 --------------------------------------------------------------------------------------


class ValueSource(BaseModel):
    document_id: int
    document: str = Field(description="Document name")
    page: int = Field(description="1-based page of the PDF")
    bbox: list[float] | None = None
    bbox_space: Literal["pdf-points-bottom-left"] = "pdf-points-bottom-left"
    file_id: int | None = Field(
        default=None, description="stored_files.id of the document's PDF (null for seeded ones)"
    )
    note: str | None = None
    registry_url: str | None = None
    value_id: int = Field(description="planning_parameter_values.id")
    viewer_url: str = Field(description="GET here for a signed link to the cited page")


class Group1Field(BaseModel):
    key: str
    label_en: str
    label_me: str
    abbreviation: str | None = None
    unit: str | None = None
    value_type: Literal["text", "number"]
    status: Literal["stated", "not_stated"]
    value: float | str | None = Field(
        default=None, description="null when not stated (never a default); a stated 0 is a 0"
    )
    scope: ValueScope | None = Field(
        default=None, description="Where the value is stated: this parcel, its block, zone or doc"
    )
    source: ValueSource | None = Field(default=None, description="Always set when stated")
    reason: GapReason | None = Field(default=None, description="Why the value is null")
    reason_en: str | None = None
    reason_me: str | None = None


class ComputedInput(BaseModel):
    key: str
    value: float | None = None


class ComputedField(BaseModel):
    key: str
    label_en: str
    label_me: str
    abbreviation: str | None = None
    unit: str | None = None
    status: Literal["computed", "cannot_compute"]
    value: float | None = None
    formula: str | None = None
    inputs: list[ComputedInput]
    reason: str | None = None
    reason_en: str | None = None
    reason_me: str | None = None


class Group1(BaseModel):
    tier: Literal["free"] = "free"
    title_en: str
    title_me: str
    document: DocumentRef | None = Field(
        default=None, description="The document the parcel's values are read from"
    )
    urban_parcel_number: str | None = None
    fields: list[Group1Field] = Field(description="The 11 planning fields, dictionary order")
    computed: list[ComputedField] = Field(description="Max GFA and max coverage area")


# --- market / assumptions / group 2 -------------------------------------------------------------


class RangeValue(BaseModel):
    low: float
    expected: float
    high: float
    kind: Literal["absolute", "multiplier"] = Field(
        description="absolute: admin bounds; multiplier: expected × the row's range factors"
    )


class MarketView(BaseModel):
    tier: Literal["paid"] = "paid"
    title_en: str
    title_me: str
    zone: ZoneRef | None = None
    scope: Literal["zone", "municipality"] = Field(
        description="The zone's own row, or the municipality-wide default row"
    )
    label_en: str
    label_me: str
    unit: str
    sale_price_eur_m2: RangeValue
    source: str | None = None
    source_date: str | None = None
    effective_from: str | None = None
    version: AssumptionsVersion | None = None


class AssumptionItem(BaseModel):
    key: Literal[
        "construction_cost_eur_m2",
        "saleable_share",
        "sale_price_eur_m2",
        "land_value_eur_m2",
        "design_documentation_eur_m2",
    ]
    label_en: str
    label_me: str
    unit: str = Field(description="share = 0–1, displayed as a percentage")
    value: float | None = None
    low: float | None = None
    high: float | None = None
    source: Literal["market", "user", "product_default"] | None = None
    editable: bool = Field(description="The visitor may change it (the browser recalculates)")
    engine_edit_key: str | None = Field(
        default=None, description="Key of the engine's recalculate() edits for this item"
    )


class AssumptionsView(BaseModel):
    tier: Literal["paid"] = "paid"
    title_en: str
    title_me: str
    formula_version: str
    client_validated: Literal[False] = False
    market_version: AssumptionsVersion | None = None
    items: list[AssumptionItem]


class Group2Field(BaseModel):
    key: str
    engine_key: str = Field(description="The same figure's key in the engine's output")
    label_en: str
    label_me: str
    unit: str
    status: Literal["ok", "cannot_calculate"]
    range_kind: Literal["deterministic", "range"]
    low: float | None = None
    expected: float | None = None
    high: float | None = None
    reason: str | None = None
    reason_params: dict[str, Any] | None = None
    reason_en: str | None = None
    reason_me: str | None = None


class InputFlag(BaseModel):
    key: str = Field(description="A Group 1 field the figures depend on or assume")
    label_en: str
    label_me: str
    reason: GapReason
    used_by_formulas: bool
    affects: list[str] = Field(description="Figures that cannot be calculated without it")
    note_en: str
    note_me: str


class Group2(BaseModel):
    tier: Literal["paid"] = "paid"
    title_en: str
    title_me: str
    status: Literal["ok", "partial", "unavailable"]
    calculation_basis: Literal["urban", "cadastral"]
    basis_area_m2: float
    formula_version: str
    client_validated: Literal[False] = False
    fields: list[Group2Field] = Field(
        description="land value, design & documentation, construction, market value, saleable "
        "area, potential profit, ROI"
    )
    input_flags: list[InputFlag] = Field(default_factory=list)
    disclaimer_en: str
    disclaimer_me: str
    disclaimer_status: Literal["placeholder", "client_approved"]
    disclaimer_version: str


class EngineInfo(BaseModel):
    engine_version: str
    formula_version: str
    range_derivation: str
    deterministic: Literal[True] = True
    inputs: dict[str, Any] = Field(
        description="The shared engine's inputs that produced Group 2: calculate(inputs) in the "
        "browser gives the same figures; recalculate(inputs, edits) applies a visitor's edits"
    )
    edit_keys: dict[str, str] = Field(description="Assumption item key -> engine edit key")


# --- panels ---------------------------------------------------------------------------------------


class ParcelPanel(BaseModel):
    type: Literal["parcel"] = "parcel"
    municipality_id: str
    parcel_id: int
    version_id: int | None = Field(default=None, description="publish_versions.id (current)")
    data_version: str = Field(description='Label of the current publish version or "unpublished"')
    data_version_date: str | None = None
    formula_version: str
    client_validated: Literal[False] = False
    covered: bool = Field(description="False when no adopted document covers the parcel")
    coverage_note_en: str | None = None
    coverage_note_me: str | None = None
    header: ParcelHeader
    group1: Group1 | None = None
    market: MarketView | None = Field(default=None, description="null without a market row")
    assumptions: AssumptionsView | None = None
    group2: Group2 | None = None
    engine: EngineInfo | None = None
    centroid: LatLng
    bbox: list[float] = Field(description="[min_lng, min_lat, max_lng, max_lat]")


class ZoneDocument(DocumentRef):
    type_name: str | None = Field(default=None, description="From the municipality profile")
    covered: bool = Field(description="Adopted and its coverage is live on the map")
    file_available: bool = Field(description="The PDF is stored (source links open)")


class ZonePanelView(BaseModel):
    type: Literal["zone"] = "zone"
    municipality_id: str
    zone_id: int
    version_id: int | None = None
    data_version: str
    data_version_date: str | None = None
    title: str
    subtitle_en: str
    subtitle_me: str
    zone: ZoneRef
    summary: str | None = Field(default=None, description="General planning summary, as stored")
    summary_label_en: str
    summary_label_me: str
    documents: list[ZoneDocument] = Field(
        description="Current versions: adopted first, then in progress, then superseded"
    )
    counts: DocumentCounts
    typical_parameters: ZoneTypicalParameters | None = None
