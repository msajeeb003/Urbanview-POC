"""Geocoding providers: the pluggable seam behind ``GET /v1/geocode``.

A provider turns free text into a short list of :class:`GeocodeHit` values for one
:class:`SearchScope` (municipality bounding box, country, bias point, preferred language) and
nothing else: no parcel resolution, no caching, no throttling (``api.services.geocode`` owns
those). Adapters exist for the OpenStreetMap-based services Photon (``photon``, the default:
built for search-as-you-type) and Nominatim (``nominatim``). A commercial geocoder (Google
Places) is a new module implementing :class:`GeocodeProvider`, an entry in
``core.geocode.PROVIDERS`` and the ``GEOCODER_PROVIDER`` literal in ``core.config``.

Every adapter maps its provider's taxonomy onto one compact vocabulary (``label``, ``address``,
``lat``/``lng``, ``kind``) so the frontend never sees provider-specific fields.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Protocol

import httpx

log = logging.getLogger("urbanview.geocode")

ResultKind = Literal["address", "street", "place", "poi", "other"]
RESULT_KINDS: tuple[ResultKind, ...] = ("address", "street", "place", "poi", "other")


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """WGS84 box in the municipality profile's order: ``min_lng, min_lat, max_lng, max_lat``."""

    min_lng: float
    min_lat: float
    max_lng: float
    max_lat: float

    def contains(self, lng: float, lat: float) -> bool:
        return self.min_lng <= lng <= self.max_lng and self.min_lat <= lat <= self.max_lat

    def as_param(self) -> str:
        """``minLng,minLat,maxLng,maxLat``: the order Nominatim (viewbox) and Photon (bbox) take."""
        return f"{self.min_lng},{self.min_lat},{self.max_lng},{self.max_lat}"


@dataclass(frozen=True, slots=True)
class SearchScope:
    """Where and how to search: the municipality's box and country, a bias point, a language."""

    bbox: BoundingBox
    center: tuple[float, float]  # lng, lat: location bias for providers that rank by distance
    country_code: str  # ISO 3166-1 alpha-2, e.g. "ME"
    language: str | None = None  # BCP 47 preference for labels; each adapter knows its support


@dataclass(frozen=True, slots=True)
class GeocodeHit:
    label: str
    address: str | None
    lat: float
    lng: float
    kind: ResultKind
    country_code: str | None = None  # upper-case ISO code when the provider reports one


class GeocodeProviderError(Exception):
    """The provider could not answer: network error, timeout, non-200 status, malformed body."""


class GeocodeProvider(Protocol):
    name: str
    min_interval_ms: int  # the provider's usage policy: minimum spacing between our calls

    async def search(self, query: str, *, limit: int, scope: SearchScope) -> list[GeocodeHit]: ...

    async def aclose(self) -> None: ...


# --- shared mapping from OSM tags to the compact result kind ------------------------------------

# Top-level OSM keys that describe a point of interest rather than an address component.
POI_KEYS = frozenset(
    {
        "amenity",
        "shop",
        "tourism",
        "leisure",
        "office",
        "craft",
        "healthcare",
        "historic",
        "sport",
        "public_transport",
        "aeroway",
        "railway",
        "emergency",
        "club",
        "man_made",
    }
)
PLACE_KEYS = frozenset({"place", "boundary"})
# highway=* values that are stops and facilities on a road, not the road itself
HIGHWAY_POI_VALUES = frozenset({"bus_stop", "platform", "rest_area", "services", "elevator"})
HOUSE_VALUES = frozenset({"house", "houses"})


def classify(osm_key: str | None, osm_value: str | None, *, has_housenumber: bool) -> ResultKind:
    """Map an OSM ``key=value`` pair (plus whether a house number is present) to a result kind."""
    key = (osm_key or "").lower()
    value = (osm_value or "").lower()
    if key == "highway":
        return "poi" if value in HIGHWAY_POI_VALUES else "street"
    if key == "place" and value in HOUSE_VALUES:
        return "address"
    if key in PLACE_KEYS:
        return "place"
    if key in POI_KEYS:
        return "poi"
    if has_housenumber:
        return "address"
    return "other"


def compose_hit(
    *,
    name: str | None,
    street: str | None,
    housenumber: str | None,
    locality: str | None,
    city: str | None,
    lat: float,
    lng: float,
    kind: ResultKind,
    country_code: str | None,
) -> GeocodeHit | None:
    """Build the display label and the secondary address line from address components.

    ``label`` is the object's own name, else its street address (street + number). The address
    line lists the remaining components (street + number, locality, city) without repeating the
    label. An object with neither a name nor a street address is useless as a suggestion and is
    dropped (``None``).
    """
    street_line = " ".join(part for part in (street, housenumber) if part) or None
    label = name or street_line
    if not label:
        return None
    parts: list[str] = []
    for part in (street_line, locality, city):
        if part and part != label and part not in parts:
            parts.append(part)
    return GeocodeHit(
        label=label,
        address=", ".join(parts) or None,
        lat=lat,
        lng=lng,
        kind=kind,
        country_code=country_code.upper() if country_code else None,
    )


def first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """The first non-blank string value under ``keys`` (provider payloads mix str and None)."""
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# --- HTTP plumbing shared by the adapters -------------------------------------------------------


class HttpGeocodeProvider:
    """One ``httpx.AsyncClient`` per provider with an identifying ``User-Agent`` (both OSM usage
    policies require one; library defaults are rejected) and a hard timeout. ``_get_json`` turns
    every failure into :class:`GeocodeProviderError` so the service has one thing to catch."""

    name: ClassVar[str] = "http"
    default_base_url: ClassVar[str] = ""
    default_min_interval_ms: ClassVar[int] = 0

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_ms: int,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        self.user_agent = user_agent
        self.min_interval_ms = self.default_min_interval_ms
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_ms / 1000),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=False,
            transport=transport,
        )

    async def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        try:
            response = await self._client.get(self.base_url + path, params=params)
        except httpx.HTTPError as exc:
            raise GeocodeProviderError(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        if response.status_code != 200:
            raise GeocodeProviderError(f"{self.name}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise GeocodeProviderError(f"{self.name}: response is not JSON") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
