"""Job status API over ``pipeline_jobs`` (``/v1/admin/jobs``): listing with filters, one job,
manual retry and the cost summary per target.

Reads the rows the base task writes (``jobs/base.py``), one statement per call. The only write
is the retry: a failed / cancelled job goes back to ``queued`` with its attempt counter reset,
``manual_retries`` incremented, an audit row, and its message re-sent through the dispatcher.
The idempotency index still applies: while another job for the same key is active the retry is
refused (409).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.admin import JobCostRow, JobCostSummary, JobList, JobOut
from api.services.audit import write_audit
from core.auth import Principal
from core.errors import ConflictError, NotFoundError
from jobs.enqueue import ACTIVE_BY_KEY_SQL, JobDispatcher, redispatch

JOB_STATUS_PATH = "/v1/admin/jobs/{id}"
RETRYABLE_STATUSES: tuple[str, ...] = ("failed", "cancelled")

JOB_JSON = """jsonb_build_object(
    'id', j.id, 'kind', j.kind, 'type', j.type, 'queue', j.queue, 'status', j.status,
    'document_id', j.document_id, 'file_id', j.file_id, 'target_type', j.target_type,
    'target_id', j.target_id, 'payload', j.payload, 'dedupe_key', j.dedupe_key,
    'attempts', j.attempts, 'max_attempts', j.max_attempts, 'manual_retries', j.manual_retries,
    'next_retry_at', j.next_retry_at, 'celery_task_id', j.celery_task_id,
    'requested_by', j.requested_by, 'requested_at', j.requested_at, 'started_at', j.started_at,
    'finished_at', j.finished_at, 'error', j.error, 'result', j.result,
    'progress', j.progress,
    'cost', jsonb_build_object(
        'wall_time_ms', j.wall_time_ms, 'llm_model', j.llm_model,
        'llm_tokens_in', j.llm_tokens_in, 'llm_tokens_out', j.llm_tokens_out,
        'estimated_cost_eur', j.estimated_cost_eur))"""

JOB_SQL = text(
    f"SELECT {JOB_JSON} AS job FROM pipeline_jobs j WHERE j.id = :id AND j.municipality_id = :m"
)
LIST_SQL = text(
    f"""
    SELECT {JOB_JSON} AS job, count(*) OVER () AS total
    FROM pipeline_jobs j
    WHERE j.municipality_id = :m
      AND (CAST(:type AS text) IS NULL OR j.type = CAST(:type AS text))
      AND (CAST(:status AS text) IS NULL OR j.status = CAST(:status AS text))
      AND (CAST(:target_type AS text) IS NULL
           OR (j.target_type = CAST(:target_type AS text)
               AND j.target_id = CAST(:target_id AS bigint)))
      AND (CAST(:document_id AS bigint) IS NULL OR j.document_id = CAST(:document_id AS bigint))
      AND (CAST(:file_id AS bigint) IS NULL OR j.file_id = CAST(:file_id AS bigint))
    ORDER BY j.requested_at DESC, j.id DESC
    LIMIT :limit OFFSET :offset
    """
)
JOB_FOR_RETRY_SQL = text(
    """
    SELECT id, type, status, dedupe_key, attempts, manual_retries
    FROM pipeline_jobs WHERE id = :id AND municipality_id = :m
    FOR UPDATE
    """
)
REQUEUE_SQL = text(
    """
    UPDATE pipeline_jobs
    SET status = 'queued', attempts = 0, manual_retries = manual_retries + 1, error = NULL,
        started_at = NULL, finished_at = NULL, next_retry_at = NULL, celery_task_id = NULL,
        result = NULL, wall_time_ms = NULL, progress = NULL
    WHERE id = :id AND municipality_id = :m
    """
)
COSTS_SQL = text(
    """
    SELECT j.target_type, j.target_id, count(*) AS jobs,
           count(*) FILTER (WHERE j.status = 'succeeded') AS succeeded,
           count(*) FILTER (WHERE j.status = 'failed') AS failed,
           COALESCE(sum(j.llm_tokens_in), 0) AS llm_tokens_in,
           COALESCE(sum(j.llm_tokens_out), 0) AS llm_tokens_out,
           COALESCE(sum(j.estimated_cost_eur), 0) AS estimated_cost_eur,
           COALESCE(sum(j.wall_time_ms), 0) AS wall_time_ms,
           max(j.finished_at) AS last_finished_at
    FROM pipeline_jobs j
    WHERE j.municipality_id = :m
      AND (CAST(:type AS text) IS NULL OR j.type = CAST(:type AS text))
      AND (CAST(:target_type AS text) IS NULL
           OR (j.target_type = CAST(:target_type AS text)
               AND j.target_id = CAST(:target_id AS bigint)))
    GROUP BY j.target_type, j.target_id
    ORDER BY estimated_cost_eur DESC, jobs DESC, j.target_type, j.target_id
    LIMIT :limit
    """
)


def parse_target(target: str | None) -> tuple[str | None, int | None]:
    """``"document:12"`` -> ``("document", 12)``; the router validates the shape."""
    if not target:
        return None, None
    target_type, _, target_id = target.partition(":")
    return target_type, int(target_id)


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def job_out(job: Mapping[str, Any]) -> JobOut:
    data = dict(job)
    for key in ("requested_at", "started_at", "finished_at", "next_retry_at"):
        data[key] = _utc(data.get(key))
    return JobOut(**data, status_url=JOB_STATUS_PATH.format(id=job["id"]))


class JobService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        dispatcher: JobDispatcher,
        municipality_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.dispatcher = dispatcher
        self.municipality_id = municipality_id
        self.clock = clock

    async def get_job(self, job_id: int) -> JobOut:
        async with self.session_factory() as session:
            job = (
                await session.execute(JOB_SQL, {"m": self.municipality_id, "id": job_id})
            ).scalar_one_or_none()
        if job is None:
            raise NotFoundError(f"No job with id {job_id}", details={"job_id": job_id})
        return job_out(job)

    async def list_jobs(
        self,
        *,
        type: str | None = None,
        status: str | None = None,
        target: str | None = None,
        document_id: int | None = None,
        file_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JobList:
        target_type, target_id = parse_target(target)
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        LIST_SQL,
                        {
                            "m": self.municipality_id,
                            "type": type,
                            "status": status,
                            "target_type": target_type,
                            "target_id": target_id,
                            "document_id": document_id,
                            "file_id": file_id,
                            "limit": limit,
                            "offset": offset,
                        },
                    )
                )
                .mappings()
                .all()
            )
        return JobList(
            items=[job_out(r["job"]) for r in rows],
            total=int(rows[0]["total"]) if rows else 0,
            limit=limit,
            offset=offset,
        )

    async def retry(self, principal: Principal, job_id: int) -> JobOut:
        params = {"m": self.municipality_id, "id": job_id}
        async with self.session_factory() as session:
            row = (await session.execute(JOB_FOR_RETRY_SQL, params)).mappings().first()
            if row is None:
                raise NotFoundError(f"No job with id {job_id}", details={"job_id": job_id})
            if row["status"] not in RETRYABLE_STATUSES:
                raise ConflictError(
                    f"Job {job_id} is {row['status']}; only failed or cancelled jobs can be "
                    "retried",
                    details={"job_id": job_id, "status": row["status"]},
                )
            if row["dedupe_key"]:
                active = (
                    (
                        await session.execute(
                            ACTIVE_BY_KEY_SQL,
                            {"m": self.municipality_id, "key": row["dedupe_key"]},
                        )
                    )
                    .mappings()
                    .first()
                )
                if active is not None:
                    raise ConflictError(
                        "Another job for the same target is already queued or running",
                        details={"job_id": job_id, "active_job_id": int(active["id"])},
                    )
            try:
                await session.execute(REQUEUE_SQL, params)
            except IntegrityError as exc:  # lost the race with an identical enqueue
                await session.rollback()
                raise ConflictError(
                    "Another job for the same target is already queued or running",
                    details={"job_id": job_id},
                ) from exc
            await write_audit(
                session,
                municipality_id=self.municipality_id,
                principal=principal,
                action="job.retry",
                entity_type="pipeline_job",
                entity_id=job_id,
                details={"type": row["type"], "manual_retries": int(row["manual_retries"]) + 1},
                before={"status": row["status"], "attempts": int(row["attempts"])},
                after={"status": "queued", "attempts": 0},
            )
            await session.commit()
        await redispatch(
            self.session_factory,
            self.dispatcher,
            municipality_id=self.municipality_id,
            job_id=job_id,
            job_type=row["type"],
        )
        return await self.get_job(job_id)

    async def costs(
        self, *, target: str | None = None, type: str | None = None, limit: int = 100
    ) -> JobCostSummary:
        target_type, target_id = parse_target(target)
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        COSTS_SQL,
                        {
                            "m": self.municipality_id,
                            "type": type,
                            "target_type": target_type,
                            "target_id": target_id,
                            "limit": limit,
                        },
                    )
                )
                .mappings()
                .all()
            )
        out = [
            JobCostRow(
                target_type=r["target_type"],
                target_id=r["target_id"],
                jobs=int(r["jobs"]),
                succeeded=int(r["succeeded"]),
                failed=int(r["failed"]),
                llm_tokens_in=int(r["llm_tokens_in"]),
                llm_tokens_out=int(r["llm_tokens_out"]),
                estimated_cost_eur=round(float(r["estimated_cost_eur"]), 4),
                wall_time_ms=int(r["wall_time_ms"]),
                last_finished_at=_utc(r["last_finished_at"]),
            )
            for r in rows
        ]
        return JobCostSummary(
            rows=out,
            total_jobs=sum(r.jobs for r in out),
            total_llm_tokens_in=sum(r.llm_tokens_in for r in out),
            total_llm_tokens_out=sum(r.llm_tokens_out for r in out),
            total_estimated_cost_eur=round(sum(r.estimated_cost_eur for r in out), 4),
            total_wall_time_ms=sum(r.wall_time_ms for r in out),
        )
