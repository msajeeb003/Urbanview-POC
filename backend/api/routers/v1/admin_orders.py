"""Staff order management: the queue, payment receipt, expert assignment, status changes and the
report upload that delivers the order.

Admins and reviewers manage every order; an expert sees and delivers only the orders assigned to
them. Every change is an ``audit_log`` row; the guarded status flow answers 409 for anything
outside ``pending_payment → paid → in_progress → delivered`` (+ ``refunded`` from paid /
in_progress). Responses are ``Cache-Control: no-store``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Path, Query, Response, UploadFile

from api.deps import OrderManagerPrincipal, OrderServiceDep, OrderStaffPrincipal
from api.schemas.orders import (
    AssignIn,
    OrderList,
    OrderOut,
    OrderStatus,
    PaymentIn,
    StatusIn,
)


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin/orders", tags=["orders"], dependencies=[Depends(_no_store)])
Id = Annotated[int, Path(gt=0)]
RESPONSES = {
    401: {"description": "Missing or unknown bearer token (`unauthorized`)"},
    403: {"description": "Role not allowed, or the order is not assigned to you (`forbidden`)"},
    404: {"description": "No such order (`not_found`)"},
    409: {"description": "The status flow does not allow this change (`conflict`)"},
}


@router.get("", response_model=OrderList, summary="The order queue")
async def list_orders(
    principal: OrderStaffPrincipal,
    service: OrderServiceDep,
    status: Annotated[OrderStatus | None, Query()] = None,
    assignee_user_id: Annotated[int | None, Query(gt=0)] = None,
    search: Annotated[
        str | None, Query(max_length=100, description="Reference, e-mail, name, company, parcel")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrderList:
    return await service.list(
        principal,
        status=status,
        assignee_user_id=assignee_user_id,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.get("/{order_id}", response_model=OrderOut, responses=RESPONSES)
async def get_order(
    principal: OrderStaffPrincipal, service: OrderServiceDep, order_id: Id
) -> OrderOut:
    return await service.get(principal, order_id)


@router.patch(
    "/{order_id}/status",
    response_model=OrderOut,
    summary="Move the order along the guarded status flow",
    responses=RESPONSES,
)
async def set_status(
    principal: OrderManagerPrincipal, service: OrderServiceDep, order_id: Id, payload: StatusIn
) -> OrderOut:
    return await service.set_status(principal, order_id, payload.status, payload.note)


@router.post(
    "/{order_id}/payment",
    response_model=OrderOut,
    summary="Record a bank transfer: received (→ paid), not received, or refunded",
    responses=RESPONSES,
)
async def record_payment(
    principal: OrderManagerPrincipal, service: OrderServiceDep, order_id: Id, payload: PaymentIn
) -> OrderOut:
    return await service.record_payment(principal, order_id, payload)


@router.post(
    "/{order_id}/assign",
    response_model=OrderOut,
    summary="Assign the order to an expert (a paid order moves to in_progress)",
    responses=RESPONSES,
)
async def assign_order(
    principal: OrderManagerPrincipal, service: OrderServiceDep, order_id: Id, payload: AssignIn
) -> OrderOut:
    return await service.assign(principal, order_id, payload)


@router.post(
    "/{order_id}/report",
    response_model=OrderOut,
    summary="Upload the expert's report (PDF): delivers the order and e-mails the download link",
    responses={
        **RESPONSES,
        413: {"description": "Larger than ADMIN_UPLOAD_MAX_MB (`payload_too_large`)"},
        422: {"description": "Not a PDF"},
    },
)
async def upload_report(
    principal: OrderStaffPrincipal,
    service: OrderServiceDep,
    order_id: Id,
    file: Annotated[UploadFile, File(description="The finished report, PDF")],
    note: Annotated[str | None, Form(max_length=2000)] = None,
) -> OrderOut:
    return await service.upload_report(principal, order_id, file, note)
