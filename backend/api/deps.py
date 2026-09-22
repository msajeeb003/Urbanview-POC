"""Request-scoped dependencies. Everything is read from ``app.state`` set by the app factory."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request

from api.services.panel import PanelService
from api.services.resolver import LocationResolver
from core.config import Settings
from core.errors import ServiceUnavailableError
from core.municipality import MunicipalityProfile
from core.storage import ObjectStorage


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_municipality(request: Request) -> MunicipalityProfile:
    return request.app.state.municipality


def get_redis(request: Request) -> Any:
    return request.app.state.redis


def get_storage(request: Request) -> ObjectStorage:
    return request.app.state.storage


def get_resolver(request: Request) -> LocationResolver:
    return request.app.state.resolver


ResolverDep = Annotated[LocationResolver, Depends(get_resolver)]


def get_panel_service(request: Request) -> PanelService:
    """The panel is served from PostGIS only: without a database (``LOCATION_RESOLVER=nodata``)
    the app still imports and runs, and the panel answers 503 ``service_unavailable``."""
    service = getattr(request.app.state, "panel_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "The information panel needs the planning database (LOCATION_RESOLVER=postgis)"
        )
    return service


PanelServiceDep = Annotated[PanelService, Depends(get_panel_service)]
