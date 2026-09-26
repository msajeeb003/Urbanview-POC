"""Request-scoped dependencies. Everything is read from ``app.state`` set by the app factory."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Header, Request

from api.services.admin import AdminService
from api.services.admin_config import AdminConfigService
from api.services.analytics import AnalyticsService
from api.services.auth import MagicLinkService
from api.services.email import EmailService
from api.services.geocode import GeocodeService
from api.services.jobs import JobService
from api.services.market import MarketService
from api.services.orders import OrderService
from api.services.panel import PanelService
from api.services.parcel_panel import ParcelPanelService
from api.services.publish import PublishService
from api.services.resolver import LocationResolver
from api.services.review import ReviewService
from api.services.source import SourceService
from api.services.zone_index import ZoneIndexService
from core.auth import Principal, Role, TokenAuthenticator
from core.config import Settings
from core.errors import ForbiddenError, ServiceUnavailableError, UnauthorizedError
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


def get_parcel_panel_service(request: Request) -> ParcelPanelService:
    service = getattr(request.app.state, "parcel_panel_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "The information panel needs the planning database (LOCATION_RESOLVER=postgis)"
        )
    return service


ParcelPanelServiceDep = Annotated[ParcelPanelService, Depends(get_parcel_panel_service)]


def get_zone_index_service(request: Request) -> ZoneIndexService:
    service = getattr(request.app.state, "zone_index_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "The zone index needs the planning database (LOCATION_RESOLVER=postgis)"
        )
    return service


ZoneIndexServiceDep = Annotated[ZoneIndexService, Depends(get_zone_index_service)]


def get_geocode_service(request: Request) -> GeocodeService:
    return request.app.state.geocode_service


GeocodeServiceDep = Annotated[GeocodeService, Depends(get_geocode_service)]


def get_source_service(request: Request) -> SourceService:
    """The source viewer looks values and documents up in PostGIS: without a database
    (``LOCATION_RESOLVER=nodata`` and no injected repository) it answers 503."""
    service = getattr(request.app.state, "source_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "The source viewer needs the planning database (LOCATION_RESOLVER=postgis)"
        )
    return service


SourceServiceDep = Annotated[SourceService, Depends(get_source_service)]


def get_analytics_service(request: Request) -> AnalyticsService:
    service = getattr(request.app.state, "analytics_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "Analytics need the events database (LOCATION_RESOLVER=postgis)"
        )
    return service


AnalyticsServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]


def require_role(*roles: Role):
    """Bearer-token gate for staff routes: 401 without a known token, 403 with the wrong role."""
    allowed = tuple(roles)

    async def dependency(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> Principal:
        authenticator: TokenAuthenticator = request.app.state.authenticator
        principal = authenticator.authenticate(authorization)
        if principal is None:
            # the users / roles model: a staff session token (core.auth.StaffSessionAuthenticator)
            staff = getattr(request.app.state, "staff_authenticator", None)
            if staff is not None:
                principal = await staff.authenticate(authorization)
        if principal is None:
            raise UnauthorizedError(
                "A valid bearer token is required", headers={"WWW-Authenticate": "Bearer"}
            )
        if principal.role not in allowed:
            raise ForbiddenError(
                f"Role '{principal.role.value}' may not access this resource",
                details={"required_roles": [role.value for role in allowed]},
            )
        return principal

    return dependency


AdminPrincipal = Annotated[Principal, Depends(require_role(Role.admin))]


def get_admin_service(request: Request) -> AdminService:
    service = getattr(request.app.state, "admin_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "The staff API needs the planning database (LOCATION_RESOLVER=postgis)"
        )
    return service


AdminServiceDep = Annotated[AdminService, Depends(get_admin_service)]


def get_job_service(request: Request) -> JobService:
    service = getattr(request.app.state, "job_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "The staff API needs the planning database (LOCATION_RESOLVER=postgis)"
        )
    return service


JobServiceDep = Annotated[JobService, Depends(get_job_service)]


def get_publish_service(request: Request) -> PublishService:
    service = getattr(request.app.state, "publish_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "Publishing and tiles need the planning database (LOCATION_RESOLVER=postgis)"
        )
    return service


PublishServiceDep = Annotated[PublishService, Depends(get_publish_service)]


def get_email_service(request: Request) -> EmailService:
    service = getattr(request.app.state, "email_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "E-mail needs the planning database (LOCATION_RESOLVER=postgis)"
        )
    return service


EmailServiceDep = Annotated[EmailService, Depends(get_email_service)]


def get_magic_link_service(request: Request) -> MagicLinkService:
    service = getattr(request.app.state, "magic_link_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "Staff login needs the planning database (LOCATION_RESOLVER=postgis)"
        )
    return service


MagicLinkServiceDep = Annotated[MagicLinkService, Depends(get_magic_link_service)]


def get_admin_config_service(request: Request) -> AdminConfigService:
    service = getattr(request.app.state, "admin_config_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "The staff API needs the planning database (LOCATION_RESOLVER=postgis)"
        )
    return service


AdminConfigServiceDep = Annotated[AdminConfigService, Depends(get_admin_config_service)]
# Expert review: every staff role may decide; the audit trail is for admins and reviewers.
ReviewerPrincipal = Annotated[
    Principal, Depends(require_role(Role.admin, Role.reviewer, Role.expert))
]
AuditReaderPrincipal = Annotated[Principal, Depends(require_role(Role.admin, Role.reviewer))]
# the publish button: admins and (expert) reviewers
PublisherPrincipal = Annotated[Principal, Depends(require_role(Role.admin, Role.reviewer))]


def get_review_service(request: Request) -> ReviewService:
    service = getattr(request.app.state, "review_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "The review queue needs the planning database (LOCATION_RESOLVER=postgis)"
        )
    return service


ReviewServiceDep = Annotated[ReviewService, Depends(get_review_service)]


def get_market_service(request: Request) -> MarketService:
    service = getattr(request.app.state, "market_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "Market-data imports need the planning database (LOCATION_RESOLVER=postgis)"
        )
    return service


MarketServiceDep = Annotated[MarketService, Depends(get_market_service)]
# Orders: admins and reviewers manage; experts see and deliver what is assigned to them.
OrderStaffPrincipal = Annotated[
    Principal, Depends(require_role(Role.admin, Role.reviewer, Role.expert))
]
OrderManagerPrincipal = Annotated[Principal, Depends(require_role(Role.admin, Role.reviewer))]


def get_order_service(request: Request) -> OrderService:
    service = getattr(request.app.state, "order_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "Orders need the planning database (LOCATION_RESOLVER=postgis)"
        )
    return service


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]
