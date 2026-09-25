"""Photon adapter: https://github.com/komoot/photon (OSM data, built for search-as-you-type).

Public instance ``photon.komoot.io``: fair use, identify the application, cache results. Photon
takes a ``bbox`` (minLon,minLat,maxLon,maxLat), a ``lat``/``lon`` location bias and ``lang`` for
en / de / fr / it only (anything else is HTTP 400), so other locales get local names, which is
what the map labels in Podgorica anyway. It has no country filter: the service drops foreign hits
by ``countrycode`` and box.
"""

from __future__ import annotations

from typing import Any

from core.geocode.base import (
    GeocodeHit,
    GeocodeProviderError,
    HttpGeocodeProvider,
    ResultKind,
    SearchScope,
    classify,
    compose_hit,
    first_present,
)

PHOTON_LANGUAGES = frozenset({"en", "de", "fr", "it"})
PLACE_TYPES = frozenset({"locality", "district", "city", "county", "state", "country"})


class PhotonProvider(HttpGeocodeProvider):
    name = "photon"
    default_base_url = "https://photon.komoot.io"
    default_min_interval_ms = 0  # fair use: the cache and the per-IP limit keep the volume sane

    async def search(self, query: str, *, limit: int, scope: SearchScope) -> list[GeocodeHit]:
        lng, lat = scope.center
        params: dict[str, Any] = {
            "q": query,
            "limit": limit,
            "bbox": scope.bbox.as_param(),
            "lat": lat,
            "lon": lng,
        }
        lang = (scope.language or "").split("-")[0].lower()
        if lang in PHOTON_LANGUAGES:
            params["lang"] = lang
        data = await self._get_json("/api/", params)
        features = data.get("features") if isinstance(data, dict) else None
        if not isinstance(features, list):
            raise GeocodeProviderError("photon: unexpected response body")
        hits = [parse_feature(feature) for feature in features if isinstance(feature, dict)]
        return [hit for hit in hits if hit is not None]


def parse_feature(feature: dict[str, Any]) -> GeocodeHit | None:
    """One GeoJSON feature -> compact hit (``None`` when unusable)."""
    geometry = feature.get("geometry")
    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
    try:
        lng, lat = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
    housenumber = first_present(props, ("housenumber",))
    street = first_present(props, ("street",))
    kind: ResultKind = classify(
        props.get("osm_key"), props.get("osm_value"), has_housenumber=bool(housenumber)
    )
    # Photon's own ``type`` refines the raw tags (a boundary is a district, a way is a street).
    photon_type = props.get("type")
    if photon_type == "street":
        kind = "street"
    elif photon_type in PLACE_TYPES and kind != "poi":
        kind = "place"
    name = first_present(props, ("name",))
    if kind == "street" and not name:
        name = street
    return compose_hit(
        name=name,
        street=street,
        housenumber=housenumber,
        locality=first_present(props, ("district", "locality")),
        city=first_present(props, ("city", "town", "village", "county")),
        lat=lat,
        lng=lng,
        kind=kind,
        country_code=first_present(props, ("countrycode",)),
    )
