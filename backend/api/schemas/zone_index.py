"""`GET /v1/zones`: the zone index the search box matches zone names against."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from api.schemas.locate import LatLng


class ZoneIndexEntry(BaseModel):
    id: int
    name: str
    zone_type: str | None = Field(
        default=None, description="res | com | mix | pub | grn; null = not classified"
    )
    covered: bool = Field(
        description="An adopted, live, current planning document with coverage belongs to the zone"
    )
    bbox: tuple[float, float, float, float] = Field(
        description="min_lng, min_lat, max_lng, max_lat: where the map fits to"
    )
    centroid: LatLng = Field(description="A point on the zone's surface (label / pin)")
    geometry: dict[str, Any] = Field(
        description="GeoJSON MultiPolygon, simplified (~5 m): which zone a search result is in"
    )


class ZoneIndex(BaseModel):
    zones: list[ZoneIndexEntry]
