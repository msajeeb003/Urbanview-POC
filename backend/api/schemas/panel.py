"""Information-panel payloads (``GET /v1/panel``, ``docs/specs/panel-payload.md`` section 5).

One response model per panel type, discriminated by ``type`` (``zone`` / ``document`` /
``cadastral`` / ``urban``), built from shared blocks (``Header``, ``Areas``, ``PlanningBlock``,
``MarketInputsBlock``, ``AssumptionsBlock``, ``FeasibilityBlock``).

Conventions (5.0): numbers are raw JSON numbers, never formatted strings (``_pct`` 0–100 with one
decimal, ``_share`` 0–1, ``_factor`` multipliers, areas one decimal, euros whole; the client
formats per language). Every panel carries ``type``, ``municipality_id``, ``data_version``,
``data_version_date``, ``formula_version`` and ``client_validated: false`` (formulas and
Montenegrin labels are pending client validation). Entity ids are always ``id`` inside refs.
Tier markers (``free`` / ``paid``) make the free/paid boundary explicit on screen; the POC serves
the paid blocks without entitlement checks.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from api.schemas.locate import DocumentStatus, LatLng

PanelType = Literal["zone", "document", "cadastral", "urban"]
CalculationBasis = Literal["urban", "cadastral"]
PlanningFieldStatus = Literal["stated", "not_stated", "computed", "cannot_compute"]
FeasibilityStatus = Literal["ok", "cannot_calculate"]
RangeKind = Literal["deterministic", "range"]
RateSource = Literal["market", "user"]
Tier = Literal["free", "paid"]

FORMULA_VERSION_DESCRIPTION = "Formula version the figures were computed with (poc-1)"


class AssumptionOverrides(BaseModel):
    """Optional user overrides from the query string (contract section 1). ``None`` = default:
    0.70 saleable share, market construction cost and sale price."""

    saleable_share: float | None = Field(default=None, gt=0, le=1.00)
    construction_cost_eur_m2: float | None = Field(default=None, gt=0, le=100_000)
    sale_price_eur_m2: float | None = Field(default=None, gt=0, le=100_000)


# --- refs ---------------------------------------------------------------------------------------


class DocumentRef(BaseModel):
    id: int
    name: str
    type: str | None = Field(default=None, description="Abbreviation (DUP / PUP / PGR), as-is")
    status: DocumentStatus
    status_label_en: str
    status_label_me: str
    source: str | None = None
    registry_url: str | None = Field(default=None, description="planning_documents.source_url")
    amends_document_id: int | None = None
    adopted_on: date | None = Field(default=None, description="Adoption date, when known")


class HeaderDocumentRef(DocumentRef):
    role: Literal["governing", "amendment"]


class DocumentDetail(DocumentRef):
    ingestion_dataset_version: str | None = None
    file_available: bool = Field(default=False, description="The source PDF is stored")


class ZonePlanningDocument(DocumentRef):
    """A document in the zone panel's list, with what the map can show of it."""

    covered: bool = Field(
        default=False, description="Adopted, live, current version with a coverage geometry"
    )
    file_available: bool = Field(default=False, description="The source PDF is stored")
    parcel_count: int | None = Field(
        default=None,
        description="Cadastral parcels (point on surface) in the coverage; null = not covered",
    )


class ZoneRef(BaseModel):
    id: int
    name: str


class ZoneDetail(ZoneRef):
    zone_type: str | None = Field(
        default=None, description="res | com | mix | pub | grn; null = not classified"
    )
    general_planning_summary: str | None = None


class ZoneTypicalSummary(BaseModel):
    land_use: str | None = None
    max_far: float | None = None
    max_site_coverage_pct: float | None = None
    max_height_m: float | None = None
    max_floors: int | None = None


class DocumentZone(ZoneRef):
    """A zone the document's coverage spans, with its type and typical values (if any)."""

    zone_type: str | None = None
    typical: ZoneTypicalSummary | None = Field(
        default=None, description="The zone's current parameter set, or null"
    )


class BlockRef(BaseModel):
    id: int
    block_ref: str


class Label(BaseModel):
    en: str
    me: str


# --- shared blocks (5.0 / 5.5) -------------------------------------------------------------------


