"""Nominatim adapter: https://nominatim.org/release-docs/latest/api/Search/

Usage policy of the public instance (https://operations.osmfoundation.org/policies/nominatim/):
identify the application (User-Agent), at most one request per second, cache results, and no
search-as-you-type. So this adapter is the choice for a self-hosted Nominatim
(``GEOCODER_BASE_URL``); against the public instance the geocode service's throttle keeps the
1 s spacing (``default_min_interval_ms``) and most keystrokes answer from the cache or empty.
"""

from __future__ import annotations

from typing import Any

from core.geocode.base import (
    GeocodeHit,
    GeocodeProviderError,
    HttpGeocodeProvider,
    SearchScope,
    classify,
    compose_hit,
    first_present,
)

ROAD_KEYS = ("road", "pedestrian", "footway", "path", "cycleway", "living_street", "square")
LOCALITY_KEYS = ("neighbourhood", "suburb", "quarter", "city_district", "borough", "residential")
CITY_KEYS = ("city", "town", "village", "municipality", "county")


class NominatimProvider(HttpGeocodeProvider):
    name = "nominatim"
    default_base_url = "https://nominatim.openstreetmap.org"
    default_min_interval_ms = 1000

    def __init__(self, *, email: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.email = email  # policy: identify larger-volume users; sent as the ``email`` parameter

    async def search(self, query: str, *, limit: int, scope: SearchScope) -> list[GeocodeHit]:
        params: dict[str, Any] = {
            "q": query,
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": limit,
            "countrycodes": scope.country_code.lower(),
            "viewbox": scope.bbox.as_param(),
            "bounded": 1,  # results inside the viewbox only
            "dedupe": 1,
        }
        if scope.language:
            params["accept-language"] = scope.language
        if self.email:
            params["email"] = self.email
        data = await self._get_json("/search", params)
        if not isinstance(data, list):
            raise GeocodeProviderError("nominatim: unexpected response body")
        hits = [parse_item(item) for item in data if isinstance(item, dict)]
        return [hit for hit in hits if hit is not None]


def parse_item(item: dict[str, Any]) -> GeocodeHit | None:
    """One ``jsonv2`` search result -> compact hit (``None`` when unusable)."""
    try:
        lat, lng = float(item["lat"]), float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    address = item.get("address") if isinstance(item.get("address"), dict) else {}
    housenumber = first_present(address, ("house_number",))
    street = first_present(address, ROAD_KEYS)
    kind = classify(
        item.get("category") or item.get("class"),
        item.get("type"),
        has_housenumber=bool(housenumber),
    )
    name = first_present(item, ("name",))
    if kind == "street" and not name:
        name = street
    return compose_hit(
        name=name,
        street=street,
        housenumber=housenumber,
        locality=first_present(address, LOCALITY_KEYS),
        city=first_present(address, CITY_KEYS),
        lat=lat,
        lng=lng,
        kind=kind,
        country_code=first_present(address, ("country_code",)),
    )
