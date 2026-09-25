"""Provider registry and factory for the geocoding proxy (see ``core.geocode.base``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.geocode.base import (
    BoundingBox,
    GeocodeHit,
    GeocodeProvider,
    GeocodeProviderError,
    HttpGeocodeProvider,
    ResultKind,
    SearchScope,
)
from core.geocode.nominatim import NominatimProvider
from core.geocode.photon import PhotonProvider

if TYPE_CHECKING:
    from core.config import Settings

# ``GEOCODER_PROVIDER`` -> adapter. Adding Google = a module implementing ``GeocodeProvider``,
# an entry here and the literal in ``core.config.Settings.geocoder_provider``.
PROVIDERS: dict[str, type[HttpGeocodeProvider]] = {
    PhotonProvider.name: PhotonProvider,
    NominatimProvider.name: NominatimProvider,
}


def user_agent_for(settings: Settings) -> str:
    """Identify the application to the provider (both OSM usage policies require it)."""
    if settings.geocoder_user_agent:
        return settings.geocoder_user_agent
    agent = f"{settings.app_name}/{settings.app_version}"
    return f"{agent} (+{settings.geocoder_contact})" if settings.geocoder_contact else agent


def build_provider(settings: Settings) -> GeocodeProvider:
    try:
        cls = PROVIDERS[settings.geocoder_provider]
    except KeyError:
        known = ", ".join(sorted(PROVIDERS))
        raise ValueError(
            f"unknown GEOCODER_PROVIDER {settings.geocoder_provider!r}; known: {known}"
        ) from None
    kwargs: dict[str, object] = {
        "user_agent": user_agent_for(settings),
        "timeout_ms": settings.geocoder_timeout_ms,
        "base_url": settings.geocoder_base_url,
    }
    if cls is NominatimProvider:
        contact = settings.geocoder_contact or ""
        kwargs["email"] = contact if "@" in contact else None
    return cls(**kwargs)  # type: ignore[arg-type]


__all__ = [
    "PROVIDERS",
    "BoundingBox",
    "GeocodeHit",
    "GeocodeProvider",
    "GeocodeProviderError",
    "HttpGeocodeProvider",
    "NominatimProvider",
    "PhotonProvider",
    "ResultKind",
    "SearchScope",
    "build_provider",
    "user_agent_for",
]
