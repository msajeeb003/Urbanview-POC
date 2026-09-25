"""Batch ingest of client analytics events: ``POST /v1/events``.

The prototype is a validation instrument: the 13 product events (``api.schemas.analytics``) are
accepted in batches of up to 100, validated strictly (an unknown name, a malformed id, an
oversized, nested or personal ``properties`` object rejects the whole batch with 422) and stored
in ``analytics_events``. Retried batches are safe when events carry an ``event_id``. Names,
emails and IPs are never stored.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.deps import AnalyticsServiceDep
from api.schemas.analytics import EventBatch, IngestResult

router = APIRouter(prefix="/events", tags=["analytics"])


@router.post(
    "",
    status_code=202,
    response_model=IngestResult,
    summary="Ingest a batch of anonymous client analytics events",
    response_description="Accepted: every event in the batch was valid and stored (or a retry)",
    responses={
        422: {"description": "The batch was rejected as a whole (`validation_error`)"},
        503: {"description": "The events database is unavailable (`service_unavailable`)"},
    },
)
async def ingest_events(batch: EventBatch, service: AnalyticsServiceDep) -> IngestResult:
    return await service.ingest(batch)
