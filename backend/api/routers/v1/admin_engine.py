"""The calculation engine's proposals (role ``admin``): formulas and data inputs staff propose for
the client's review. Recording one writes an audit row and changes no calculation: the engine's
formulas are the shared package's (``FORMULA_VERSION``), validated by the client before launch."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from api.deps import AdminPrincipal, EngineProposalServiceDep
from api.schemas.engine import EngineProposalIn, EngineProposalList, EngineProposalOut


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin/engine", tags=["admin"], dependencies=[Depends(_no_store)])
RESPONSES = {
    401: {"description": "Missing or unknown bearer token (`unauthorized`)"},
    403: {"description": "The principal's role is not admin (`forbidden`)"},
    422: {"description": "A formula needs an expression, a data input what it provides"},
}


@router.get(
    "/proposals",
    response_model=EngineProposalList,
    summary="Proposed formulas and data inputs, oldest first",
    responses=RESPONSES,
)
async def list_proposals(
    principal: AdminPrincipal, service: EngineProposalServiceDep
) -> EngineProposalList:
    return await service.list_proposals()


@router.post(
    "/proposals",
    status_code=201,
    response_model=EngineProposalOut,
    summary="Record a proposed formula or data input (audited; the engine is unchanged)",
    responses=RESPONSES,
)
async def create_proposal(
    principal: AdminPrincipal, service: EngineProposalServiceDep, payload: EngineProposalIn
) -> EngineProposalOut:
    return await service.create_proposal(principal, payload)
