from fastapi import APIRouter

from api.routers.v1 import feasibility, locate, municipality, panel

router = APIRouter(prefix="/v1")
router.include_router(municipality.router)
router.include_router(locate.router)
router.include_router(panel.router)
router.include_router(feasibility.router)

__all__ = ["router"]
