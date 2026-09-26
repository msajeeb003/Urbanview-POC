"""Preview of an unsaved financial-assumptions set on one parcel (the admin console's "Preview").

The draft replaces the zone's live version in the parcel panel's own statement row, and the
panel's builders and the shared engine compute Group 2 from it: the figures are exactly what the
public panel would show once the set applies, with no formula of the preview's own. Nothing is
written, cached or audited.
"""

from __future__ import annotations

from typing import Any

from api.schemas.admin_config import (
    AssumptionsPreviewIn,
    AssumptionsPreviewOut,
    PreviewSide,
    RateIn,
)
from api.schemas.parcel_panel import ParcelPanel
from api.services.parcel_panel import ParcelPanelService

RATES = ("land", "build", "design", "sale")


def draft_market(payload: AssumptionsPreviewIn) -> dict[str, Any]:
    """The draft shaped like the statement's ``market`` column (``panel_sql._MARKET_COLUMN``)."""

    def rate(prefix: str) -> RateIn:
        return getattr(payload, f"{prefix}_rate")

    return {
        "id": 0,
        "zone_id": payload.zone_id,
        "version": 0,
        **{f"{prefix}_rate_eur_m2": rate(prefix).expected for prefix in RATES},
        "range_low_factor": payload.range_low_factor,
        "range_high_factor": payload.range_high_factor,
        "saleable_share": payload.saleable_share,
        "source": None,
        "source_date": None,
        "effective_from": None,
        "rate_sources": None,
        "bounds": {prefix: [rate(prefix).low, rate(prefix).high] for prefix in RATES},
    }


def _side(panel: ParcelPanel, *, draft: bool) -> PreviewSide:
    market, assumptions = panel.market, panel.assumptions
    if draft:  # a draft has no version yet
        market = market.model_copy(update={"version": None}) if market else None
        assumptions = (
            assumptions.model_copy(update={"market_version": None}) if assumptions else None
        )
    return PreviewSide(market=market, assumptions=assumptions, group2=panel.group2)


async def preview_assumptions(
    panels: ParcelPanelService, payload: AssumptionsPreviewIn
) -> AssumptionsPreviewOut:
    current, draft = await panels.preview(payload.parcel_id, draft_market(payload))
    zone = current.header.zone
    basis = current.header.calculation_basis
    return AssumptionsPreviewOut(
        parcel_id=current.parcel_id,
        title=current.header.title,
        zone=zone,
        zone_mismatch=payload.zone_id is not None and (zone is None or zone.id != payload.zone_id),
        covered=current.covered,
        coverage_note_en=current.coverage_note_en,
        calculation_basis=basis.basis,
        basis_area_m2=basis.area_m2,
        formula_version=current.formula_version,
        current=_side(current, draft=False),
        draft=_side(draft, draft=True),
    )
