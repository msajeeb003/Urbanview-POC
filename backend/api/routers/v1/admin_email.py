"""The e-mail log (role ``admin``): every send with its recipient, template, order / user id,
provider message id and status.

- ``GET /v1/admin/email-log`` (filters ``order_id``, ``user_id``, ``status``, ``template``);
- ``GET /v1/admin/email-log/{id}``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from api.deps import EmailServiceDep, OrderManagerPrincipal
from api.schemas.email import EmailLogList, EmailLogOut, EmailStatus, EmailTemplate


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
