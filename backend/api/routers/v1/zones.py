"""`GET /v1/zones`: the zone index (name, type, coverage, bbox) for the search box."""

from __future__ import annotations

from fastapi import APIRouter, Response

from api.deps import ZoneIndexServiceDep
from api.schemas.zone_index import ZoneIndex

router = APIRouter(tags=["zones"])


@router.get(
    "/zones",
    response_model=ZoneIndex,
    summary="Every zone with its type, coverage, bounding box and label point",
    responses={503: {"description": "The planning database is not configured"}},
)
async def zone_index(service: ZoneIndexServiceDep, response: Response) -> ZoneIndex:
    # zones change only with a publish or an admin edit: a few minutes of caching is harmless
    response.headers["Cache-Control"] = "public, max-age=300"
    return await service.index()
