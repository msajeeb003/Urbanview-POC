"""Information-panel assembly (``GET /v1/panel``; contract ``docs/specs/panel-payload.md``).

``PanelService`` runs ONE statement per panel (``api.services.panel_sql``), then assembles the
payload: document refs get their bilingual status labels, planning values are resolved per
field (parcel-level value, else document-level value of the parcel's document, else
``not_stated``), the feasibility engine (``core.engine.feasibility``) computes the two computed
planning fields and the feasibility block, and every reason code becomes text through
``api.services.panel_text``. Nothing is cached between requests: an admin publish or a market
update is visible on the next call.

Rules (contract 5.3 / 5.4 / 5.5):
- a cadastral panel embeds the PRIMARY linked planned urban parcel's planning and feasibility
  when the basis is ``urban`` (one round trip renders both tickets; other links are opened via
  ``urban_parcels``) and the governing document's document-level values on the cadastral area
  when the basis is ``cadastral``;
- ``covered`` is false when no adopted document governs the cadastral parcel / when the planned
  parcel's document is not adopted: planning, market inputs, assumptions and feasibility are null
  and a neutral coverage note is set (never an error, CLAUDE.md);
- a missing entity (unknown id for the type) is a 404 ``not_found``.

Numbers follow contract 5.0: areas and percentages carry one decimal, euros are whole (engine);
the basis area handed to the engine is the one shown, so a reader can redo the arithmetic.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.locate import LatLng
from api.schemas.panel import (
    Areas,
    AssumptionOverrides,
    AssumptionsBlock,
    BlockRef,
    CadastralIdentification,
    CadastralLink,
    CadastralPanel,
    CalculationBasis,
    CoverageCounts,
    DocumentCounts,
    DocumentDetail,
    DocumentPanel,
    DocumentRef,
    FeasibilityBlock,
    FeasibilityField,
    Flags,
    Header,
    HeaderDocumentRef,
    Label,
    MarketInputsBlock,
    OverrideFlags,
    PanelResponse,
    PanelType,
    PlanningBlock,
    PlanningField,
    RateSources,
    Source,
    UrbanIdentification,
    UrbanLink,
    UrbanPanel,
    ZoneDetail,
    ZoneHeader,
    ZonePanel,
    ZoneRef,
)
from api.services import panel_text
from api.services.panel_sql import CADASTRAL_SQL, DOCUMENT_SQL, URBAN_SQL, ZONE_SQL
from core.engine import (
    DEFAULT_SALEABLE_SHARE,
    FORMULA_VERSION,
    Assumptions,
    FeasibilityResult,
    FieldRange,
    MarketInputs,
    compute_feasibility,
)
from core.errors import NotFoundError
from core.municipality import MunicipalityProfile

log = logging.getLogger("urbanview.panel")

UNPUBLISHED = "unpublished"

# What the two computed planning fields derive from (contract 2.2 formulas).
COMPUTED_DERIVED_FROM: dict[str, list[str]] = {
    "max_gfa_m2": ["max_far", "basis_area_m2"],
    "max_coverage_area_m2": ["max_site_coverage_pct", "basis_area_m2"],
}

ENTITY_LABELS: dict[str, str] = {
    "zone": "zone",
    "document": "planning document",
    "cadastral": "cadastral parcel",
    "urban": "planned urban parcel",
}


# --- small helpers --------------------------------------------------------------------------------


def _as_json(value: Any) -> Any:
    """asyncpg may hand jsonb back decoded or as text depending on codec setup."""
    if isinstance(value, str | bytes):
        return json.loads(value)
    return value


def _r1(value: float | None) -> float | None:
    """One decimal; ``+ 0.0`` turns a rounded -0.0 into 0.0."""
    return None if value is None else round(float(value), 1) + 0.0


def _pct(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return _r1(part / whole * 100)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _parcel_ref(parcel_number: str, sub_number: str | None) -> str:
    return f"{parcel_number}/{sub_number}" if sub_number else parcel_number


def _ko_and_number(ko_name: str, parcel_number: str, sub_number: str | None) -> str:
    return f"KO {ko_name}, {_parcel_ref(parcel_number, sub_number)}"


def _document_ref(raw: Mapping[str, Any]) -> DocumentRef:
    labels = panel_text.document_status_label(raw["status"])
    return DocumentRef(**raw, status_label_en=labels.en, status_label_me=labels.me)


def _document_detail(raw: Mapping[str, Any]) -> DocumentDetail:
    labels = panel_text.document_status_label(raw["status"])
    return DocumentDetail(**raw, status_label_en=labels.en, status_label_me=labels.me)


def _header_documents(
    governing: DocumentRef | None, amendments: list[DocumentRef]
) -> list[HeaderDocumentRef]:
    documents: list[HeaderDocumentRef] = []
    if governing is not None:
        documents.append(HeaderDocumentRef(**governing.model_dump(), role="governing"))
    documents.extend(HeaderDocumentRef(**a.model_dump(), role="amendment") for a in amendments)
    return documents


@dataclass(frozen=True, slots=True)
class _Version:
    label: str
    date: str | None


def _version(row: Mapping[str, Any]) -> _Version:
    return _Version(label=row["data_version"] or UNPUBLISHED, date=_iso(row["data_version_date"]))


# --- planning values ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Resolved:
    row: Mapping[str, Any]
    scope: Literal["parcel", "document"]


def _resolve_values(values_raw: list[Mapping[str, Any]]) -> dict[str, _Resolved]:
    """Parcel-level value first (the statement orders parcel rows first), else document-level."""
    resolved: dict[str, _Resolved] = {}
    for row in values_raw:
        key = row["field_key"]
        if key in resolved:
            continue
        resolved[key] = _Resolved(row=row, scope="parcel" if row["parcel_level"] else "document")
    return resolved


def _number(resolved: Mapping[str, _Resolved], key: str) -> float | None:
    item = resolved.get(key)
    if item is None or item.row.get("value_number") is None:
        return None
    return float(item.row["value_number"])


def _source(row: Mapping[str, Any]) -> Source:
    return Source(
        document_id=row["document_id"],
        document_name=row["document_name"],
        page=row["source_page"],
        bbox=row.get("source_bbox"),
        note=row.get("source_note"),
        registry_url=row.get("registry_url"),
    )


def _planning_block(
    fields_raw: list[Mapping[str, Any]],
    resolved: Mapping[str, _Resolved],
    result: FeasibilityResult,
    basis: CalculationBasis,
    basis_area_m2: float,
) -> PlanningBlock:
    fields: list[PlanningField] = []
    for spec in fields_raw:
        key = spec["key"]
        base: dict[str, Any] = {
            "key": key,
            "label_en": spec["label_en"],
            "label_me": spec["label_me"],
            "abbreviation": spec["abbreviation"],
            "unit": spec["unit"],
            "value_type": spec["value_type"],
        }
        if spec["computed"]:
            fields.append(_computed_field(base, spec, result))
            continue
        item = resolved.get(key)
        if item is None:
            fields.append(PlanningField(**base, status="not_stated"))
            continue
        row = item.row
        value = row["value_text"] if row.get("value_text") is not None else row["value_number"]
        base["unit"] = row.get("unit") or spec["unit"]
        fields.append(
            PlanningField(
                **base,
                status="stated",
                value=value,
                scope=item.scope,
                fallback=item.scope == "document" and basis == "urban",
                source=_source(row),
            )
        )
    return PlanningBlock(
        calculation_basis=basis,
        basis_area_m2=basis_area_m2,
        fields=fields,
        not_stated_label=Label(
            en=panel_text.NOT_STATED_LABEL.en, me=panel_text.NOT_STATED_LABEL.me
        ),
    )


def _computed_field(
    base: dict[str, Any], spec: Mapping[str, Any], result: FeasibilityResult
) -> PlanningField:
    try:
        figure = result.get(base["key"])
    except KeyError:
        # A computed dictionary key the engine does not know: never stored, so never a value.
        log.warning("computed planning field %s has no engine figure", base["key"])
        return PlanningField(
            **base, status="cannot_compute", formula=spec["formula"], derived_from=None
        )
    reason = panel_text.reason_text(figure.reason_code, figure.reason_params)
    return PlanningField(
        **base,
        status="computed" if figure.status == "ok" else "cannot_compute",
        value=figure.expected,
        formula=spec["formula"],
        derived_from=COMPUTED_DERIVED_FROM.get(base["key"]),
        reason_code=figure.reason_code,
        reason_en=reason.en if reason else None,
        reason_me=reason.me if reason else None,
    )


# --- market, assumptions, feasibility -------------------------------------------------------------


def _market_inputs(raw: Mapping[str, Any] | None) -> MarketInputs | None:
    if raw is None:
        return None
    return MarketInputs(
        land_rate_eur_m2=float(raw["land_rate_eur_m2"]),
        build_rate_eur_m2=float(raw["build_rate_eur_m2"]),
        design_rate_eur_m2=float(raw["design_rate_eur_m2"]),
        sale_rate_eur_m2=float(raw["sale_rate_eur_m2"]),
        range_low_factor=float(raw["range_low_factor"]),
        range_high_factor=float(raw["range_high_factor"]),
    )


def _market_reason(zone: ZoneRef | None) -> tuple[str, dict[str, Any]]:
    if zone is None:
        return "no_market_data_zone_unknown", {}
    return "no_market_data", {"zone_name": zone.name}


def _assumptions(overrides: AssumptionOverrides) -> Assumptions:
    share = overrides.saleable_share
    return Assumptions(
        saleable_share=DEFAULT_SALEABLE_SHARE if share is None else share,
        construction_cost_eur_m2=overrides.construction_cost_eur_m2,
        sale_price_eur_m2=overrides.sale_price_eur_m2,
    )


def _market_block(
    raw: Mapping[str, Any] | None,
    zone: ZoneRef | None,
    reason_code: str,
    reason_params: dict[str, Any],
) -> MarketInputsBlock:
    if raw is None:
        reason = panel_text.reason_text(reason_code, reason_params)
        assert reason is not None
        return MarketInputsBlock(
            available=False,
            reason_code=reason_code,
            reason_en=reason.en,
            reason_me=reason.me,
            zone=zone,
        )
    return MarketInputsBlock(
        available=True,
        zone=zone,
        land_rate_eur_m2=raw["land_rate_eur_m2"],
        build_rate_eur_m2=raw["build_rate_eur_m2"],
        design_rate_eur_m2=raw["design_rate_eur_m2"],
        sale_rate_eur_m2=raw["sale_rate_eur_m2"],
        range_low_factor=raw["range_low_factor"],
        range_high_factor=raw["range_high_factor"],
        source=raw.get("source"),
        source_date=_iso(raw.get("source_date")),
        effective_from=_iso(raw.get("effective_from")),
    )


def _assumptions_block(
    result: FeasibilityResult,
    overrides: AssumptionOverrides,
    market_raw: Mapping[str, Any] | None,
    version: _Version,
) -> AssumptionsBlock:
    used = result.assumptions_used
    return AssumptionsBlock(
        saleable_share=used.saleable_share,
        construction_cost_eur_m2=used.construction_cost_eur_m2,
        sale_price_eur_m2=used.sale_price_eur_m2,
        design_documentation_eur_m2=used.design_rate_eur_m2,
        land_rate_eur_m2=used.land_rate_eur_m2,
        range_low_factor=used.range_low_factor,
        range_high_factor=used.range_high_factor,
        overrides=OverrideFlags(
            saleable_share=overrides.saleable_share is not None,
            construction_cost_eur_m2=overrides.construction_cost_eur_m2 is not None,
            sale_price_eur_m2=overrides.sale_price_eur_m2 is not None,
        ),
        sources=RateSources(**used.sources.to_dict()),
        market_source=market_raw.get("source") if market_raw else None,
        market_source_date=_iso(market_raw.get("source_date")) if market_raw else None,
        formula_version=result.formula_version,
        data_version=version.label,
        data_version_date=version.date,
    )


def _feasibility_field(figure: FieldRange) -> FeasibilityField:
    label = panel_text.feasibility_label(figure.key)
    reason = panel_text.reason_text(figure.reason_code, figure.reason_params)
    return FeasibilityField(
        key=figure.key,
        label_en=label.en,
        label_me=label.me,
        unit=label.unit,
        range_kind=figure.range_kind,
        status=figure.status,
        reason_code=figure.reason_code,
        reason_params=figure.reason_params,
        reason_en=reason.en if reason else None,
        reason_me=reason.me if reason else None,
        low=figure.low,
        expected=figure.expected,
        high=figure.high,
    )


def _feasibility_block(
    result: FeasibilityResult, basis: CalculationBasis, basis_area_m2: float
) -> FeasibilityBlock:
    return FeasibilityBlock(
        calculation_basis=basis,
        basis_area_m2=basis_area_m2,
        formula_version=result.formula_version,
        fields=[_feasibility_field(f) for f in result.fields],
        cost_rows=[_feasibility_field(row) for row in result.cost_rows],
        disclaimer_en=panel_text.DISCLAIMER.en,
        disclaimer_me=panel_text.DISCLAIMER.me,
        disclaimer_status=panel_text.DISCLAIMER_STATUS,
        disclaimer_version=panel_text.DISCLAIMER_VERSION,
    )


@dataclass(frozen=True, slots=True)
class _Blocks:
    planning: PlanningBlock
    market_inputs: MarketInputsBlock
    assumptions: AssumptionsBlock
    feasibility: FeasibilityBlock


def _blocks(
    *,
    basis: CalculationBasis,
    basis_area_m2: float,
    zone: ZoneRef | None,
    version: _Version,
    fields_raw: list[Mapping[str, Any]],
    resolved: Mapping[str, _Resolved],
    market_raw: Mapping[str, Any] | None,
    overrides: AssumptionOverrides,
) -> _Blocks:
    """Planning + market + assumptions + feasibility for one covered basis."""
    reason_code, reason_params = _market_reason(zone)
    result = compute_feasibility(
        basis_area_m2,
        _number(resolved, "max_far"),
        _number(resolved, "max_site_coverage_pct"),
        _market_inputs(market_raw),
        _assumptions(overrides),
        market_reason_code=reason_code,
        market_reason_params=reason_params,
        calculation_basis=basis,
    )
    return _Blocks(
        planning=_planning_block(fields_raw, resolved, result, basis, basis_area_m2),
        market_inputs=_market_block(market_raw, zone, reason_code, reason_params),
        assumptions=_assumptions_block(result, overrides, market_raw, version),
        feasibility=_feasibility_block(result, basis, basis_area_m2),
    )


# --- areas ----------------------------------------------------------------------------------------


def _areas(
    *,
    cadastral_area_m2: float | None,
    urban_parcel_area_m2: float | None,
    overlap_m2: float | None,
    planned_area_stated_m2: float | None,
    basis: CalculationBasis,
    basis_area_m2: float,
    reason_code: Literal["urban_covers_cadastral", "no_urban_parcel", "no_cadastral_parcel"],
    reason_params: dict[str, Any],
) -> Areas:
    delta_m2 = delta_pct = None
    if cadastral_area_m2 is not None and urban_parcel_area_m2 is not None:
        delta = urban_parcel_area_m2 - cadastral_area_m2
        delta_m2 = _r1(delta)
        delta_pct = _pct(delta, cadastral_area_m2)
    stated_delta_pct = None
    if planned_area_stated_m2 is not None and basis_area_m2:
        stated_delta_pct = _pct(planned_area_stated_m2 - basis_area_m2, basis_area_m2)
    reason = panel_text.basis_reason_text(reason_code, reason_params)
    return Areas(
        cadastral_area_m2=_r1(cadastral_area_m2),
        urban_parcel_area_m2=_r1(urban_parcel_area_m2),
        delta_m2=delta_m2,
        delta_pct=delta_pct,
        overlap_m2=_r1(overlap_m2),
        share_of_cadastral_pct=_pct(overlap_m2, cadastral_area_m2),
        share_of_urban_pct=_pct(overlap_m2, urban_parcel_area_m2),
        planned_area_stated_m2=planned_area_stated_m2,
        stated_vs_geometry_delta_pct=stated_delta_pct,
        calculation_basis=basis,
        basis_area_m2=basis_area_m2,
        basis_reason_code=reason_code,
        basis_reason_params=reason_params,
        basis_reason_en=reason.en,
        basis_reason_me=reason.me,
    )


# --- service --------------------------------------------------------------------------------------


class PanelService:
    """One statement per panel against the serving tables; no cache."""

    def __init__(
        self,
        profile: MunicipalityProfile,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        min_overlap_m2: float = 1.0,
        min_overlap_fraction: float = 0.02,
    ) -> None:
        self.profile = profile
        self.session_factory = session_factory
        self.min_overlap_m2 = float(min_overlap_m2)
        self.min_overlap_fraction = float(min_overlap_fraction)

    async def get_panel(
        self, panel_type: PanelType, entity_id: int, overrides: AssumptionOverrides
    ) -> PanelResponse:
        if panel_type == "zone":
            return await self.zone_panel(entity_id)
        if panel_type == "document":
            return await self.document_panel(entity_id)
        if panel_type == "cadastral":
            return await self.cadastral_panel(entity_id, overrides)
        return await self.urban_panel(entity_id, overrides)

    # --- zone / document ---------------------------------------------------------------------

    async def zone_panel(self, zone_id: int) -> ZonePanel:
        row = await self._execute(ZONE_SQL, zone_id)
        zone_raw = _as_json(row["zone"])
        if zone_raw is None:
            raise self._not_found("zone", zone_id)
        version = _version(row)
        return ZonePanel(
            municipality_id=self.profile.id,
            data_version=version.label,
            data_version_date=version.date,
            formula_version=FORMULA_VERSION,
            zone=ZoneDetail(**zone_raw),
            header=ZoneHeader(
                title=zone_raw["name"],
                subtitle_en=panel_text.ZONE_SUBTITLE.en,
                subtitle_me=panel_text.ZONE_SUBTITLE.me,
            ),
            planning_documents=[_document_ref(d) for d in _as_json(row["documents"]) or []],
            counts=DocumentCounts(**_as_json(row["counts"])),
        )

    async def document_panel(self, document_id: int) -> DocumentPanel:
        row = await self._execute(DOCUMENT_SQL, document_id)
        doc_raw = _as_json(row["document"])
        if doc_raw is None:
            raise self._not_found("document", document_id)
        version = _version(row)
        return DocumentPanel(
            municipality_id=self.profile.id,
            data_version=version.label,
            data_version_date=version.date,
            formula_version=FORMULA_VERSION,
            document=_document_detail(doc_raw),
            amendments_in_progress=[_document_ref(a) for a in _as_json(row["amendments"]) or []],
            zones=[ZoneRef(**z) for z in _as_json(row["zones"]) or []],
            coverage_counts=CoverageCounts(
                cadastral_parcels=row["cadastral_parcels"], urban_parcels=row["urban_parcels"]
            ),
            general_planning_summary=row["general_planning_summary"],
        )

    # --- cadastral ---------------------------------------------------------------------------

    async def cadastral_panel(
        self, parcel_id: int, overrides: AssumptionOverrides
    ) -> CadastralPanel:
        row = await self._execute(CADASTRAL_SQL, parcel_id, with_thresholds=True)
        cad = _as_json(row["cadastral"])
        if cad is None:
            raise self._not_found("cadastral", parcel_id)
        version = _version(row)
        doc_raw = _as_json(row["governing_document"])
        governing = _document_ref(doc_raw) if doc_raw else None
        zone_raw = _as_json(row["zone"])
        zone = ZoneRef(**zone_raw) if zone_raw else None
        blk_raw = _as_json(row["urban_block"])
        amendments = [_document_ref(a) for a in _as_json(row["amendments"]) or []]
        covered = governing is not None

        cadastral_area = float(cad["area_m2"])
        parcel_ref = _parcel_ref(cad["parcel_number"], cad["sub_number"])
        links = self._urban_links(_as_json(row["urban_parcels"]) or [], cadastral_area)
        primary = links[0] if links else None

        basis: CalculationBasis
        if primary is not None:
            basis = "urban"
            basis_area = primary.area_m2
            reason_code: Literal["urban_covers_cadastral", "no_urban_parcel"] = (
                "urban_covers_cadastral"
            )
            reason_params: dict[str, Any] = {
                "urban_parcel_number": primary.urban_parcel_number,
                "cadastral_parcel_number": parcel_ref,
                "share_pct": primary.share_of_cadastral_pct,
            }
        else:
            basis = "cadastral"
            basis_area = _r1(cadastral_area) or 0.0
            reason_code = "no_urban_parcel"
            reason_params = {"cadastral_parcel_number": parcel_ref}

        blocks: _Blocks | None = None
        stated_area = None
        if covered:
            resolved = _resolve_values(_as_json(row["values"]) or [])
            stated_area = _number(resolved, "planned_parcel_area_m2")
            blocks = _blocks(
                basis=basis,
                basis_area_m2=basis_area,
                zone=zone,
                version=version,
                fields_raw=_as_json(row["fields"]) or [],
                resolved=resolved,
                market_raw=_as_json(row["market"]),
                overrides=overrides,
            )

        areas = _areas(
            cadastral_area_m2=cadastral_area,
            urban_parcel_area_m2=primary.area_m2 if primary else None,
            overlap_m2=primary.overlap_m2 if primary else None,
            planned_area_stated_m2=stated_area,
            basis=basis,
            basis_area_m2=basis_area,
            reason_code=reason_code,
            reason_params=reason_params,
        )
        note = None if covered else panel_text.CADASTRAL_NOT_COVERED_NOTE
        return CadastralPanel(
            municipality_id=self.profile.id,
            data_version=version.label,
            data_version_date=version.date,
            formula_version=FORMULA_VERSION,
            identification=CadastralIdentification(
                parcel_id=cad["id"],
                parcel_number=cad["parcel_number"],
                sub_number=cad["sub_number"],
                ko_name=cad["ko_name"],
                street_address=cad["street_address"],
                urban_block=BlockRef(**blk_raw) if blk_raw else None,
                cadastral_area_m2=_r1(cadastral_area) or 0.0,
                governing_document=governing,
                zone=zone,
            ),
            header=Header(
                ko_and_number=_ko_and_number(
                    cad["ko_name"], cad["parcel_number"], cad["sub_number"]
                ),
                zone=zone,
                documents=_header_documents(governing, amendments),
                data_version=version.label,
                data_version_date=version.date,
            ),
            flags=Flags(
                public_ownership=cad["public_ownership"],
                restitution_or_legal_burden=cad["restitution_or_legal_burden"],
                note_en=panel_text.FLAGS_NOTE.en,
                note_me=panel_text.FLAGS_NOTE.me,
            ),
            urban_parcel_defined=primary is not None,
            urban_parcel=primary,
            urban_parcels=links,
            split=len(links) > 1,
            areas=areas,
            calculation_basis=basis,
            basis_area_m2=basis_area,
            planning=blocks.planning if blocks else None,
            market_inputs=blocks.market_inputs if blocks else None,
            assumptions=blocks.assumptions if blocks else None,
            feasibility=blocks.feasibility if blocks else None,
            covered=covered,
            coverage_note_en=note.en if note else None,
            coverage_note_me=note.me if note else None,
            centroid=LatLng(**cad["centroid"]),
            geometry=cad["geometry"],
        )

    @staticmethod
    def _urban_links(ups_raw: list[Mapping[str, Any]], cadastral_area: float) -> list[UrbanLink]:
        """Links in primary-first order (largest overlap, smallest planned area, lowest id);
        ``share_of_linked_pct`` distributes the linked overlap so the shares sum to 100."""
        total_overlap = sum(float(u["overlap_m2"]) for u in ups_raw)
        links: list[UrbanLink] = []
        for u in ups_raw:
            area = float(u["area_m2"])
            overlap = float(u["overlap_m2"])
            links.append(
                UrbanLink(
                    id=u["id"],
                    urban_parcel_number=u["urban_parcel_number"],
                    area_m2=_r1(area) or 0.0,
                    overlap_m2=_r1(overlap) or 0.0,
                    share_of_cadastral_pct=_pct(overlap, cadastral_area) or 0.0,
                    share_of_linked_pct=_pct(overlap, total_overlap) or 0.0,
                    delta_pct=_pct(area - cadastral_area, cadastral_area) or 0.0,
                    document=_document_ref(u["document"]),
                    urban_block=BlockRef(**u["urban_block"]) if u.get("urban_block") else None,
                )
            )
        return links

    # --- urban -------------------------------------------------------------------------------

    async def urban_panel(self, urban_parcel_id: int, overrides: AssumptionOverrides) -> UrbanPanel:
        row = await self._execute(URBAN_SQL, urban_parcel_id, with_thresholds=True)
        up = _as_json(row["urban_parcel"])
        if up is None:
            raise self._not_found("urban", urban_parcel_id)
        version = _version(row)
        document = _document_ref(_as_json(row["document"]))
        covered = document.status == "adopted"
        zone_raw = _as_json(row["zone"])
        zone = ZoneRef(**zone_raw) if zone_raw else None
        blk_raw = _as_json(row["urban_block"])
        amendments = [_document_ref(a) for a in _as_json(row["amendments"]) or []]

        urban_area = float(up["area_m2"])
        basis_area = _r1(urban_area) or 0.0
        links = self._cadastral_links(_as_json(row["cadastral_parcels"]) or [], urban_area)
        primary = links[0] if links else None

        reason_code: Literal["urban_covers_cadastral", "no_cadastral_parcel"]
        if primary is not None:
            reason_code = "urban_covers_cadastral"
            reason_params: dict[str, Any] = {
                "urban_parcel_number": up["urban_parcel_number"],
                "cadastral_parcel_number": _parcel_ref(primary.parcel_number, primary.sub_number),
                "share_pct": primary.share_of_cadastral_pct,
            }
        else:
            reason_code = "no_cadastral_parcel"
            reason_params = {"urban_parcel_number": up["urban_parcel_number"]}

        blocks: _Blocks | None = None
        stated_area = None
        if covered:
            resolved = _resolve_values(_as_json(row["values"]) or [])
            stated_area = _number(resolved, "planned_parcel_area_m2")
            blocks = _blocks(
                basis="urban",
                basis_area_m2=basis_area,
                zone=zone,
                version=version,
                fields_raw=_as_json(row["fields"]) or [],
                resolved=resolved,
                market_raw=_as_json(row["market"]),
                overrides=overrides,
            )

        areas = _areas(
            cadastral_area_m2=primary.area_m2 if primary else None,
            urban_parcel_area_m2=urban_area,
            overlap_m2=primary.overlap_m2 if primary else None,
            planned_area_stated_m2=stated_area,
            basis="urban",
            basis_area_m2=basis_area,
            reason_code=reason_code,
            reason_params=reason_params,
        )
        note = (
            None
            if covered
            else panel_text.URBAN_NOT_COVERED_NOTE.format(
                document_name=document.name,
                status_en=document.status_label_en,
                status_me=document.status_label_me,
            )
        )
        return UrbanPanel(
            municipality_id=self.profile.id,
            data_version=version.label,
            data_version_date=version.date,
            formula_version=FORMULA_VERSION,
            identification=UrbanIdentification(
                urban_parcel_id=up["id"],
                urban_parcel_number=up["urban_parcel_number"],
                urban_block=BlockRef(**blk_raw) if blk_raw else None,
                governing_document=document,
                zone=zone,
                cadastral_parcel=primary,
                linked_cadastral_parcels=links,
            ),
            header=Header(
                ko_and_number=(
                    _ko_and_number(primary.ko_name, primary.parcel_number, primary.sub_number)
                    if primary
                    else None
                ),
                zone=zone,
                documents=_header_documents(document, amendments),
                data_version=version.label,
                data_version_date=version.date,
            ),
            areas=areas,
            basis_area_m2=basis_area,
            covered=covered,
            coverage_note_en=note.en if note else None,
            coverage_note_me=note.me if note else None,
            planning=blocks.planning if blocks else None,
            market_inputs=blocks.market_inputs if blocks else None,
            assumptions=blocks.assumptions if blocks else None,
            feasibility=blocks.feasibility if blocks else None,
            centroid=LatLng(**up["centroid"]),
            geometry=up["geometry"],
        )

    @staticmethod
    def _cadastral_links(
        cads_raw: list[Mapping[str, Any]], urban_area: float
    ) -> list[CadastralLink]:
        links: list[CadastralLink] = []
        for c in cads_raw:
            area = float(c["area_m2"])
            overlap = float(c["overlap_m2"])
            links.append(
                CadastralLink(
                    parcel_id=c["parcel_id"],
                    parcel_number=c["parcel_number"],
                    sub_number=c["sub_number"],
                    ko_name=c["ko_name"],
                    street_address=c["street_address"],
                    area_m2=_r1(area) or 0.0,
                    overlap_m2=_r1(overlap) or 0.0,
                    share_of_urban_pct=_pct(overlap, urban_area) or 0.0,
                    share_of_cadastral_pct=_pct(overlap, area) or 0.0,
                )
            )
        return links

    # --- plumbing ----------------------------------------------------------------------------

    def _not_found(self, panel_type: str, entity_id: int) -> NotFoundError:
        return NotFoundError(
            f"No {ENTITY_LABELS[panel_type]} with id {entity_id} in municipality {self.profile.id}",
            details={"type": panel_type, "id": entity_id},
        )

    async def _execute(
        self, sql: str, entity_id: int, *, with_thresholds: bool = False
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {"municipality_id": self.profile.id, "id": entity_id}
        if with_thresholds:
            params["min_overlap_m2"] = self.min_overlap_m2
            params["min_overlap_fraction"] = self.min_overlap_fraction
        async with self.session_factory() as session:
            result = await session.execute(text(sql), params)
            return result.mappings().one()
