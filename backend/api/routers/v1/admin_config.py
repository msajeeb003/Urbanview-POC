"""Admin configuration (role ``admin``, every write audited): financial assumptions per zone, the
formula versions (read-only) and staff users. Assumptions are version histories: POST creates
version 1 (or the next version when the zone already has one), PUT creates the next version from
the newest row, DELETE retires the live version; earlier versions are read-only and listed with
``include_history``.
Assumptions are effective-dated (``core.assumptions``): a set applies from its
``effective_from`` (today or later) and the panel reads the one that applies today;
``/assumptions/batch`` saves several zones at once. Staff users have no passwords (magic-link
login); deactivating one revokes its sessions.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from api.deps import AdminConfigServiceDep, AdminPrincipal, StaffPrincipal
from api.schemas.admin_config import (
    AssumptionsBatchIn,
    AssumptionsBatchOut,
    AssumptionsIn,
    AssumptionsList,
    AssumptionsOut,
    AssumptionsUpdate,
    FormulaVersionOut,
    StaffMeOut,
    StaffUserIn,
    StaffUserList,
    StaffUserOut,
    StaffUserUpdate,
)


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(_no_store)])
Id = Annotated[int, Path(gt=0)]
RESPONSES = {
    401: {"description": "Missing or unknown bearer token (`unauthorized`)"},
    403: {"description": "The principal's role is not admin (`forbidden`)"},
    409: {
        "description": "Not the current version, duplicate e-mail, or a self-change (`conflict`)"
    },
    422: {"description": "Validation: low ≤ expected ≤ high, percentages, dates, references"},
}


# --- financial assumptions ------------------------------------------------------------------------


@router.get(
    "/assumptions",
    response_model=AssumptionsList,
    summary="Financial assumptions: the live and scheduled versions per zone, or the history",
)
async def list_assumptions(
    principal: AdminPrincipal,
    service: AdminConfigServiceDep,
    zone_id: Annotated[int | None, Query(gt=0)] = None,
    default_only: Annotated[
        bool, Query(description="Only the municipality-wide row (range factors)")
    ] = False,
    include_history: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AssumptionsList:
    return await service.list_assumptions(
        zone_id=zone_id,
        default_only=default_only,
        include_history=include_history,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/assumptions",
    status_code=201,
    response_model=AssumptionsOut,
    summary="Publish a new version of a zone's (or the default) financial assumptions",
    responses=RESPONSES,
)
async def create_assumptions(
    principal: AdminPrincipal, service: AdminConfigServiceDep, payload: AssumptionsIn
) -> AssumptionsOut:
    return await service.create_assumptions(principal, payload)


@router.post(
    "/assumptions/batch",
    status_code=201,
    response_model=AssumptionsBatchOut,
    summary="Save new sets for several zones at once (one transaction, all or nothing)",
    responses=RESPONSES,
)
async def create_assumptions_batch(
    principal: AdminPrincipal, service: AdminConfigServiceDep, payload: AssumptionsBatchIn
) -> AssumptionsBatchOut:
    return await service.create_assumptions_batch(principal, payload)


@router.get("/assumptions/{assumptions_id}", response_model=AssumptionsOut, responses=RESPONSES)
async def get_assumptions(
    principal: AdminPrincipal, service: AdminConfigServiceDep, assumptions_id: Id
) -> AssumptionsOut:
    return await service.get_assumptions(assumptions_id)


@router.put(
    "/assumptions/{assumptions_id}",
    status_code=201,
    response_model=AssumptionsOut,
    summary="Create the next version from the current one with the given changes",
    responses=RESPONSES,
)
async def update_assumptions(
    principal: AdminPrincipal,
    service: AdminConfigServiceDep,
    assumptions_id: Id,
    payload: AssumptionsUpdate,
) -> AssumptionsOut:
    return await service.update_assumptions(principal, assumptions_id, payload)


@router.delete(
    "/assumptions/{assumptions_id}",
    response_model=AssumptionsOut,
    summary="Retire the live version (the zone has no market figures until a later set)",
    responses=RESPONSES,
)
async def retire_assumptions(
    principal: AdminPrincipal, service: AdminConfigServiceDep, assumptions_id: Id
) -> AssumptionsOut:
    return await service.retire_assumptions(principal, assumptions_id)


# --- formula versions -----------------------------------------------------------------------------


@router.get(
    "/formulas",
    response_model=list[FormulaVersionOut],
    summary="Formula versions: the current one is what the engine runs and the panels state",
    responses=RESPONSES,
)
async def list_formulas(
    principal: AdminPrincipal, service: AdminConfigServiceDep
) -> list[FormulaVersionOut]:
    return await service.list_formulas()


# --- staff users ----------------------------------------------------------------------------------


@router.get("/users", response_model=StaffUserList, summary="Staff users with their open sessions")
async def list_users(principal: AdminPrincipal, service: AdminConfigServiceDep) -> StaffUserList:
    return await service.list_users()


@router.post(
    "/users",
    status_code=201,
    response_model=StaffUserOut,
    summary="Create a staff user (no password: login is a magic link)",
    responses=RESPONSES,
)
async def create_user(
    principal: AdminPrincipal, service: AdminConfigServiceDep, payload: StaffUserIn
) -> StaffUserOut:
    return await service.create_user(principal, payload)


# before /users/{user_id}: "me" is not an id
@router.get(
    "/users/me",
    response_model=StaffMeOut,
    summary="The signed-in staff member and role (every staff role)",
)
async def get_me(principal: StaffPrincipal, service: AdminConfigServiceDep) -> StaffMeOut:
    return await service.me(principal)


@router.get("/users/{user_id}", response_model=StaffUserOut, responses=RESPONSES)
async def get_user(
    principal: AdminPrincipal, service: AdminConfigServiceDep, user_id: Id
) -> StaffUserOut:
    return await service.get_user(user_id)


@router.patch(
    "/users/{user_id}",
    response_model=StaffUserOut,
    summary="Change role or name, deactivate (revokes sessions) or reactivate",
    responses=RESPONSES,
)
async def update_user(
    principal: AdminPrincipal,
    service: AdminConfigServiceDep,
    user_id: Id,
    payload: StaffUserUpdate,
) -> StaffUserOut:
    return await service.update_user(principal, user_id, payload)
