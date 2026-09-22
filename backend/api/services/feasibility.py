"""Server-side recalculation from edited assumptions (``POST /v1/feasibility``).

Built on the panel service so it cannot diverge from ``GET /v1/panel``: the same statement loads
the parcel's planning parameters and its zone's market inputs from the serving tables, the same
merge applies the visitor's edits over the admin defaults, and the same shared engine
(``core.engine.shared``, the Python copy of ``packages/feasibility-engine``) produces the ranges.
No AI, no randomness, no caching: identical inputs give identical output.
"""

from __future__ import annotations

from typing import Any

from api.schemas.feasibility import (
    Disclaimer,
    EngineInfo,
    FeasibilityRequest,
    FeasibilityResponse,
    PlanningInputsUsed,
)
from api.schemas.panel import AssumptionOverrides, CadastralPanel, UrbanPanel
from api.services import panel_text
from api.services.panel import PanelService
from core.engine import shared


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


async def recalculate_feasibility(
    service: PanelService, request: FeasibilityRequest
) -> FeasibilityResponse:
    edits = request.assumptions
    overrides = AssumptionOverrides(
        saleable_share=edits.saleable_share,
        construction_cost_eur_m2=edits.construction_cost_per_m2,
        sale_price_eur_m2=edits.selling_price_per_m2,
    )
    panel = await service.get_panel(request.type, request.parcel_id, overrides)
    if not isinstance(panel, UrbanPanel | CadastralPanel):  # pragma: no cover - type is restricted
        raise TypeError(f"unexpected panel type {panel.type!r}")

    planning_inputs = None
    if panel.planning is not None:
        values = {field.key: field.value for field in panel.planning.fields}
        planning_inputs = PlanningInputsUsed(
            calculation_basis=panel.calculation_basis,
            plot_area_m2=panel.basis_area_m2,
            far=_number(values.get("max_far")),
            site_coverage_pct=_number(values.get("max_site_coverage_pct")),
            max_gfa_m2=_number(values.get("max_gfa_m2")),
            max_coverage_area_m2=_number(values.get("max_coverage_area_m2")),
        )

    return FeasibilityResponse(
        type=request.type,
        parcel_id=request.parcel_id,
        municipality_id=panel.municipality_id,
        covered=panel.covered,
        coverage_note_en=panel.coverage_note_en,
        coverage_note_me=panel.coverage_note_me,
        calculation_basis=panel.calculation_basis,
        basis_area_m2=panel.basis_area_m2,
        planning_inputs=planning_inputs,
        feasibility=panel.feasibility,
        assumptions=panel.assumptions,
        engine=EngineInfo(
            engine_version=shared.ENGINE_VERSION,
            formula_version=panel.formula_version,
            range_derivation=shared.RANGE_DERIVATION,
        ),
        data_version=panel.data_version,
        data_version_date=panel.data_version_date,
        disclaimer=Disclaimer(
            en=panel_text.DISCLAIMER.en,
            me=panel_text.DISCLAIMER.me,
            status=panel_text.DISCLAIMER_STATUS,
            version=panel_text.DISCLAIMER_VERSION,
        ),
    )
