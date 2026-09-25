"""``GET /v1/tiles/current``: where the map's vector tiles are.

One signed, short-lived URL to the current version's PMTiles archive (the bucket stays private;
the PMTiles client fetches byte ranges from it), the ``data_version`` it carries and the source
layers inside it. Without a published version: 200 with ``status = unpublished`` and no URL.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from api.deps import PublishServiceDep
from api.schemas.publish import TilesCurrent


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/tiles", tags=["tiles"], dependencies=[Depends(_no_store)])


@router.get(
    "/current",
    response_model=TilesCurrent,
    summary="Current tile archive",
    responses={503: {"description": "The planning database is not configured"}},
)
async def current_tiles(service: PublishServiceDep) -> TilesCurrent:
    return await service.current_tiles()
