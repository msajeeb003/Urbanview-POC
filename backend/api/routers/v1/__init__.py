from fastapi import APIRouter

from api.routers.v1 import (
    admin_analytics,
    admin_config,
    admin_email,
    admin_jobs,
    admin_orders,
    admin_pipeline,
    admin_publish,
    admin_review,
    auth,
    events,
    feasibility,
    geocode,
    locate,
    municipality,
    orders,
    panel,
    parcel_panel,
    source,
    tiles,
    zones,
)

router = APIRouter(prefix="/v1")
router.include_router(municipality.router)
router.include_router(locate.router)
router.include_router(geocode.router)
router.include_router(panel.router)
router.include_router(zones.router)
router.include_router(parcel_panel.router)
router.include_router(feasibility.router)
router.include_router(source.router)
router.include_router(events.router)
router.include_router(admin_analytics.router)
router.include_router(admin_pipeline.router)
router.include_router(admin_jobs.router)
router.include_router(admin_publish.router)
router.include_router(tiles.router)
router.include_router(admin_config.router)
router.include_router(admin_review.router)
router.include_router(auth.router)
router.include_router(orders.router)
router.include_router(admin_orders.router)
router.include_router(admin_email.router)

__all__ = ["router"]
