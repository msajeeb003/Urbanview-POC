"""``POST /v1/feasibility``: recalculate a parcel's feasibility ranges from edited assumptions.

The route loads the parcel's planning parameters and its zone's market inputs from the serving
tables exactly as ``GET /v1/panel`` does, merges the visitor's edits over the admin defaults and
runs the shared formula engine (``packages/feasibility-engine`` / ``core.engine.shared``).
Deterministic: no AI is involved anywhere on this path.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.deps import PanelServiceDep
from api.schemas.feasibility import FeasibilityRequest, FeasibilityResponse
from api.services.feasibility import recalculate_feasibility

router = APIRouter(prefix="/feasibility", tags=["feasibility"])


@router.post(
    "",
    response_model=FeasibilityResponse,
    summary="Recalculate the Group 2 feasibility ranges of a parcel from edited assumptions",
    response_description=(
        "Every Group 2 figure as a low / expected / high range, the assumptions used, the "
        "calculation basis, engine and data versions and the indicative-figures disclaimer. "
        "An uncovered parcel returns 200 with ``covered: false`` and no figures."
    ),
    responses={404: {"description": "No parcel with that id for the type (`not_found`)"}},
)
async def post_feasibility(
    request: FeasibilityRequest, service: PanelServiceDep
) -> FeasibilityResponse:
    return await recalculate_feasibility(service, request)
