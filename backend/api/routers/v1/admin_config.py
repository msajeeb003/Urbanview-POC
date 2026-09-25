"""Admin configuration (role ``admin``, every write audited): financial assumptions per zone,
zone parameter sets, staff users. Assumptions and parameter sets are version histories: POST
creates version 1 (or the next version when the zone already has one), PUT creates the next
version from the current row, DELETE retires the current row; earlier versions are read-only and
listed with ``include_history``. Staff users have no passwords (magic-link login); deactivating
one revokes its sessions.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from api.deps import AdminConfigServiceDep, AdminPrincipal
from api.schemas.admin_config import (
    AssumptionsIn,
    AssumptionsList,
    AssumptionsOut,
    AssumptionsUpdate,
    StaffUserIn,
    StaffUserList,
    StaffUserOut,
    StaffUserUpdate,
    ZoneParametersIn,
    ZoneParametersList,
    ZoneParametersOut,
    ZoneParametersUpdate,
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
    summary="Financial assumptions: current version per zone (and the default), or the history",
)
async def list_assumptions(
    principal: AdminPrincipal,
    service: AdminConfigServiceDep,
    zone_id: Annotated[int | None, Query(gt=0)] = None,
    default_only: Annotated[bool, Query(description="Only the municipality-wide default")] = False,
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
    summary="Retire the current version (the panel falls back to the municipality default)",
    responses=RESPONSES,
)
async def retire_assumptions(
    principal: AdminPrincipal, service: AdminConfigServiceDep, assumptions_id: Id
) -> AssumptionsOut:
    return await service.retire_assumptions(principal, assumptions_id)


# --- zone parameter sets --------------------------------------------------------------------------


@router.get(
    "/zone-parameters",
    response_model=ZoneParametersList,
    summary="Typical planning values per zone: current versions, or the history",
)
async def list_zone_parameters(
    principal: AdminPrincipal,
    service: AdminConfigServiceDep,
    zone_id: Annotated[int | None, Query(gt=0)] = None,
    include_history: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ZoneParametersList:
    return await service.list_zone_parameters(
        zone_id=zone_id, include_history=include_history, limit=limit, offset=offset
    )


@router.post(
    "/zone-parameters",
    status_code=201,
    response_model=ZoneParametersOut,
    summary="Publish a new version of a zone's typical planning values",
    responses=RESPONSES,
)
async def create_zone_parameters(
    principal: AdminPrincipal, service: AdminConfigServiceDep, payload: ZoneParametersIn
) -> ZoneParametersOut:
    return await service.create_zone_parameters(principal, payload)


@router.get(
    "/zone-parameters/{parameters_id}", response_model=ZoneParametersOut, responses=RESPONSES
)
async def get_zone_parameters(
    principal: AdminPrincipal, service: AdminConfigServiceDep, parameters_id: Id
) -> ZoneParametersOut:
    return await service.get_zone_parameters(parameters_id)


@router.put(
    "/zone-parameters/{parameters_id}",
    status_code=201,
    response_model=ZoneParametersOut,
    summary="Create the next version from the current one with the given changes",
    responses=RESPONSES,
)
async def update_zone_parameters(
    principal: AdminPrincipal,
    service: AdminConfigServiceDep,
    parameters_id: Id,
    payload: ZoneParametersUpdate,
) -> ZoneParametersOut:
    return await service.update_zone_parameters(principal, parameters_id, payload)


@router.delete(
    "/zone-parameters/{parameters_id}",
    response_model=ZoneParametersOut,
    summary="Retire the current version (the zone panel shows no typical values)",
    responses=RESPONSES,
)
async def retire_zone_parameters(
    principal: AdminPrincipal, service: AdminConfigServiceDep, parameters_id: Id
) -> ZoneParametersOut:
    return await service.retire_zone_parameters(principal, parameters_id)


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
