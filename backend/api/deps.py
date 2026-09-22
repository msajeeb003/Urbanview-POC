"""Request-scoped dependencies. Everything is read from ``app.state`` set by the app factory."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request

from api.services.resolver import LocationResolver
from core.config import Settings
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
