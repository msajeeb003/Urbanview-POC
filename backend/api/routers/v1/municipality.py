"""Active municipality profile: bounds, centre, terminology and data sources for the frontend."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from api.deps import get_municipality
from core.municipality import MunicipalityProfile

router = APIRouter(prefix="/municipality", tags=["municipality"])


@router.get("", response_model=MunicipalityProfile, summary="Active municipality profile")
async def get_active_municipality(
    municipality: Annotated[MunicipalityProfile, Depends(get_municipality)],
) -> MunicipalityProfile:
    return municipality
