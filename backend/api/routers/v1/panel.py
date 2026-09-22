"""Information panel: what a visitor sees after selecting an object on the map.

``GET /v1/panel?type=zone|document|cadastral|urban&id=<int>`` plus three optional assumption
overrides (``saleable_share``, ``construction_cost_eur_m2``, ``sale_price_eur_m2``) that apply
to every panel carrying a feasibility block (contract ``docs/specs/panel-payload.md`` section 1).

Unknown ``type`` or an out-of-range override is a 422 ``validation_error``; an unknown ``id`` for
the type is a 404 ``not_found`` (a missing *entity* looked up by primary key is an error, unlike
an uncovered *location*, which the panel reports as ``covered: false`` with 200).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from api.deps import PanelServiceDep
from api.schemas.panel import AssumptionOverrides, PanelResponse, PanelType

router = APIRouter(prefix="/panel", tags=["panel"])


@router.get(
    "",
    response_model=PanelResponse,
    summary="Information panel for a zone, planning document, cadastral parcel or planned parcel",
    response_description=(
        "One payload per type, discriminated by ``type``. Cadastral and urban panels carry the "
        "planning block (free) and the market / assumptions / feasibility blocks (paid), or null "
        "with ``covered: false`` when no adopted document governs the object."
    ),
    responses={404: {"description": "No entity with that id for the type (`not_found`)"}},
)
async def get_panel(
    panel_type: Annotated[PanelType, Query(alias="type", description="Panel type")],
    entity_id: Annotated[
        int,
        Query(
            alias="id",
            description="zones.id / planning_documents.id / cadastral_parcels.id (Parcel ID) / "
            "urban_parcels.id",
        ),
    ],
    service: PanelServiceDep,
    saleable_share: Annotated[
        float | None,
        Query(gt=0, le=1.00, description="Saleable share of the GFA, 0 < share ≤ 1 (default 0.70)"),
    ] = None,
    construction_cost_eur_m2: Annotated[
        float | None,
        Query(gt=0, le=100_000, description="Construction cost per m² GFA (default: market)"),
    ] = None,
    sale_price_eur_m2: Annotated[
        float | None,
        Query(gt=0, le=100_000, description="Selling price per m² saleable (default: market)"),
    ] = None,
) -> PanelResponse:
    overrides = AssumptionOverrides(
        saleable_share=saleable_share,
        construction_cost_eur_m2=construction_cost_eur_m2,
        sale_price_eur_m2=sale_price_eur_m2,
    )
    return await service.get_panel(panel_type, entity_id, overrides)