class Header(BaseModel):
    ko_and_number: str | None = Field(
        default=None,
        description='"KO {ko_name}, {parcel_number}[/{sub_number}]"; null without a cadastral '
        "parcel",
    )
    zone: ZoneRef | None = None
    documents: list[HeaderDocumentRef] = Field(
        default_factory=list, description="Governing first, then its in-progress amendments"
    )
    data_version: str
    data_version_date: str | None = None


class Areas(BaseModel):
    cadastral_area_m2: float | None = None
    urban_parcel_area_m2: float | None = None
    delta_m2: float | None = None
    delta_pct: float | None = Field(
        default=None, description="(urban − cadastral) / cadastral × 100; negative = smaller"
    )
    overlap_m2: float | None = None
    share_of_cadastral_pct: float | None = None
    share_of_urban_pct: float | None = None
    planned_area_stated_m2: float | None = Field(
        default=None, description="The plan's own planned_parcel_area_m2 value when stated"
    )
    stated_vs_geometry_delta_pct: float | None = Field(
        default=None, description="(stated − basis geometry area) / basis geometry area × 100"
    )
    calculation_basis: CalculationBasis
    basis_area_m2: float
    basis_reason_code: Literal["urban_covers_cadastral", "no_urban_parcel", "no_cadastral_parcel"]
    basis_reason_params: dict[str, Any]
    basis_reason_en: str
    basis_reason_me: str


class Source(BaseModel):
    document_id: int
    document_name: str
    page: int
    bbox: list[float] | None = None
    bbox_space: Literal["pdf-points-bottom-left"] = "pdf-points-bottom-left"
    note: str | None = None
    registry_url: str | None = None
    value_id: int | None = Field(default=None, description="planning_parameter_values.id")
    viewer_url: str | None = Field(
        default=None,
        description="API path answering with a short-lived signed URL to the cited page: "
        "GET /v1/source/value/{value_id}",
    )


class PlanningField(BaseModel):
    key: str
    label_en: str
    label_me: str
    abbreviation: str | None = None
    unit: str | None = Field(default=None, description="Row override, else dictionary unit")
    value_type: Literal["text", "number"]
    status: PlanningFieldStatus
    value: float | str | None = Field(
        default=None, description="null when not_stated / cannot_compute; a stated 0 is a real 0"
    )
    scope: Literal["parcel", "document"] | None = None
    fallback: bool = Field(
        default=False, description="A document-level value used for a planned urban parcel"
    )
    source: Source | None = Field(default=None, description="Non-null whenever status = stated")
    formula: str | None = None
    derived_from: list[str] | None = None
    reason_code: str | None = None
    reason_en: str | None = None
    reason_me: str | None = None


class PlanningBlock(BaseModel):
    tier: Literal["free"] = "free"
    calculation_basis: CalculationBasis
    basis_area_m2: float
    fields: list[PlanningField] = Field(description="Dictionary order; all 13 always present")
    not_stated_label: Label


class RateRange(BaseModel):
    expected: float
    low: float
    high: float
    kind: Literal["absolute", "multiplier"] = Field(
        description="absolute: admin bounds on the row; multiplier: expected × range factors"
    )


class AssumptionsVersion(BaseModel):
    """Which financial_assumptions row (version) produced the figures."""

    id: int
    version: int
    zone_id: int | None = Field(
        default=None, description="The zone of the row (the panel reads zone rows only)"
    )
    effective_from: str | None = Field(default=None, description="created_at of that version")


class MarketRanges(BaseModel):
    land_rate: RateRange
    build_rate: RateRange
    design_rate: RateRange
    sale_rate: RateRange


class MarketInputsBlock(BaseModel):
    tier: Literal["paid"] = "paid"
    available: bool
    reason_code: str | None = None
    reason_en: str | None = None
    reason_me: str | None = None
    zone: ZoneRef | None = None
    land_rate_eur_m2: float | None = None
    build_rate_eur_m2: float | None = None
    design_rate_eur_m2: float | None = None
    sale_rate_eur_m2: float | None = None
    range_low_factor: float | None = None
    range_high_factor: float | None = None
    source: str | None = None
    source_date: str | None = None
    effective_from: str | None = Field(
        default=None, description="created_at of the current market row (ISO 8601)"
    )
    version: AssumptionsVersion | None = None
    ranges: MarketRanges | None = Field(
        default=None, description="Per-rate low / expected / high as the engine used them"
    )


class OverrideFlags(BaseModel):
    saleable_share: bool
    construction_cost_eur_m2: bool
    sale_price_eur_m2: bool


