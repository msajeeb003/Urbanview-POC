"""The admin console's Overview (roles admin, reviewer): the stat cards and the pipeline status
per district (``api.services.overview``)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from api.deps import AuditReaderPrincipal, OverviewServiceDep
from api.schemas.overview import OverviewOut


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(_no_store)])


@router.get(
    "/overview",
    response_model=OverviewOut,
    summary="Parcels, documents, pending review, paid orders and the pipeline per district",
)
async def get_overview(principal: AuditReaderPrincipal, service: OverviewServiceDep) -> OverviewOut:
    return await service.overview()
