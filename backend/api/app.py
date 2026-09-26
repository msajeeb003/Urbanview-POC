"""Application factory.

``create_app`` wires settings, infrastructure clients, middleware, error handlers and routers.
Tests inject fakes (Redis, clock, geocoder, storage, repositories, job dispatcher, staff auth).

Middleware order (outermost first): RequestContext -> CORS -> RateLimit -> routes.
Rate-limit responses therefore still carry request ids, timing and CORS headers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import health
from api.routers.v1 import router as v1_router
from api.services.admin import AdminService
from api.services.admin_config import AdminConfigService
from api.services.analytics import AnalyticsRepository, AnalyticsService, SqlAnalyticsRepository
from api.services.auth import MagicLinkService
from api.services.email import EmailService
from api.services.geocode import GeocodeService
from api.services.jobs import JobService
from api.services.market import MarketService
from api.services.orders import OrderService
from api.services.panel import PanelService
from api.services.panel_cache import PanelCache
from api.services.parcel_panel import ParcelPanelService
from api.services.publish import PublishService
from api.services.resolver import NoDataResolver, PostgisResolver
from api.services.review import ReviewService
from api.services.source import SourceRepository, SourceService, SqlSourceRepository
from api.services.zone_index import ZoneIndexService
from core.auth import StaffSessionAuthenticator, TokenAuthenticator, parse_api_tokens
from core.config import Settings, get_settings
from core.db import check_database, create_engine, create_session_factory
from core.errors import register_exception_handlers
from core.geocode import build_provider
from core.geocode.base import BoundingBox, GeocodeProvider, SearchScope
from core.logging import configure_logging
from core.middleware import RateLimitMiddleware, RequestContextMiddleware
from core.municipality import load_profile
from core.payments import BankTransferProvider, PaymentProvider
from core.pricing import parse_price_tiers
from core.redis import create_redis
from core.storage import ObjectStorage
from jobs.enqueue import CeleryDispatcher, JobDispatcher

log = logging.getLogger("urbanview.app")

EXPOSED_HEADERS = [
    "X-Request-ID",
    "Server-Timing",
    "X-Response-Time",
    "Retry-After",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
    "X-Geocode-Status",
    "X-Panel-Cache",
    "ETag",
]


def create_app(
    settings: Settings | None = None,
    *,
    redis_client: Any | None = None,
    rate_limit_clock: Callable[[], float] | None = None,
    geocoder: GeocodeProvider | None = None,
    storage: ObjectStorage | None = None,
    source_repository: SourceRepository | None = None,
    analytics_repository: AnalyticsRepository | None = None,
    admin_dispatcher: JobDispatcher | None = None,
    staff_authenticator: Any | None = None,
    payment_provider: PaymentProvider | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    municipality = load_profile(settings.municipality_id)
    provider = geocoder if geocoder is not None else build_provider(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.redis = redis_client if redis_client is not None else create_redis(settings)
        app.state.engine = create_engine(settings)
        app.state.session_factory = create_session_factory(app.state.engine)
        app.state.storage = storage if storage is not None else ObjectStorage(settings)
        if settings.location_resolver == "postgis":
            app.state.resolver = PostgisResolver(
                municipality,
                app.state.session_factory,
                min_overlap_m2=settings.locate_min_overlap_m2,
                min_overlap_fraction=settings.locate_min_overlap_fraction,
            )
            # Same links, same thresholds: the panel and locate agree on which planned urban
            # parcel corresponds to a cadastral parcel.
            app.state.panel_service = PanelService(
                municipality,
                app.state.session_factory,
                min_overlap_m2=settings.locate_min_overlap_m2,
                min_overlap_fraction=settings.locate_min_overlap_fraction,
            )
            app.state.zone_index_service = ZoneIndexService(
                app.state.session_factory, municipality_id=municipality.id
            )
            app.state.parcel_panel_service = ParcelPanelService(
                municipality,
                app.state.session_factory,
                cache=PanelCache(
                    app.state.session_factory,
                    lambda: getattr(app.state, "redis", None),
                    municipality_id=municipality.id,
                    ttl_seconds=settings.panel_cache_ttl_seconds,
                    namespace=settings.app_version,
                ),
            )
            # Open the first pooled connection now so the first user request does not pay for
            # it (connection + TLS + type introspection is a few hundred ms). Best effort:
            # readiness reports a database that is down; startup must not crash on it.
            try:
                await asyncio.wait_for(check_database(app.state.engine), timeout=5)
            except Exception as exc:  # noqa: BLE001
                log.warning("database warm-up failed: %s: %s", type(exc).__name__, exc)
        # Source viewer: the SQL repository needs PostGIS; tests inject an in-memory one.
        repository = source_repository
        if repository is None and settings.location_resolver == "postgis":
            repository = SqlSourceRepository(app.state.session_factory, municipality.id)
        app.state.source_service = (
            SourceService(
                repository,
                app.state.storage,
                municipality_id=municipality.id,
                expires_in_seconds=settings.source_url_expires_seconds,
            )
            if repository is not None
            else None
        )
        # Analytics: same rule, the SQL repository needs PostGIS.
        analytics_repo = analytics_repository
        if analytics_repo is None and settings.location_resolver == "postgis":
            analytics_repo = SqlAnalyticsRepository(app.state.session_factory, municipality.id)
        app.state.analytics_service = (
            AnalyticsService(analytics_repo, municipality_id=municipality.id)
            if analytics_repo is not None
            else None
        )
        # Staff API: the users / roles model and the pipeline service need PostGIS.
        if settings.location_resolver == "postgis":
            if app.state.staff_authenticator is None:
                app.state.staff_authenticator = StaffSessionAuthenticator(
                    app.state.session_factory, municipality.id
                )
            app.state.admin_service = AdminService(
                app.state.session_factory,
                storage=app.state.storage,
                dispatcher=admin_dispatcher if admin_dispatcher is not None else CeleryDispatcher(),
                municipality=municipality,
                upload_max_bytes=settings.admin_upload_max_mb * 1024 * 1024,
                max_attempts=settings.job_max_attempts,
                extraction_model=settings.extraction_model,
            )
            app.state.job_service = JobService(
                app.state.session_factory,
                dispatcher=app.state.admin_service.dispatcher,
                municipality_id=municipality.id,
            )
            app.state.publish_service = PublishService(
                app.state.session_factory,
                dispatcher=app.state.admin_service.dispatcher,
                storage=app.state.storage,
                municipality_id=municipality.id,
                keep_versions=settings.publish_keep_versions,
                tiles_url_expires_seconds=settings.tiles_url_expires_seconds,
                price_band_breaks=municipality.price_band_breaks_eur_m2,
            )
            app.state.email_service = EmailService(
                app.state.session_factory,
                dispatcher=app.state.admin_service.dispatcher,
                municipality_id=municipality.id,
                max_attempts=settings.job_max_attempts,
            )
            app.state.magic_link_service = MagicLinkService(
                app.state.session_factory,
                emails=app.state.email_service,
                municipality_id=municipality.id,
                session_ttl_days=settings.staff_session_days,
            )
            app.state.admin_config_service = AdminConfigService(
                app.state.session_factory, municipality=municipality
            )
            app.state.review_service = ReviewService(
                app.state.session_factory,
                storage=app.state.storage,
                municipality=municipality,
                link_expires_in_seconds=settings.source_url_expires_seconds,
            )
            app.state.market_service = MarketService(
                app.state.session_factory,
                storage=app.state.storage,
                dispatcher=app.state.admin_service.dispatcher,
                municipality=municipality,
                settings=settings,
                max_attempts=settings.job_max_attempts,
            )
            app.state.order_service = OrderService(
                app.state.session_factory,
                panel_service=app.state.panel_service,
                storage=app.state.storage,
                emails=app.state.email_service,
                provider=app.state.payment_provider,
                municipality=municipality,
                tiers=parse_price_tiers(settings.order_price_tiers),
                turnaround_business_days=settings.order_turnaround_business_days,
                max_per_email_per_day=settings.order_max_per_email_per_day,
                report_link_expires_seconds=settings.order_report_link_expires_seconds,
                support_email=settings.order_support_email,
                public_base_url=settings.order_public_base_url,
                upload_max_bytes=settings.admin_upload_max_mb * 1024 * 1024,
            )
        log.info(
            "startup",
            extra={
                "env": settings.app_env.value,
                "municipality": municipality.id,
                "version": settings.app_version,
            },
        )
        try:
            yield
        finally:
            if redis_client is None:
                await app.state.redis.aclose()
            await app.state.engine.dispose()
            await provider.aclose()
            log.info("shutdown")

    app = FastAPI(
        title="UrbanView API",
        version=settings.app_version,
        description=(
            "Urban feasibility engine: planning parameters and feasibility ranges per parcel. "
            "Uncovered locations are returned as 200 with an explicit uncovered result."
        ),
        lifespan=lifespan,
        docs_url=None if settings.is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_prod else "/openapi.json",
    )

    # State available before lifespan runs (used by dependencies and tests). The PostGIS resolver
    # replaces the no-data one in lifespan once the session factory exists; the panel service
    # exists only with PostGIS (its dependency answers 503 while it is None).
    app.state.settings = settings
    app.state.municipality = municipality
    app.state.resolver = NoDataResolver(municipality)
    app.state.panel_service = None
    app.state.parcel_panel_service = None
    app.state.zone_index_service = None
    app.state.source_service = None
    app.state.analytics_service = None
    app.state.admin_service = None
    app.state.job_service = None
    app.state.publish_service = None
    app.state.email_service = None
    app.state.magic_link_service = None
    app.state.admin_config_service = None
    app.state.review_service = None
    app.state.order_service = None
    # Orders: the payment seam (bank transfer in the POC; a card provider plugs in here).
    # Transactional mail is a job (jobs.tasks.email) queued by EmailService below.
    app.state.payment_provider = (
        payment_provider
        if payment_provider is not None
        else BankTransferProvider(
            beneficiary=settings.order_bank_beneficiary,
            iban=settings.order_bank_iban,
            bank_name=settings.order_bank_name,
            swift=settings.order_bank_swift,
        )
    )
    app.state.staff_authenticator = staff_authenticator  # tests inject one; else PostGIS
    # Staff routes: bearer tokens from configuration (core.auth); none configured = 401 everywhere.
    app.state.authenticator = TokenAuthenticator(
        parse_api_tokens(
            settings.admin_api_tokens.get_secret_value() if settings.admin_api_tokens else None
        )
    )
    # Search-box autocomplete: scope from the municipality profile, provider from settings
    # (or injected by tests), cache / throttle in the shared Redis.
    app.state.geocode_service = GeocodeService(
        provider,
        redis_getter=lambda: getattr(app.state, "redis", None),
        scope=SearchScope(
            bbox=BoundingBox(*municipality.bounds),
            center=municipality.center,
            country_code=municipality.country,
            language=settings.geocoder_language or municipality.locale,
        ),
        max_results=settings.geocoder_max_results,
        min_query_length=settings.geocoder_min_query_length,
        cache_ttl_seconds=settings.geocoder_cache_ttl_seconds,
        min_interval_ms=settings.geocoder_min_interval_ms,
        throttle_wait_ms=settings.geocoder_throttle_wait_ms,
        failure_backoff_seconds=settings.geocoder_failure_backoff_seconds,
        redis_timeout=settings.rate_limit_redis_timeout_ms / 1000,
        redis_fail_open_seconds=settings.rate_limit_fail_open_seconds,
    )
    if redis_client is not None:
        app.state.redis = redis_client

    register_exception_handlers(app)

    # add_middleware: the last one added is the outermost.
    app.add_middleware(
        RateLimitMiddleware,
        redis_getter=lambda: getattr(app.state, "redis", None),
        limit=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
        exempt_paths=settings.rate_limit_exempt_paths,
        trust_proxy_headers=settings.trust_proxy_headers,
        enabled=settings.rate_limit_enabled,
        redis_timeout=settings.rate_limit_redis_timeout_ms / 1000,
        fail_open_seconds=settings.rate_limit_fail_open_seconds,
        **({"clock": rate_limit_clock} if rate_limit_clock else {}),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=EXPOSED_HEADERS,
    )
    app.add_middleware(RequestContextMiddleware, slow_request_ms=settings.slow_request_ms)

    app.include_router(health.router)
    app.include_router(v1_router)
    return app