class RateSources(BaseModel):
    construction_cost_eur_m2: RateSource | None = None
    sale_price_eur_m2: RateSource | None = None


class AssumptionsBlock(BaseModel):
    tier: Literal["paid"] = "paid"
    saleable_share: float
    construction_cost_eur_m2: float | None = None
    sale_price_eur_m2: float | None = None
    design_documentation_eur_m2: float | None = None
    land_rate_eur_m2: float | None = None
    range_low_factor: float | None = None
    range_high_factor: float | None = None
    overrides: OverrideFlags
    sources: RateSources
    market_source: str | None = None
    market_source_date: str | None = None
    market_version: AssumptionsVersion | None = None
    formula_version: str
    data_version: str
    data_version_date: str | None = None
    client_validated: Literal[False] = False


class FeasibilityField(BaseModel):
    key: str
    label_en: str
    label_me: str
    unit: str
    range_kind: RangeKind
    status: FeasibilityStatus
    reason_code: str | None = None
    reason_params: dict[str, Any] | None = None
    reason_en: str | None = None
    reason_me: str | None = None
    low: float | None = None
    expected: float | None = None
    high: float | None = None


class FeasibilityBlock(BaseModel):
    tier: Literal["paid"] = "paid"
    calculation_basis: CalculationBasis
    basis_area_m2: float
    formula_version: str
    fields: list[FeasibilityField] = Field(
        description="max_gfa_m2, max_coverage_area_m2, saleable_area_m2, construction_cost_eur, "
        "revenue_eur, profit_eur, roi_pct"
    )
    cost_rows: list[FeasibilityField] = Field(
        description="land_value_eur, design_documentation_eur, construction_cost_eur, "
        "total_cost_eur"
    )
    disclaimer_en: str
    disclaimer_me: str
    disclaimer_status: Literal["placeholder", "client_approved"]
    disclaimer_version: str


# --- panels ---------------------------------------------------------------------------------------


class PanelBase(BaseModel):
    type: PanelType
    municipality_id: str
    data_version: str = Field(description='Label of the current publish version, or "unpublished"')
    data_version_date: str | None = Field(
        default=None, description="ISO date of the current publish version (UTC)"
    )
    formula_version: str = Field(description=FORMULA_VERSION_DESCRIPTION)
    client_validated: Literal[False] = False


class ZoneHeader(BaseModel):
    title: str
    subtitle_en: str
    subtitle_me: str


class DocumentCounts(BaseModel):
    documents: int
    adopted: int
    in_progress: int
    superseded: int
    covered: int = Field(default=0, description="Documents that resolve locations on the map")


class ZoneTypicalSource(BaseModel):
    document_id: int
    document_name: str | None = None
    page: int | None = None
    note: str | None = None
    registry_url: str | None = None


class ZoneTypicalParameters(BaseModel):
    """The zone's typical planning values (staff-maintained, versioned): fallback figures for
    the zone panel; a parcel's own document values always take precedence."""

    id: int
    version: int
    land_use: str | None = None
    max_far: float | None = None
    max_site_coverage_pct: float | None = None
    max_height_m: float | None = None
    max_floors: int | None = None
    notes: str | None = None
    source: ZoneTypicalSource | None = None
    verified_on: str | None = None
    verified_by: str | None = None
    note_en: str
    note_me: str


class ZonePanel(PanelBase):
    type: Literal["zone"] = "zone"
    zone: ZoneDetail
    header: ZoneHeader
    planning_documents: list[ZonePlanningDocument] = Field(
        description="Current versions: adopted first, then in progress, then superseded; name asc"
    )
    counts: DocumentCounts
    typical_parameters: ZoneTypicalParameters | None = Field(
        default=None, description="The zone's current parameter set, or null"
    )


class CoverageCounts(BaseModel):
    cadastral_parcels: int = Field(description="ST_PointOnSurface(geom) within the coverage")
    urban_parcels: int = Field(description="urban_parcels.document_id = id")


class DocumentPanel(PanelBase):
    type: Literal["document"] = "document"
    document: DocumentDetail
    amendments_in_progress: list[DocumentRef] = Field(
        description="amends_document_id = id AND status = in_progress, name asc"
    )
    zones: list[DocumentZone] = Field(
        description="zone_id plus zones whose geometry intersects the coverage, name asc"
    )
    coverage_counts: CoverageCounts
    general_planning_summary: str | None = None


