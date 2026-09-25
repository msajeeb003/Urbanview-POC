"""Display-shaped panels (contract ``docs/specs/panel-payload.md`` section 13).

- ``GET /v1/parcels/{parcel_id}/panel``: everything the panel shows for a cadastral parcel in one
  response (header, Group 1 with a source on every value, market, assumptions, Group 2 from the
  shared engine and the engine inputs for the browser's recalculation);
- ``GET /v1/zones/{zone_id}/panel``: zone name, documents with status, summary text.

Both are cached per entity per data state (current publish version + admin changes) and answer
with a strong ``ETag``; ``If-None-Match`` gives 304. ``X-Panel-Cache`` says hit / miss / bypass /
revalidated. An unknown id is 404; an uncovered parcel is 200 with ``covered: false``; without
PostGIS both answer 503.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Path, Response

from api.deps import ParcelPanelServiceDep
from api.schemas.parcel_panel import ParcelPanel, ZonePanelView
from api.services.panel_cache import PanelView

router = APIRouter(tags=["panel"])

RESPONSES = {
    304: {"description": "The client's copy (If-None-Match) is current"},
    404: {"description": "No entity with that id (`not_found`)"},
    503: {"description": "The planning database is not configured"},
}


def _response(view: PanelView) -> Response:
    headers = {"ETag": view.etag, "Cache-Control": "no-cache", "X-Panel-Cache": view.cache}
    if view.body is None:
        return Response(status_code=304, headers=headers)
    return Response(content=view.body, media_type="application/json", headers=headers)


@router.get(
    "/parcels/{parcel_id}/panel",
    response_model=ParcelPanel,
    summary="Everything the panel shows for a cadastral parcel",
    responses=RESPONSES,
)
async def parcel_panel(
    parcel_id: Annotated[int, Path(gt=0, description="cadastral_parcels.id (Parcel ID)")],
    service: ParcelPanelServiceDep,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    return _response(await service.parcel(parcel_id, if_none_match))


@router.get(
    "/zones/{zone_id}/panel",
    response_model=ZonePanelView,
    summary="Zone name, documents with status and summary text",
    responses=RESPONSES,
)
async def zone_panel(
    zone_id: Annotated[int, Path(gt=0, description="zones.id")],
    service: ParcelPanelServiceDep,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    return _response(await service.zone(zone_id, if_none_match))
