"""Display-shaped panels: ``GET /v1/parcels/{id}/panel`` and ``GET /v1/zones/{id}/panel``
(contract ``docs/specs/panel-payload.md`` section 13).

One statement per panel (``api.services.parcel_panel_sql``), then pure assembly (``build_*``,
unit-tested on canned rows) and the per-version response cache (``api.services.panel_cache``).

Parcel panel rules:
- the calculation basis comes from ``parcel_links`` of the current version: the rank-1 planned
  urban parcel (largest overlap) when the parcel is covered and linked (``planned_parcel``, or
  ``split`` when several planned parcels share it), else the cadastral area
  (``no_planned_parcel``); ``not_covered`` / ``unpublished`` otherwise; the explanation says why;
- Group 1 lists the 11 stored planning fields: a value resolves parcel → block → zone → document
  and always carries its source (document, page, bbox, file id, viewer link); a missing value is
  null with ``not_in_document`` or ``rejected`` (a gap the publish job recorded), never a default;
  the two computed fields (max GFA, max coverage area) follow with their inputs;
- Group 2 is the shared engine's output for exactly the inputs in ``engine.inputs`` (the browser
  runs the same engine on them), and ``input_flags`` name the Group 1 inputs that are missing;
- an uncovered parcel is 200 with ``covered: false`` and null groups (never an error).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.locate import LatLng
from api.schemas.panel import BlockRef, DocumentCounts, DocumentRef, ZoneRef
from api.schemas.parcel_panel import (
    AssumptionItem,
    AssumptionsView,
    CalculationBasisView,
    ComputedField,
    ComputedInput,
    EngineInfo,
    Group1,
    Group1Field,
    Group2,
    Group2Field,
    HeaderDocument,
    InputFlag,
    LinkedPlannedParcel,
    MarketView,
    ParcelAreas,
    ParcelFlag,
    ParcelHeader,
    ParcelPanel,
    RangeValue,
    ValueSource,
    ZoneDocument,
    ZonePanelView,
)
from api.services import panel_text
from api.services.panel import (
    UNPUBLISHED,
    _as_json,
    _assumptions_version,
    _document_ref,
    _iso,
    _ko_and_number,
    _market_inputs,
    _market_reason,
    _pct,
    _r1,
    _rate_range,
    _typical_parameters,
)
from api.services.panel_cache import PanelCache, PanelView
from api.services.parcel_panel_sql import PARCEL_PANEL_SQL, ZONE_PANEL_SQL
from core.engine import (
    DEFAULT_SALEABLE_SHARE,
    FORMULA_VERSION,
    Assumptions,
    FeasibilityResult,
    compute_feasibility,
)
from core.engine.feasibility import EDIT_KEYS, SHARED_KEY
from core.engine.shared import ENGINE_VERSION, RANGE_DERIVATION, select_calculation_basis
from core.errors import NotFoundError
from core.municipality import MunicipalityProfile

log = logging.getLogger("urbanview.panel")

# Group 2 in display order (product rule "Panel Group 2").
GROUP2_KEYS: tuple[str, ...] = (
    "land_value_eur",
    "design_documentation_eur",
    "construction_cost_eur",
    "revenue_eur",
    "saleable_area_m2",
    "profit_eur",
    "roi_pct",
)
# What the two computed planning fields are derived from.
COMPUTED_INPUTS: dict[str, tuple[str, str]] = {
    "max_gfa_m2": ("max_far", "basis_area_m2"),
    "max_coverage_area_m2": ("max_site_coverage_pct", "basis_area_m2"),
}
# Group 1 inputs the figures depend on (used by the formulas) or silently assume.
FLAGGED_INPUTS: dict[str, tuple[bool, tuple[str, ...]]] = {
    "max_far": (
        True,
        (
            "max_gfa_m2",
            "saleable_area_m2",
            "construction_cost_eur",
            "design_documentation_eur",
            "revenue_eur",
            "profit_eur",
            "roi_pct",
        ),
    ),
    "max_site_coverage_pct": (True, ("max_coverage_area_m2",)),
    "max_height_m": (False, ()),
    "max_floors": (False, ()),
}


def _text(field: Group1Field | None) -> str | None:
    if field is None or field.value is None:
        return None
    return str(field.value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


# --- group 1 --------------------------------------------------------------------------------------


def _value_source(row: Mapping[str, Any]) -> ValueSource:
    value_id = int(row["value_id"])
    return ValueSource(
        document_id=row["document_id"],
        document=row["document_name"],
        page=row["source_page"],
        bbox=row.get("source_bbox"),
        file_id=row.get("file_id"),
        note=row.get("source_note"),
        registry_url=row.get("registry_url"),
        value_id=value_id,
        viewer_url=f"/v1/source/value/{value_id}",
    )


def group1_fields(
    fields_raw: Sequence[Mapping[str, Any]],
    values_raw: Sequence[Mapping[str, Any]],
    gaps_raw: Sequence[Mapping[str, Any]],
    *,
    published: bool,
) -> list[Group1Field]:
    """The 11 stored planning fields in dictionary order. ``values_raw`` arrives in precedence
    order (parcel, block, zone, document), so the first row per field wins."""
    values: dict[str, Mapping[str, Any]] = {}
    for row in values_raw:
        values.setdefault(row["field_key"], row)
    rejected = {row["field_key"] for row in gaps_raw if row.get("reason") == "rejected"}
    fields: list[Group1Field] = []
    for spec in fields_raw:
        if spec.get("computed"):
            continue
        key = spec["key"]
        base: dict[str, Any] = {
            "key": key,
            "label_en": spec["label_en"],
            "label_me": spec["label_me"],
            "abbreviation": spec.get("abbreviation"),
            "value_type": spec["value_type"],
        }
        row = values.get(key)
        if row is not None:
            value = row["value_text"] if row.get("value_text") is not None else row["value_number"]
            fields.append(
                Group1Field(
                    **base,
                    unit=row.get("unit") or spec.get("unit"),
                    status="stated",
                    value=value,
                    scope=row["scope"],
                    source=_value_source(row),
                )
            )
            continue
        if not published:
            reason = "unpublished"
        elif key in rejected:
            reason = "rejected"
        else:
            reason = "not_in_document"
        label = panel_text.field_gap_reason(reason)
        fields.append(
            Group1Field(
                **base,
                unit=spec.get("unit"),
                status="not_stated",
                reason=reason,
                reason_en=label.en,
                reason_me=label.me,
            )
        )
    return fields


def computed_fields(
    fields_raw: Sequence[Mapping[str, Any]],
    result: FeasibilityResult,
    *,
    inputs: Mapping[str, float | None],
) -> list[ComputedField]:
    out: list[ComputedField] = []
    for spec in fields_raw:
        if not spec.get("computed"):
            continue
        key = spec["key"]
        figure = result.get(key)
        reason = panel_text.reason_text(figure.reason_code, figure.reason_params)
        out.append(
            ComputedField(
                key=key,
                label_en=spec["label_en"],
                label_me=spec["label_me"],
                abbreviation=spec.get("abbreviation"),
                unit=spec.get("unit"),
                status="computed" if figure.status == "ok" else "cannot_compute",
                value=figure.expected,
                formula=spec.get("formula"),
                inputs=[
                    ComputedInput(key=name, value=inputs.get(name))
                    for name in COMPUTED_INPUTS.get(key, ())
                ],
                reason=figure.reason_code,
                reason_en=reason.en if reason else None,
                reason_me=reason.me if reason else None,
            )
        )
    return out


# --- engine, group 2, market, assumptions ---------------------------------------------------------


def run_engine(
    *,
    basis: str,
    basis_area_m2: float,
    far: float | None,
    coverage: float | None,
    market_raw: Mapping[str, Any] | None,
    zone: ZoneRef | None,
    context: Mapping[str, Any] | None = None,
) -> FeasibilityResult:
    """The shared engine with the product defaults (0.70 saleable share, market rates);
    ``context`` (both areas, height, floors, land use) travels in ``engine.inputs``."""
    code, params = _market_reason(zone)
    return compute_feasibility(
        basis_area_m2,
        far,
        coverage,
        _market_inputs(market_raw),
        Assumptions(saleable_share=DEFAULT_SALEABLE_SHARE),
        market_reason_code=code,
        market_reason_params=params,
        calculation_basis=basis,
        planning_context=context,
    )


def input_flags(fields: Sequence[Group1Field]) -> list[InputFlag]:
    by_key = {f.key: f for f in fields}
    flags: list[InputFlag] = []
    for key, (used, affects) in FLAGGED_INPUTS.items():
        field = by_key.get(key)
        if field is None or field.status == "stated" or field.reason is None:
            continue
        note = panel_text.INPUT_FLAG_NOTES[key]
        flags.append(
            InputFlag(
                key=key,
                label_en=field.label_en,
                label_me=field.label_me,
                reason=field.reason,
                used_by_formulas=used,
                affects=list(affects),
                note_en=note.en,
                note_me=note.me,
            )
        )
    return flags


def group2_block(
    result: FeasibilityResult,
    *,
    basis: str,
    basis_area_m2: float,
    fields: Sequence[Group1Field],
) -> Group2:
    items: list[Group2Field] = []
    for key in GROUP2_KEYS:
        figure = result.get(key)
        label = panel_text.feasibility_label(key)
        reason = panel_text.reason_text(figure.reason_code, figure.reason_params)
        items.append(
            Group2Field(
                key=key,
                engine_key=SHARED_KEY[key],
                label_en=label.en,
                label_me=label.me,
                unit=label.unit,
                status=figure.status,
                range_kind=figure.range_kind,
                low=figure.low,
                expected=figure.expected,
                high=figure.high,
                reason=figure.reason_code,
                reason_params=figure.reason_params,
                reason_en=reason.en if reason else None,
                reason_me=reason.me if reason else None,
            )
        )
    ok = sum(1 for item in items if item.status == "ok")
    status = "ok" if ok == len(items) else ("partial" if ok else "unavailable")
    title = panel_text.PANEL_SECTIONS["group2"]
    return Group2(
        title_en=title.en,
        title_me=title.me,
        status=status,
        calculation_basis=basis,  # type: ignore[arg-type]
        basis_area_m2=basis_area_m2,
        formula_version=result.formula_version,
        fields=items,
        input_flags=input_flags(fields),
        disclaimer_en=panel_text.DISCLAIMER.en,
        disclaimer_me=panel_text.DISCLAIMER.me,
        disclaimer_status=panel_text.DISCLAIMER_STATUS,
        disclaimer_version=panel_text.DISCLAIMER_VERSION,
    )


def market_view(market_raw: Mapping[str, Any] | None, zone: ZoneRef | None) -> MarketView | None:
    if market_raw is None:
        return None
    sale = _rate_range(market_raw, "sale")
    title = panel_text.PANEL_SECTIONS["market"]
    label = panel_text.MARKET_PARAMETER_LABELS["sale_rate_eur_m2"]
    return MarketView(
        title_en=title.en,
        title_me=title.me,
        zone=zone,
        scope="zone" if market_raw.get("zone_id") is not None else "municipality",
        label_en=label.en,
        label_me=label.me,
        unit="€/m²",
        sale_price_eur_m2=RangeValue(
            low=sale.low, expected=sale.expected, high=sale.high, kind=sale.kind
        ),
        source=market_raw.get("source"),
        source_date=_iso(market_raw.get("source_date")),
        effective_from=_iso(market_raw.get("effective_from")),
        version=_assumptions_version(market_raw),
    )


def assumptions_view(
    result: FeasibilityResult, market_raw: Mapping[str, Any] | None
) -> AssumptionsView:
    used = result.assumptions_used

    def ranged(prefix: str) -> tuple[float | None, float | None]:
        if market_raw is None:
            return None, None
        rate = _rate_range(market_raw, prefix)
        return rate.low, rate.high

    def item(
        key: str,
        value: float | None,
        bounds: tuple[float | None, float | None],
        source: str | None,
        editable: bool,
    ) -> AssumptionItem:
        label = panel_text.ASSUMPTION_LABELS[key]
        return AssumptionItem(
            key=key,  # type: ignore[arg-type]
            label_en=label.en,
            label_me=label.me,
            unit=label.unit,
            value=value,
            low=bounds[0],
            high=bounds[1],
            source=source,  # type: ignore[arg-type]
            editable=editable,
            engine_edit_key=EDIT_KEYS.get(key),
        )

    market_source = "market" if market_raw is not None else None
    title = panel_text.PANEL_SECTIONS["assumptions"]
    return AssumptionsView(
        title_en=title.en,
        title_me=title.me,
        formula_version=result.formula_version,
        market_version=_assumptions_version(market_raw),
        items=[
            item(
                "construction_cost_eur_m2",
                used.construction_cost_eur_m2,
                ranged("build"),
                used.sources.construction_cost_eur_m2,
                True,
            ),
            item("saleable_share", used.saleable_share, (None, None), "product_default", True),
            item(
                "sale_price_eur_m2",
                used.sale_price_eur_m2,
                ranged("sale"),
                used.sources.sale_price_eur_m2,
                True,
            ),
            item("land_value_eur_m2", used.land_rate_eur_m2, ranged("land"), market_source, False),
            item(
                "design_documentation_eur_m2",
                used.design_rate_eur_m2,
                ranged("design"),
                market_source,
                False,
            ),
        ],
    )


def engine_info(result: FeasibilityResult) -> EngineInfo:
    return EngineInfo(
        engine_version=ENGINE_VERSION,
        formula_version=result.formula_version,
        range_derivation=RANGE_DERIVATION,
        inputs=dict(result.engine_inputs or {}),
        edit_keys=dict(EDIT_KEYS),
    )


# --- header ---------------------------------------------------------------------------------------


def linked_parcels(links_raw: Sequence[Mapping[str, Any]]) -> list[LinkedPlannedParcel]:
    """Rank order from the statement; the first one is the calculation basis."""
    links: list[LinkedPlannedParcel] = []
    for index, raw in enumerate(links_raw):
        links.append(
            LinkedPlannedParcel(
                urban_parcel_id=raw["id"],
                urban_parcel_number=raw["urban_parcel_number"],
                document=_document_ref(raw["document"]),
                urban_block=BlockRef(**raw["urban_block"]) if raw.get("urban_block") else None,
                area_m2=_r1(raw["area_m2"]) or 0.0,
                overlap_m2=_r1(raw["overlap_m2"]) or 0.0,
                overlap_pct=_r1(float(raw["overlap_fraction"]) * 100) or 0.0,
                area_delta_m2=_r1(raw["area_delta_m2"]) or 0.0,
                rank=int(raw["rank"]),
                primary=index == 0,
            )
        )
    return links


def basis_view(
    *,
    covered: bool,
    published: bool,
    links: list[LinkedPlannedParcel],
    cadastral_area_m2: float,
    document_name: str | None,
) -> CalculationBasisView:
    cadastral = _r1(cadastral_area_m2) or 0.0
    params: dict[str, Any] = {"cadastral_m2": cadastral}
    if not covered:
        basis, area, reason = "cadastral", cadastral, "not_covered"
    elif not published:
        basis, area, reason = "cadastral", cadastral, "unpublished"
    elif links:
        primary = links[0]
        # the shared planned-first rule (engine: select_calculation_basis)
        choice = select_calculation_basis(primary.area_m2, cadastral)
        basis, area = choice["calculation_basis"], choice["plot_area"]
        reason = "split" if len(links) > 1 else "planned_parcel"
        params.update(
            urban_parcel_number=primary.urban_parcel_number,
            planned_m2=primary.area_m2,
            overlap_pct=primary.overlap_pct,
            count=len(links),
            numbers=", ".join(link.urban_parcel_number for link in links),
        )
    else:
        basis, area, reason = "cadastral", cadastral, "no_planned_parcel"
        params["document_name"] = document_name or ""
    explanation = panel_text.parcel_basis_text(reason, params)
    return CalculationBasisView(
        basis=basis,  # type: ignore[arg-type]
        area_m2=area,
        reason=reason,  # type: ignore[arg-type]
        explanation_en=explanation.en,
        explanation_me=explanation.me,
        split=len(links) > 1,
        links=links,
    )


def areas_view(
    cadastral_area_m2: float, links: list[LinkedPlannedParcel], stated_m2: float | None
) -> ParcelAreas:
    cadastral = _r1(cadastral_area_m2) or 0.0
    planned = links[0].area_m2 if links else None
    total = _r1(sum(link.area_m2 for link in links)) if len(links) > 1 else None
    delta_m2 = delta_pct = None
    note = None
    if planned is not None:
        delta_m2 = _r1(planned - cadastral)
        delta_pct = _pct(planned - cadastral, cadastral)
        if delta_m2 is not None and abs(delta_m2) >= 0.1:
            note = panel_text.area_mismatch_text(
                planned_m2=planned, cadastral_m2=cadastral, delta_m2=delta_m2, delta_pct=delta_pct
            )
    return ParcelAreas(
        cadastral_m2=cadastral,
        planned_m2=planned,
        linked_planned_total_m2=total,
        planned_stated_m2=stated_m2,
        delta_m2=delta_m2,
        delta_pct=delta_pct,
        mismatch=note is not None,
        note_en=note.en if note else None,
        note_me=note.me if note else None,
    )


def _header_documents(
    governing: DocumentRef | None,
    basis_document: DocumentRef | None,
    amendments: list[DocumentRef],
) -> list[HeaderDocument]:
    documents: list[HeaderDocument] = []
    if governing is not None:
        documents.append(HeaderDocument(**governing.model_dump(), role="governing"))
    if basis_document is not None and (governing is None or basis_document.id != governing.id):
        documents.append(HeaderDocument(**basis_document.model_dump(), role="basis"))
    documents.extend(HeaderDocument(**a.model_dump(), role="amendment") for a in amendments)
    return documents


def _flags(cad: Mapping[str, Any]) -> list[ParcelFlag]:
    flags: list[ParcelFlag] = []
    for key in ("public_ownership", "restitution_or_legal_burden"):
        label = panel_text.PARCEL_FLAG_LABELS[key]
        flags.append(
            ParcelFlag(
                key=key,  # type: ignore[arg-type]
                value=bool(cad.get(key)),
                label_en=label.en,
                label_me=label.me,
            )
        )
    return flags


# --- assembly -------------------------------------------------------------------------------------


def build_parcel_panel(row: Mapping[str, Any], profile: MunicipalityProfile) -> ParcelPanel:
    cad = _as_json(row["cadastral"])
    version_id = row.get("version_id")
    published = version_id is not None
    data_version = row.get("data_version") or UNPUBLISHED
    data_version_date = _iso(row.get("data_version_date"))

    governing_raw = _as_json(row["governing_document"])
    governing = _document_ref(governing_raw) if governing_raw else None
    covered = governing is not None
    basis_raw = _as_json(row["basis_document"])
    basis_document = _document_ref(basis_raw) if basis_raw and covered else None
    zone_raw = _as_json(row["zone"])
    zone = ZoneRef(**zone_raw) if zone_raw else None
    block_raw = _as_json(row["urban_block"])
    amendments = [_document_ref(a) for a in _as_json(row["amendments"]) or []]

    cadastral_area = float(cad["area_m2"])
    links = linked_parcels(_as_json(row["links"]) or [])
    basis = basis_view(
        covered=covered,
        published=published,
        links=links,
        cadastral_area_m2=cadastral_area,
        document_name=basis_document.name if basis_document else None,
    )

    group1 = market = assumptions = group2 = engine = None
    stated_area = None
    if covered:
        fields_raw = _as_json(row["fields"]) or []
        fields = group1_fields(
            fields_raw,
            _as_json(row["values"]) or [],
            _as_json(row["gaps"]) or [],
            published=published,
        )
        by_key = {f.key: f for f in fields}
        far = _number(by_key["max_far"].value) if "max_far" in by_key else None
        coverage = (
            _number(by_key["max_site_coverage_pct"].value)
            if "max_site_coverage_pct" in by_key
            else None
        )
        if "planned_parcel_area_m2" in by_key:
            stated_area = _number(by_key["planned_parcel_area_m2"].value)
        market_raw = _as_json(row["market"])
        result = run_engine(
            basis=basis.basis,
            basis_area_m2=basis.area_m2,
            far=far,
            coverage=coverage,
            market_raw=market_raw,
            zone=zone,
            context={
                "planned_area": links[0].area_m2 if links and basis.basis == "urban" else None,
                "cadastral_area": _r1(cadastral_area),
                "max_height_m": _number(by_key["max_height_m"].value)
                if "max_height_m" in by_key
                else None,
                "max_floors": _text(by_key.get("max_floors")),
                "land_use": _text(by_key.get("land_use")),
            },
        )
        title = panel_text.PANEL_SECTIONS["group1"]
        group1 = Group1(
            title_en=title.en,
            title_me=title.me,
            document=basis_document,
            urban_parcel_number=links[0].urban_parcel_number
            if links and basis.basis == "urban"
            else None,
            fields=fields,
            computed=computed_fields(
                fields_raw,
                result,
                inputs={
                    "max_far": far,
                    "max_site_coverage_pct": coverage,
                    "basis_area_m2": basis.area_m2,
                },
            ),
        )
        market = market_view(market_raw, zone)
        assumptions = assumptions_view(result, market_raw)
        group2 = group2_block(result, basis=basis.basis, basis_area_m2=basis.area_m2, fields=fields)
        engine = engine_info(result)

    note = None if covered else panel_text.CADASTRAL_NOT_COVERED_NOTE
    header = ParcelHeader(
        parcel_id=cad["id"],
        ko=cad["ko_name"],
        parcel_number=cad["parcel_number"],
        sub_number=cad["sub_number"],
        title=_ko_and_number(cad["ko_name"], cad["parcel_number"], cad["sub_number"]),
        street_address=cad["street_address"],
        zone=zone,
        urban_block=BlockRef(**block_raw) if block_raw else None,
        documents=_header_documents(governing, basis_document, amendments),
        flags=_flags(cad),
        areas=areas_view(cadastral_area, links if basis.basis == "urban" else [], stated_area),
        calculation_basis=basis,
    )
    return ParcelPanel(
        municipality_id=profile.id,
        parcel_id=cad["id"],
        version_id=int(version_id) if version_id is not None else None,
        data_version=data_version,
        data_version_date=data_version_date,
        formula_version=FORMULA_VERSION,
        covered=covered,
        coverage_note_en=note.en if note else None,
        coverage_note_me=note.me if note else None,
        header=header,
        group1=group1,
        market=market,
        assumptions=assumptions,
        group2=group2,
        engine=engine,
        centroid=LatLng(**cad["centroid"]),
        bbox=[float(x) for x in cad["bbox"]],
    )


def build_zone_panel(row: Mapping[str, Any], profile: MunicipalityProfile) -> ZonePanelView:
    zone_raw = _as_json(row["zone"])
    version_id = row.get("version_id")
    documents: list[ZoneDocument] = []
    for raw in _as_json(row["documents"]) or []:
        raw = dict(raw)
        file_available = bool(raw.pop("file_available", False))
        coverage_live = bool(raw.get("coverage_live"))
        ref = _document_ref(raw)
        documents.append(
            ZoneDocument(
                **ref.model_dump(),
                type_name=profile.terminology.document_types.get(ref.type or ""),
                covered=ref.status == "adopted" and coverage_live,
                file_available=file_available,
            )
        )
    counts = _as_json(row["counts"]) or {}
    subtitle = panel_text.ZONE_SUBTITLE
    summary_label = panel_text.ZONE_SUMMARY_LABEL
    return ZonePanelView(
        municipality_id=profile.id,
        zone_id=zone_raw["id"],
        version_id=int(version_id) if version_id is not None else None,
        data_version=row.get("data_version") or UNPUBLISHED,
        data_version_date=_iso(row.get("data_version_date")),
        title=zone_raw["name"],
        subtitle_en=subtitle.en,
        subtitle_me=subtitle.me,
        zone=ZoneRef(id=zone_raw["id"], name=zone_raw["name"]),
        summary=zone_raw.get("general_planning_summary"),
        summary_label_en=summary_label.en,
        summary_label_me=summary_label.me,
        documents=documents,
        counts=DocumentCounts(
            documents=int(counts.get("documents") or 0),
            adopted=int(counts.get("adopted") or 0),
            in_progress=int(counts.get("in_progress") or 0),
            superseded=int(counts.get("superseded") or 0),
        ),
        typical_parameters=_typical_parameters(_as_json(row["typical_parameters"])),
    )


# --- service --------------------------------------------------------------------------------------


class ParcelPanelService:
    """One statement per panel, one stamp statement per request, a Redis entry per version."""

    def __init__(
        self,
        profile: MunicipalityProfile,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cache: PanelCache,
    ) -> None:
        self.profile = profile
        self.session_factory = session_factory
        self.cache = cache

    async def parcel(self, parcel_id: int, if_none_match: str | None = None) -> PanelView:
        return await self.cache.serve(
            "parcel", parcel_id, lambda: self.parcel_panel(parcel_id), if_none_match
        )

    async def zone(self, zone_id: int, if_none_match: str | None = None) -> PanelView:
        return await self.cache.serve(
            "zone", zone_id, lambda: self.zone_panel(zone_id), if_none_match
        )

    async def parcel_panel(self, parcel_id: int) -> ParcelPanel:
        row = await self._execute(PARCEL_PANEL_SQL, parcel_id)
        if _as_json(row["cadastral"]) is None:
            raise NotFoundError(
                f"No cadastral parcel with id {parcel_id} in municipality {self.profile.id}",
                details={"type": "parcel", "id": parcel_id},
            )
        return build_parcel_panel(row, self.profile)

    async def zone_panel(self, zone_id: int) -> ZonePanelView:
        row = await self._execute(ZONE_PANEL_SQL, zone_id)
        if _as_json(row["zone"]) is None:
            raise NotFoundError(
                f"No zone with id {zone_id} in municipality {self.profile.id}",
                details={"type": "zone", "id": zone_id},
            )
        return build_zone_panel(row, self.profile)

    async def _execute(self, sql: str, entity_id: int) -> Mapping[str, Any]:
        params = {"municipality_id": self.profile.id, "id": entity_id}
        async with self.session_factory() as session:
            result = await session.execute(text(sql), params)
            return result.mappings().one()


__all__ = [
    "GROUP2_KEYS",
    "ParcelPanelService",
    "build_parcel_panel",
    "build_zone_panel",
    "group1_fields",
    "run_engine",
]