class Flags(BaseModel):
    public_ownership: bool
    restitution_or_legal_burden: bool
    note_en: str
    note_me: str


class UrbanLink(BaseModel):
    id: int
    urban_parcel_number: str
    area_m2: float
    overlap_m2: float
    share_of_cadastral_pct: float
    share_of_linked_pct: float = Field(description="Share of all linked overlap; sums to 100")
    delta_pct: float = Field(description="(area_m2 − cadastral_area) / cadastral_area × 100")
    document: DocumentRef
    urban_block: BlockRef | None = None


class CadastralIdentification(BaseModel):
    parcel_id: int = Field(description="UrbanView's own numeric Parcel ID (cadastral_parcels.id)")
    parcel_number: str
    sub_number: str | None = None
    ko_name: str
    street_address: str | None = None
    urban_block: BlockRef | None = Field(
        default=None,
        description="Primary urban parcel's block, else the block containing the parcel's "
        "point on surface",
    )
    cadastral_area_m2: float
    governing_document: DocumentRef | None = None
    zone: ZoneRef | None = None


class PanelEngine(BaseModel):
    """The shared engine's exact inputs behind ``feasibility``: ``calculate(inputs)`` in the browser
    gives the same figures, ``recalculate(inputs, edits)`` applies a visitor's edits (keys of
    ``edit_keys``) without a server round trip."""

    engine_version: str
    formula_version: str
    range_derivation: str
    deterministic: Literal[True] = True
    inputs: dict[str, Any]
    edit_keys: dict[str, str] = Field(description="Assumption key -> engine edit key")
    field_keys: dict[str, str] = Field(
        description="Feasibility figure key (fields, cost_rows) -> engine result field key"
    )


class CadastralPanel(PanelBase):
    type: Literal["cadastral"] = "cadastral"
    identification: CadastralIdentification
    header: Header
    flags: Flags
    urban_parcel_defined: bool
    urban_parcel: UrbanLink | None = Field(
        default=None, description="Primary: largest overlap, then smallest planned area, lowest id"
    )
    urban_parcels: list[UrbanLink] = Field(default_factory=list)
    split: bool = Field(description="More than one linked planned urban parcel")
    areas: Areas
    calculation_basis: CalculationBasis
    basis_area_m2: float
    planning: PlanningBlock | None = None
    market_inputs: MarketInputsBlock | None = None
    assumptions: AssumptionsBlock | None = None
    feasibility: FeasibilityBlock | None = None
    engine: PanelEngine | None = None
    covered: bool = Field(description="False when no adopted document governs the parcel")
    coverage_note_en: str | None = None
    coverage_note_me: str | None = None
    centroid: LatLng
    geometry: dict[str, Any] = Field(description="GeoJSON geometry, EPSG:4326")


class CadastralLink(BaseModel):
    parcel_id: int
    parcel_number: str
    sub_number: str | None = None
    ko_name: str
    street_address: str | None = None
    area_m2: float
    overlap_m2: float
    share_of_urban_pct: float
    share_of_cadastral_pct: float


class UrbanIdentification(BaseModel):
    urban_parcel_id: int
    urban_parcel_number: str
    urban_block: BlockRef | None = None
    governing_document: DocumentRef = Field(description="The parcel's document")
    zone: ZoneRef | None = None
    cadastral_parcel: CadastralLink | None = Field(
        default=None, description="Primary = largest overlap"
    )
    linked_cadastral_parcels: list[CadastralLink] = Field(default_factory=list)


class UrbanPanel(PanelBase):
    type: Literal["urban"] = "urban"
    identification: UrbanIdentification
    header: Header
    areas: Areas
    calculation_basis: Literal["urban"] = "urban"
    basis_area_m2: float
    covered: bool = Field(description="False when the parcel's document is not adopted")
    coverage_note_en: str | None = None
    coverage_note_me: str | None = None
    planning: PlanningBlock | None = None
    market_inputs: MarketInputsBlock | None = None
    assumptions: AssumptionsBlock | None = None
    feasibility: FeasibilityBlock | None = None
    engine: PanelEngine | None = None
    centroid: LatLng
    geometry: dict[str, Any] = Field(description="GeoJSON geometry, EPSG:4326")


PanelResponse = Annotated[
    ZonePanel | DocumentPanel | CadastralPanel | UrbanPanel, Field(discriminator="type")
]
