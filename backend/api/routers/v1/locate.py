"""Location resolution: map point or cadastral reference -> parcels, block, zone, document.

Product rule (BRD S6): a location with no adopted planning data is NOT an error. Both endpoints
always answer 200; ``covered`` is false and the planning fields are null when nothing adopted
covers the location, and a parcel reference that matches nothing is ``coverage.reason =
parcel_not_found``. Only malformed input (latitude out of range, missing KO) is a 422.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from api.deps import ResolverDep
from api.schemas.locate import LocationResolution
from core.errors import AppError

router = APIRouter(prefix="/locate", tags=["locate"])


@router.get(
    "",
    response_model=LocationResolution,
    summary="Resolve a map point to cadastral parcel, planned urban parcel, block, zone, document",
    response_description=(
        "Always 200. ``covered`` is false and zone/document/urban parcel are null when no adopted "
        "planning document covers the point; the query coordinates are echoed back."
    ),
)
async def locate_point(
    lat: Annotated[float, Query(ge=-90, le=90, description="Latitude, WGS84")],
    lng: Annotated[float, Query(ge=-180, le=180, description="Longitude, WGS84")],
    resolver: ResolverDep,
) -> LocationResolution:
    return await resolver.resolve_point(lat=lat, lng=lng)


@router.get(
    "/parcel",
    response_model=LocationResolution,
    summary="Look up a cadastral parcel by cadastral municipality (KO) + number (+ sub-number)",
    response_description=(
        "Same payload as /locate plus the parcel centroid so the map can pan to it. "
        "KO is required: the same parcel number exists in several cadastral municipalities."
    ),
)
async def locate_parcel(
    ko: Annotated[
        str,
        Query(
            min_length=1,
            description="Cadastral municipality (KO) name, e.g. 'Podgorica I'. Case-insensitive.",
        ),
    ],
    number: Annotated[
        str,
        Query(
            min_length=1, description="Cadastral parcel number; '1042/3' is split into number + sub"
        ),
    ],
    resolver: ResolverDep,
    sub: Annotated[str | None, Query(description="Sub-number, if any")] = None,
) -> LocationResolution:
    ko, number, sub = normalise_parcel_ref(ko, number, sub)
    return await resolver.resolve_parcel(ko=ko, parcel_number=number, sub_number=sub)


def normalise_parcel_ref(ko: str, number: str, sub: str | None) -> tuple[str, str, str | None]:
    ko = " ".join(ko.split())
    number = number.strip()
    sub = sub.strip() if sub else None
    if sub == "":
        sub = None
    if sub is None and "/" in number:
        number, _, sub = number.partition("/")
        number, sub = number.strip(), sub.strip() or None
    problems = []
    if not ko:
        problems.append({"loc": ["query", "ko"], "msg": "cadastral municipality (KO) is required"})
    if not number:
        problems.append({"loc": ["query", "number"], "msg": "parcel number is required"})
    if problems:
        raise AppError(
            "Request validation failed",
            code="validation_error",
            status_code=422,
            details=problems,
        )
    return ko, number, sub
