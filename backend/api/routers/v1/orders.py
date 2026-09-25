"""Public order flow: guest checkout for an expert analysis and the public status page.

``GET /v1/orders/pricing`` publishes the configured price tiers so the panel can show a parcel's
price before the order. ``POST /v1/orders`` takes the form the panel showed (no account, no
password, no verification step before purchase), prices the order server-side, stores a snapshot
of what the visitor saw, and e-mails the bank-transfer instructions.
``GET /v1/orders/{reference}/status`` returns status, location and turnaround only: no personal
data. All are behind the global per-IP rate limit; creation is additionally capped per e-mail
address and day.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response

from api.deps import OrderServiceDep, get_settings_dep
from api.schemas.orders import (
    OrderCreated,
    OrderIn,
    OrderPricing,
    OrderStatusPublic,
    PriceTierOut,
)
from core.config import Settings
from core.pricing import parse_price_tiers

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post(
    "",
    status_code=201,
    response_model=OrderCreated,
    summary="Order an expert analysis of a parcel (guest checkout, bank transfer)",
    responses={
        404: {"description": "The parcel does not exist (`not_found`)"},
        422: {"description": "Form validation (`validation_error`)"},
        429: {"description": "Too many orders for this e-mail address today (`rate_limited`)"},
        503: {"description": "The planning database is unavailable"},
    },
)
async def create_order(
    payload: OrderIn, service: OrderServiceDep, response: Response
) -> OrderCreated:
    response.headers["Cache-Control"] = "no-store"
    return await service.create(payload)


@router.get(
    "/pricing",
    response_model=OrderPricing,
    summary="Order price tiers and turnaround (configuration; no database needed)",
)
async def order_pricing(
    settings: Annotated[Settings, Depends(get_settings_dep)], response: Response
) -> OrderPricing:
    response.headers["Cache-Control"] = "public, max-age=300"
    tiers = parse_price_tiers(settings.order_price_tiers)
    return OrderPricing(
        tiers=[PriceTierOut(up_to_m2=t.up_to_m2, price_eur=t.price_eur) for t in tiers],
        turnaround_business_days=settings.order_turnaround_business_days,
    )


@router.get(
    "/{reference}/status",
    response_model=OrderStatusPublic,
    summary="Public order status: status, location and turnaround only",
    responses={404: {"description": "No order with that reference (`not_found`)"}},
)
async def order_status(
    reference: Annotated[str, Path(min_length=8, max_length=40, pattern=r"^[A-Za-z0-9-]+$")],
    service: OrderServiceDep,
    response: Response,
) -> OrderStatusPublic:
    response.headers["Cache-Control"] = "no-store"
    return await service.public_status(reference)
