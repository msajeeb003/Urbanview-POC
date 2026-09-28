"""The admin console's AI extraction page (role ``admin``): the Anthropic API key (saved
encrypted, write-only), a connection test the worker runs, the model settings, spend and a
readiness checklist (``api.services.ai_settings``).

The key travels only in the PUT body; it is never returned, logged or audited, and validation
errors on that route never echo the input (``x-redact-input``, ``core.errors``)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from api.deps import AdminPrincipal, AiSettingsServiceDep
from api.schemas.admin import JobOut
from api.schemas.ai_settings import AiKeyIn, AiStatusOut
from core.errors import REDACT_INPUT_EXTRA


async def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin/ai", tags=["admin"], dependencies=[Depends(_no_store)])
RESPONSES = {
    401: {"description": "Missing or unknown bearer token (`unauthorized`)"},
    403: {"description": "The principal's role is not admin (`forbidden`)"},
    503: {"description": "No planning database (`service_unavailable`)"},
}


@router.get(
    "",
    response_model=AiStatusOut,
    summary="AI extraction status: key, model, last connection test, worker, spend, readiness",
    responses=RESPONSES,
)
async def get_status(principal: AdminPrincipal, service: AiSettingsServiceDep) -> AiStatusOut:
    return await service.status()


@router.put(
    "/key",
    response_model=AiStatusOut,
    summary="Save the Anthropic API key (encrypted, write-only; queues a connection test)",
    openapi_extra={REDACT_INPUT_EXTRA: True},
    responses={
        **RESPONSES,
        409: {
            "description": "SECRETS_ENCRYPTION_KEY is not set on the server "
            "(`encryption_key_missing`); nothing was stored"
        },
        422: {"description": "Not an Anthropic API key (`validation_error`; never echoes it)"},
    },
)
async def set_key(
    principal: AdminPrincipal, service: AiSettingsServiceDep, payload: AiKeyIn
) -> AiStatusOut:
    return await service.set_key(principal, payload)


@router.delete(
    "/key",
    response_model=AiStatusOut,
    summary="Remove the saved key",
    responses={
        **RESPONSES,
        404: {"description": "No key is saved in this console (`not_found`)"},
    },
)
async def remove_key(principal: AdminPrincipal, service: AiSettingsServiceDep) -> AiStatusOut:
    return await service.remove_key(principal)


@router.post(
    "/check",
    status_code=202,
    response_model=JobOut,
    summary="Test the connection (the worker calls the Messages API with the resolved key)",
    responses={
        **RESPONSES,
        200: {"description": "A test for this key is already queued or running"},
        503: {
            "description": "The job queue is unavailable (`service_unavailable`, details "
            "`job_id`, `status_url`): the test was recorded as failed; or no planning database"
        },
    },
)
async def check(
    principal: AdminPrincipal, service: AiSettingsServiceDep, response: Response
) -> JobOut:
    job, created = await service.check(principal)
    response.status_code = 202 if created else 200
    return job
