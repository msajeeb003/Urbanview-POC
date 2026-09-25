"""Staff login by magic link (public routes, rate-limited like everything else).

- ``POST /v1/auth/magic-link {email}``: always 202 with the same neutral message; an active
  staff address gets a single-use login link by e-mail (``magic_link`` template);
- ``POST /v1/auth/magic-link/exchange {token}``: consumes the link, answers the session bearer
  token for the staff routes (401 for an unknown, used or expired link).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from api.deps import MagicLinkServiceDep
from api.schemas.email import MagicLinkAccepted, MagicLinkExchange, MagicLinkRequest, SessionOut


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(_no_store)])


@router.post(
    "/magic-link",
    response_model=MagicLinkAccepted,
    status_code=202,
    summary="Request a login link",
    responses={503: {"description": "The staff database is not configured"}},
)
async def request_magic_link(
    payload: MagicLinkRequest, service: MagicLinkServiceDep
) -> MagicLinkAccepted:
    return await service.request(payload.email)


@router.post(
    "/magic-link/exchange",
    response_model=SessionOut,
    summary="Exchange a login link for a session",
    responses={401: {"description": "Invalid, expired or already used link"}},
)
async def exchange_magic_link(
    payload: MagicLinkExchange, service: MagicLinkServiceDep
) -> SessionOut:
    return await service.exchange(payload.token)
