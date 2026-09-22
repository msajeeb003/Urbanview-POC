from fastapi import APIRouter

from api.routers.v1 import locate, municipality

router = APIRouter(prefix="/v1")
router.include_router(municipality.router)
router.include_router(locate.router)

__all__ = ["router"]
