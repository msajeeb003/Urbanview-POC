"""Search-box autocomplete: ``GET /v1/geocode?q=`` proxies an OSM-based geocoder.

A thin proxy (``api.services.geocode``): results are scoped to the municipality, cached briefly,
and the provider's usage policy is respected. Product rule: search never dead-ends, so a failing
provider answers 200 with an empty list; only a malformed ``q`` is a 422. The client follows a
selected suggestion with ``GET /v1/locate?lat=&lng=``; no parcel is resolved here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response

from api.deps import GeocodeServiceDep
from api.schemas.geocode import GeocodeResponse
from api.services.geocode import GeocodeStatus

router = APIRouter(prefix="/geocode", tags=["geocode"])

MAX_QUERY_LENGTH = 200
STATUS_HEADER = "X-Geocode-Status"


@router.get(
    "",
    response_model=GeocodeResponse,
    summary="Autocomplete addresses, streets and places inside the municipality",
    response_description=(
        "Always 200. ``results`` is empty when nothing matched, the query is too short or the "
        "provider was unavailable; the ``X-Geocode-Status`` header says which."
    ),
    responses={
        200: {
            "headers": {
                STATUS_HEADER: {
                    "description": "hit | miss | too_short | throttled | provider_unavailable",
                    "schema": {"type": "string", "enum": [s.value for s in GeocodeStatus]},
                }
            }
        }
    },
)
async def geocode(
    q: Annotated[
        str,
        Query(
            min_length=1,
            max_length=MAX_QUERY_LENGTH,
            description="Free text: street, house number, place or point of interest",
        ),
    ],
    service: GeocodeServiceDep,
    response: Response,
) -> GeocodeResponse:
    outcome = await service.search(q)
    response.headers[STATUS_HEADER] = outcome.status.value
    return outcome.response
