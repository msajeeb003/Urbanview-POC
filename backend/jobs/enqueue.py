"""Enqueue a job from the API: one ``pipeline_jobs`` row, one Celery message, idempotent.

``JOB_TYPES`` maps a job type to its Celery task, queue and family (``kind``). ``enqueue_job``
returns the active job for the same ``dedupe_key`` (queued / running / retrying) instead of
starting another; the key defaults to ``type:target_type:target_id`` plus, for file-based work,
the file's SHA-256, so the same content is never processed twice. The message is sent after the
row is committed; a broker failure marks the job failed and raises ``ServiceUnavailableError``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from core.errors import ServiceUnavailableError

log = logging.getLogger("urbanview.jobs")


@dataclass(frozen=True, slots=True)
class JobType:
    name: str
    task: str
    queue: str
    kind: str


JOB_TYPES: dict[str, JobType] = {
    t.name: t
    for t in (
        JobType(
            "extract_document", "jobs.tasks.extraction.extract_document", "extraction", "extract"
        ),
        JobType(
            "preprocess_file", "jobs.tasks.extraction.preprocess_file", "extraction", "extract"
        ),
        JobType("process_geometry", "jobs.tasks.ingestion.process_geometry", "geo", "geo"),
        JobType("publish_approved", "jobs.tasks.publish.publish_approved", "publish", "publish"),
        JobType("send_email", "jobs.tasks.email.send_email", "email", "email"),
        JobType(
            "import_market_data", "jobs.tasks.market.import_market_data", "extraction", "extract"
        ),
    )
}
QUEUES: tuple[str, ...] = ("default", "extraction", "geo", "publish", "email")


class JobDispatcher(Protocol):
    def enqueue(self, job_type: str, job_id: int, municipality_id: str) -> str: ...


class CeleryDispatcher:
    """Delivers the job's task. Registered tasks go through ``apply_async`` (which honours
    ``task_always_eager``); an unregistered name falls back to ``send_task``. Task modules stay
    import-light (heavy libraries are imported inside the bodies) because the API imports them."""

    def enqueue(self, job_type: str, job_id: int, municipality_id: str) -> str:
        try:
            spec = JOB_TYPES[job_type]
        except KeyError:
            raise ValueError(f"unknown job type {job_type!r}") from None
        from jobs.celery_app import celery_app

        import_tasks()
        task = celery_app.tasks.get(spec.task)
        if task is not None:
            result = task.apply_async(args=[job_id, municipality_id], queue=spec.queue)
        else:
            result = celery_app.send_task(
                spec.task, args=[job_id, municipality_id], queue=spec.queue
            )
        return str(result.id)


def import_tasks() -> None:
    """Register the task modules in this process (a worker does it through ``include``)."""
    import jobs.tasks.email  # noqa: F401
    import jobs.tasks.extraction  # noqa: F401
    import jobs.tasks.ingestion  # noqa: F401
    import jobs.tasks.market  # noqa: F401
    import jobs.tasks.publish  # noqa: F401


def dedupe_key(
    job_type: str,
    target_type: str | None,
    target_id: int | None,
    checksum: str | None = None,
) -> str:
    parts = [job_type, target_type or "-", str(target_id) if target_id is not None else "-"]
    if checksum:
        parts.append(f"sha256:{checksum}")
    return ":".join(parts)


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job_id: int
    created: bool
    task_id: str | None


ACTIVE_BY_KEY_SQL = text(
    """
    SELECT id, celery_task_id FROM pipeline_jobs
    WHERE municipality_id = :m AND dedupe_key = :key
      AND status IN ('queued', 'running', 'retrying')
    ORDER BY id DESC LIMIT 1
    """
)
INSERT_JOB_SQL = text(
    """
    INSERT INTO pipeline_jobs (municipality_id, kind, type, queue, status, document_id, file_id,
                               target_type, target_id, payload, dedupe_key, max_attempts,
                               requested_by, requested_by_user_id)
    VALUES (:m, :kind, :type, :queue, 'queued', :document_id, :file_id, :target_type,
            :target_id, CAST(:payload AS jsonb), :dedupe_key, :max_attempts, :requested_by,
            :requested_by_user_id)
    RETURNING id
    """
)
SET_TASK_SQL = text(
    "UPDATE pipeline_jobs SET celery_task_id = :task_id WHERE id = :id AND celery_task_id IS NULL"
)
DISPATCH_FAILED_SQL = text(
    "UPDATE pipeline_jobs SET status = 'failed', error = :error, finished_at = :at WHERE id = :id"
)

CreatedHook = Callable[[AsyncSession, int], Awaitable[None]]
DispatchFailedHook = Callable[[AsyncSession, int, str], Awaitable[None]]


async def _active(session: AsyncSession, municipality_id: str, key: str) -> EnqueueResult | None:
    row = (
        (await session.execute(ACTIVE_BY_KEY_SQL, {"m": municipality_id, "key": key}))
        .mappings()
        .first()
    )
    if row is None:
        return None
    return EnqueueResult(job_id=int(row["id"]), created=False, task_id=row["celery_task_id"])


async def enqueue_job(
    session_factory: async_sessionmaker[AsyncSession],
    dispatcher: JobDispatcher,
    *,
    municipality_id: str,
    job_type: str,
    payload: dict[str, Any] | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    document_id: int | None = None,
    file_id: int | None = None,
    checksum: str | None = None,
    key: str | None = None,
    max_attempts: int = 3,
    requested_by: str = "system",
    requested_by_user_id: int | None = None,
    on_created: CreatedHook | None = None,
    on_dispatch_failed: DispatchFailedHook | None = None,
) -> EnqueueResult:
    """Create and dispatch a job, or return the active one for the same key.

    ``on_created(session, job_id)`` / ``on_dispatch_failed(session, job_id, error)`` run inside
    the respective transactions (the callers write their audit rows there).
    """
    spec = JOB_TYPES[job_type]
    key = key or dedupe_key(job_type, target_type, target_id, checksum)
    async with session_factory() as session:
        existing = await _active(session, municipality_id, key)
        if existing is not None:
            return existing
        try:
            job_id = int(
                (
                    await session.execute(
                        INSERT_JOB_SQL,
                        {
                            "m": municipality_id,
                            "kind": spec.kind,
                            "type": spec.name,
                            "queue": spec.queue,
                            "document_id": document_id,
                            "file_id": file_id,
                            "target_type": target_type,
                            "target_id": target_id,
                            "payload": json.dumps(payload or {}, default=str),
                            "dedupe_key": key,
                            "max_attempts": max_attempts,
                            "requested_by": requested_by,
                            "requested_by_user_id": requested_by_user_id,
                        },
                    )
                ).scalar_one()
            )
        except IntegrityError:  # lost the race with an identical enqueue
            await session.rollback()
            existing = await _active(session, municipality_id, key)
            if existing is None:
                raise
            return existing
        if on_created is not None:
            await on_created(session, job_id)
        await session.commit()

    try:
        task_id = await run_in_threadpool(dispatcher.enqueue, spec.name, job_id, municipality_id)
    except Exception as exc:  # noqa: BLE001 - broker down or misconfigured: the row says so
        error = f"queue unavailable: {type(exc).__name__}: {exc}"
        log.warning("could not dispatch job %s: %s", job_id, error, extra={"job_id": job_id})
        async with session_factory() as session:
            await session.execute(
                DISPATCH_FAILED_SQL, {"id": job_id, "error": error, "at": datetime.now(UTC)}
            )
            if on_dispatch_failed is not None:
                await on_dispatch_failed(session, job_id, error)
            await session.commit()
        raise ServiceUnavailableError(
            "The job queue is unavailable; the job was recorded as failed",
            details={"job_id": job_id, "status_url": f"/v1/admin/jobs/{job_id}"},
        ) from exc
    async with session_factory() as session:
        await session.execute(SET_TASK_SQL, {"id": job_id, "task_id": task_id})
        await session.commit()
    return EnqueueResult(job_id=job_id, created=True, task_id=task_id)


async def redispatch(
    session_factory: async_sessionmaker[AsyncSession],
    dispatcher: JobDispatcher,
    *,
    municipality_id: str,
    job_id: int,
    job_type: str,
) -> str:
    """Send the message for an existing, re-queued job (manual retry)."""
    try:
        task_id = await run_in_threadpool(dispatcher.enqueue, job_type, job_id, municipality_id)
    except Exception as exc:  # noqa: BLE001
        error = f"queue unavailable: {type(exc).__name__}: {exc}"
        async with session_factory() as session:
            await session.execute(
                DISPATCH_FAILED_SQL, {"id": job_id, "error": error, "at": datetime.now(UTC)}
            )
            await session.commit()
        raise ServiceUnavailableError(
            "The job queue is unavailable; the job was recorded as failed",
            details={"job_id": job_id, "status_url": f"/v1/admin/jobs/{job_id}"},
        ) from exc
    async with session_factory() as session:
        await session.execute(
            text("UPDATE pipeline_jobs SET celery_task_id = :task_id WHERE id = :id"),
            {"id": job_id, "task_id": task_id},
        )
        await session.commit()
    return task_id
