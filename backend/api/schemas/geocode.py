"""Schemas for ``GET /v1/geocode``: the compact autocomplete payload and nothing else."""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.geocode.base import ResultKind


class GeocodeResult(BaseModel):
    label: str = Field(
        description="Primary line of the suggestion: the object's name, else its street address"
    )
    address: str | None = Field(
        description="Secondary line: street + number, locality, city, without repeating the "
        "label; null when the provider knows none"
    )
    lat: float
    lng: float
    kind: ResultKind = Field(
        description="address (house number) | street | place (city, suburb, neighbourhood) | "
        "poi | other"
    )


class GeocodeResponse(BaseModel):
    query: str = Field(
        description="The normalised query these results answer, so a client can discard a reply "
        "that arrives for an older keystroke"
    )
    results: list[GeocodeResult] = Field(
        description="Scoped to the municipality. Empty when nothing matched or the provider was "
        "unavailable; never an error (see the X-Geocode-Status header)"
    )
