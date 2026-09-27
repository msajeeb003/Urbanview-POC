"""`GET /v1/cadastral-municipalities`: the KO list the parcel search offers."""

from __future__ import annotations

from fastapi import APIRouter, Response

from api.deps import CadastralMunicipalityServiceDep
from api.schemas.cadastral_municipalities import CadastralMunicipalities

router = APIRouter(tags=["cadastre"])


@router.get(
    "/cadastral-municipalities",
    response_model=CadastralMunicipalities,
    summary="Every cadastral municipality (KO) with parcels: name, code, count, bounding box",
    responses={503: {"description": "The planning database is not configured"}},
)
async def cadastral_municipalities(
    service: CadastralMunicipalityServiceDep, response: Response
) -> CadastralMunicipalities:
    # KOs change only with a publish: a few minutes of caching is harmless
    response.headers["Cache-Control"] = "public, max-age=300"
    return await service.list()
