"""Staff login by magic link (public routes, rate-limited like everything else).

- ``POST /v1/auth/magic-link {email}``: always 202 with the same neutral message; an active
  staff address gets a single-use login link by e-mail (``magic_link`` template);
- ``POST /v1/auth/magic-link/exchange {token}``: consumes the link, answers the session bearer
  token for the staff routes (401 for an unknown, used or expired link);
- ``POST /v1/auth/sign-out`` (bearer): revokes that session; 204 whatever the token.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response

from api.deps import MagicLinkServiceDep
from api.schemas.email import MagicLinkAccepted, MagicLinkExchange, MagicLinkRequest, SessionOut
from core.auth import bearer_token


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


@router.post(
    "/sign-out",
    status_code=204,
    summary="End the staff session of the bearer token",
    response_class=Response,
)
async def sign_out(
    service: MagicLinkServiceDep, authorization: Annotated[str | None, Header()] = None
) -> Response:
    await service.sign_out(bearer_token(authorization))
    return Response(status_code=204)
