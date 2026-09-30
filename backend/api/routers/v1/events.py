"""Batch ingest of client analytics events: ``POST /v1/events``.

The prototype is a validation instrument: the 13 product events (``core.models.analytics``) are
accepted in batches of up to 100 and validated row by row (an unknown name, a malformed id, an
oversized, nested or personal ``properties`` object rejects that row, reported by its index,
while the valid rows are stored), in ``analytics_events``, which the database keeps append-only
(migration 0030). A malformed batch, or one whose every row is malformed, is a 422. Retried
batches are safe when events carry an ``event_id``. Names, emails and IPs are never stored.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.exceptions import RequestValidationError

from api.deps import AnalyticsServiceDep
from api.schemas.analytics import EventBatch, IngestResult

router = APIRouter(prefix="/events", tags=["analytics"])


@router.post(
    "",
    status_code=202,
    response_model=IngestResult,
    summary="Ingest a batch of anonymous client analytics events",
    response_description="Accepted: the valid rows were stored (or were retries); malformed rows "
    "are listed in `rejected`",
    responses={
        422: {
            "description": "The batch is malformed, or every row is (`validation_error`, one "
            "problem per row and field)"
        },
        503: {"description": "The events database is unavailable (`service_unavailable`)"},
    },
)
async def ingest_events(batch: EventBatch, service: AnalyticsServiceDep) -> IngestResult:
    if batch.rejected and not batch.events:
        raise RequestValidationError(
            [
                {
                    "type": problem.type,
                    "loc": ("body", "events", row.index, *problem.loc),
                    "msg": problem.msg,
                }
                for row in batch.rejected
                for problem in row.problems
            ]
        )
    return await service.ingest(batch)
