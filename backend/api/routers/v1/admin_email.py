"""The e-mail log (roles ``admin`` and ``reviewer``): every send with its recipient, template,
order / user id, provider message id and status; bounces reported here surface on the order.

- ``GET /v1/admin/email-log`` (filters ``order_id``, ``user_id``, ``status``, ``template``);
- ``GET /v1/admin/email-log/{id}``;
- ``POST /v1/admin/email-log/{id}/bounce {reason}``: marks a sent e-mail bounced (audited); the
  provider webhook of the chosen SMTP service will call the same service method.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from api.deps import EmailServiceDep, OrderManagerPrincipal
from api.schemas.email import BounceIn, EmailLogList, EmailLogOut, EmailStatus, EmailTemplate


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin/email-log", tags=["admin"], dependencies=[Depends(_no_store)])

Id = Annotated[int, Path(gt=0)]
RESPONSES = {
    401: {"description": "Missing or invalid bearer token"},
    403: {"description": "Role not allowed"},
    404: {"description": "No such entry"},
}


@router.get("", response_model=EmailLogList, summary="Sent e-mails", responses=RESPONSES)
async def list_email_log(
    principal: OrderManagerPrincipal,
    service: EmailServiceDep,
    order_id: Annotated[int | None, Query(gt=0)] = None,
    user_id: Annotated[int | None, Query(gt=0)] = None,
    status: Annotated[EmailStatus | None, Query()] = None,
    template: Annotated[EmailTemplate | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EmailLogList:
    return await service.list(
        order_id=order_id,
        user_id=user_id,
        status=status,
        template=template,
        limit=limit,
        offset=offset,
    )


@router.get("/{log_id}", response_model=EmailLogOut, responses=RESPONSES)
async def get_email_log(
    principal: OrderManagerPrincipal, service: EmailServiceDep, log_id: Id
) -> EmailLogOut:
    return await service.get(log_id)


@router.post(
    "/{log_id}/bounce",
    response_model=EmailLogOut,
    summary="Record a bounce",
    responses={**RESPONSES, 409: {"description": "Only a sent e-mail can bounce"}},
)
async def record_bounce(
    principal: OrderManagerPrincipal, service: EmailServiceDep, log_id: Id, payload: BounceIn
) -> EmailLogOut:
    return await service.mark_bounced(principal, log_id, payload.reason)
