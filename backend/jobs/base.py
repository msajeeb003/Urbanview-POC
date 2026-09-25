"""One job system for every long-running task: a base Celery task with a database-backed
lifecycle, retries with exponential backoff, and cost recording.

A job is a ``pipeline_jobs`` row created by the API (``jobs.enqueue``) and delivered to a worker
as ``(job_id, municipality_id)``. :class:`JobTask.execute` wraps the task body
(``work(job) -> JobResult | dict | None``) with :func:`run_job_async`:

- ``queued → running`` (attempt counted, start time, Celery task id) → the body runs;
- success: ``succeeded`` with the result, the wall time and the LLM cost the body reported;
- a transient error (:class:`TransientError`, timeouts, connection errors, HTTP 429 →
  :class:`RateLimited`) while attempts remain: ``retrying`` with ``next_retry_at`` and a
  re-delivery after ``base × 2^(attempt-1)`` seconds (capped); the last transient failure and any
  other exception: ``failed`` with the error message (a hard failure is never retried);
- a re-delivered message for a job that already finished is ignored (``skipped``).

Workers are synchronous and the database driver is async: the lifecycle runs on a private event
loop (:func:`run_sync`). The store is swappable (:class:`SqlJobStore` in workers,
:class:`MemoryJobStore` in eager tests) through :func:`configure_job_store`; the retry policy
through :func:`configure_retry_policy` (defaults from the ``JOB_RETRY_*`` settings).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any, Protocol

from celery import Task
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

log = logging.getLogger("urbanview.jobs")

ACTIVE_STATUSES: tuple[str, ...] = ("queued", "running", "retrying")
FINAL_STATUSES: tuple[str, ...] = ("succeeded", "failed", "cancelled")


class TransientError(Exception):
    """Retryable: the next attempt may succeed (LLM timeout, provider outage, 429)."""


class RateLimited(TransientError):
    """HTTP 429 from a provider."""


TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TransientError,
    TimeoutError,
    ConnectionError,
)


@dataclass(frozen=True, slots=True)
class JobCost:
    llm_model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    estimated_cost_eur: float | None = None


@dataclass(frozen=True, slots=True)
class JobResult:
    result: dict[str, Any] | None = None
    cost: JobCost | None = None


@dataclass(slots=True)
class JobContext:
    """What a task body sees: the row's identity, input and attempt counter."""

    id: int
    municipality_id: str
    type: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    target_type: str | None = None
    target_id: int | None = None
    document_id: int | None = None
    file_id: int | None = None
    attempts: int = 0
    max_attempts: int = 3
    result: dict[str, Any] | None = None
    # set by the lifecycle: an async body reports per-step progress through it
    report: Callable[[dict[str, Any]], Awaitable[None]] | None = None


@dataclass(frozen=True, slots=True)
class JobOutcome:
    job_id: int
    status: str  # succeeded | failed | retrying | skipped
    error: str | None = None
    retry_in_seconds: int | None = None
    attempts: int = 0
    result: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AsyncJobStore(Protocol):
    async def load(self, job_id: int, municipality_id: str) -> JobContext | None: ...

    async def mark_running(
        self, job_id: int, municipality_id: str, task_id: str | None
    ) -> JobContext | None: ...

    async def mark_succeeded(
        self,
        job_id: int,
        municipality_id: str,
        *,
        result: dict[str, Any] | None,
        cost: JobCost | None,
        wall_time_ms: int,
    ) -> None: ...

    async def mark_retrying(
        self,
        job_id: int,
        municipality_id: str,
        *,
        error: str,
        next_retry_at: datetime,
        wall_time_ms: int,
    ) -> None: ...

    async def mark_failed(
        self, job_id: int, municipality_id: str, *, error: str, wall_time_ms: int
    ) -> None: ...

    async def set_progress(
        self, job_id: int, municipality_id: str, progress: dict[str, Any]
    ) -> None: ...


def backoff_seconds(attempt: int, base_seconds: int, max_seconds: int) -> int:
    """Exponential ``base × 2^(attempt-1)``, capped at ``max_seconds``; attempt 1 waits ``base``."""
    return int(min(max_seconds, base_seconds * (2 ** max(0, attempt - 1))))


# --- stores --------------------------------------------------------------------------------------


class MemoryJobStore:
    """In-memory store for eager-mode tests; the same transitions as the SQL store."""

    def __init__(self) -> None:
        self.jobs: dict[int, dict[str, Any]] = {}
        self._next_id = 1

    def add(
        self,
        *,
        type: str,
        payload: dict[str, Any] | None = None,
        municipality_id: str = "podgorica",
        max_attempts: int = 3,
        target_type: str | None = None,
        target_id: int | None = None,
    ) -> int:
        job_id = self._next_id
        self._next_id += 1
        self.jobs[job_id] = {
            "id": job_id,
            "municipality_id": municipality_id,
            "type": type,
            "status": "queued",
            "payload": dict(payload or {}),
            "target_type": target_type,
            "target_id": target_id,
            "document_id": None,
            "file_id": None,
            "attempts": 0,
            "max_attempts": max_attempts,
            "celery_task_id": None,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "result": None,
            "next_retry_at": None,
            "wall_time_ms": None,
            "cost": None,
            "progress": None,
            "history": [],
        }
        return job_id

    def _context(self, row: dict[str, Any]) -> JobContext:
        return JobContext(
            id=row["id"],
            municipality_id=row["municipality_id"],
            type=row["type"],
            status=row["status"],
            payload=dict(row["payload"]),
            target_type=row["target_type"],
            target_id=row["target_id"],
            document_id=row["document_id"],
            file_id=row["file_id"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            result=row["result"],
        )

    async def load(self, job_id: int, municipality_id: str) -> JobContext | None:
        row = self.jobs.get(job_id)
        if row is None or row["municipality_id"] != municipality_id:
            return None
        return self._context(row)

    async def mark_running(
        self, job_id: int, municipality_id: str, task_id: str | None
    ) -> JobContext | None:
        row = self.jobs[job_id]
        row["status"] = "running"
        row["attempts"] += 1
        row["started_at"] = datetime.now(UTC)
        row["celery_task_id"] = row["celery_task_id"] or task_id
        row["next_retry_at"] = None
        row["history"].append("running")
        return self._context(row)

    async def mark_succeeded(
        self,
        job_id: int,
        municipality_id: str,
        *,
        result: dict[str, Any] | None,
        cost: JobCost | None,
        wall_time_ms: int,
    ) -> None:
        row = self.jobs[job_id]
        row.update(
            status="succeeded",
            finished_at=datetime.now(UTC),
            error=None,
            result=result,
            wall_time_ms=wall_time_ms,
            cost=asdict(cost) if cost else None,
        )
        row["history"].append("succeeded")

    async def mark_retrying(
        self,
        job_id: int,
        municipality_id: str,
        *,
        error: str,
        next_retry_at: datetime,
        wall_time_ms: int,
    ) -> None:
        row = self.jobs[job_id]
        row.update(
            status="retrying", error=error, next_retry_at=next_retry_at, wall_time_ms=wall_time_ms
        )
        row["history"].append("retrying")

    async def mark_failed(
        self, job_id: int, municipality_id: str, *, error: str, wall_time_ms: int
    ) -> None:
        row = self.jobs[job_id]
        row.update(
            status="failed", finished_at=datetime.now(UTC), error=error, wall_time_ms=wall_time_ms
        )
        row["history"].append("failed")

    async def set_progress(
        self, job_id: int, municipality_id: str, progress: dict[str, Any]
    ) -> None:
        self.jobs[job_id]["progress"] = progress


_CONTEXT_COLUMNS = """id, municipality_id, type, status, payload, target_type, target_id,
           document_id, file_id, attempts, max_attempts, result"""
LOAD_SQL = text(
    f"SELECT {_CONTEXT_COLUMNS} FROM pipeline_jobs WHERE id = :id AND municipality_id = :m"
)
RUNNING_SQL = text(
    f"""
    UPDATE pipeline_jobs
    SET status = 'running', started_at = now(), attempts = attempts + 1, next_retry_at = NULL,
        celery_task_id = COALESCE(celery_task_id, :task_id)
    WHERE id = :id AND municipality_id = :m
    RETURNING {_CONTEXT_COLUMNS}
    """
)
SUCCEEDED_SQL = text(
    """
    UPDATE pipeline_jobs
    SET status = 'succeeded', finished_at = now(), error = NULL, result = CAST(:result AS jsonb),
        wall_time_ms = :wall_time_ms, llm_model = :llm_model, llm_tokens_in = :tokens_in,
        llm_tokens_out = :tokens_out, estimated_cost_eur = :cost
    WHERE id = :id AND municipality_id = :m
    """
)
RETRYING_SQL = text(
    """
    UPDATE pipeline_jobs
    SET status = 'retrying', error = :error, next_retry_at = :next_retry_at,
        wall_time_ms = :wall_time_ms
    WHERE id = :id AND municipality_id = :m
    """
)
PROGRESS_SQL = text(
    "UPDATE pipeline_jobs SET progress = CAST(:progress AS jsonb) "
    "WHERE id = :id AND municipality_id = :m"
)
FAILED_SQL = text(
    """
    UPDATE pipeline_jobs
    SET status = 'failed', finished_at = now(), error = :error, wall_time_ms = :wall_time_ms
    WHERE id = :id AND municipality_id = :m
    """
)


def _context_from_row(row: Any) -> JobContext:
    return JobContext(
        id=int(row["id"]),
        municipality_id=row["municipality_id"],
        type=row["type"],
        status=row["status"],
        payload=dict(row["payload"] or {}),
        target_type=row["target_type"],
        target_id=row["target_id"],
        document_id=row["document_id"],
        file_id=row["file_id"],
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        result=row["result"],
    )


class SqlJobStore:
    """``pipeline_jobs`` through an async engine. With ``database_url`` every call opens and
    disposes its own engine (a worker process has no pool to share and the call may run on a
    private loop); with ``session_factory`` the caller's engine is used (tests on one loop)."""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        if session_factory is None and database_url is None:
            raise ValueError("SqlJobStore needs a database_url or a session_factory")
        self.database_url = database_url
        self._session_factory = session_factory

    async def _run(self, fn: Callable[[AsyncSession], Coroutine[Any, Any, Any]]) -> Any:
        if self._session_factory is not None:
            async with self._session_factory() as session:
                value = await fn(session)
                await session.commit()
                return value
        engine = create_async_engine(self.database_url or "", poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                value = await fn(session)
                await session.commit()
                return value
        finally:
            await engine.dispose()

    async def load(self, job_id: int, municipality_id: str) -> JobContext | None:
        async def fn(session: AsyncSession) -> JobContext | None:
            result = await session.execute(LOAD_SQL, {"id": job_id, "m": municipality_id})
            row = result.mappings().first()
            return _context_from_row(row) if row is not None else None

        return await self._run(fn)

    async def mark_running(
        self, job_id: int, municipality_id: str, task_id: str | None
    ) -> JobContext | None:
        async def fn(session: AsyncSession) -> JobContext | None:
            params = {"id": job_id, "m": municipality_id, "task_id": task_id}
            row = (await session.execute(RUNNING_SQL, params)).mappings().first()
            return _context_from_row(row) if row is not None else None

        return await self._run(fn)

    async def mark_succeeded(
        self,
        job_id: int,
        municipality_id: str,
        *,
        result: dict[str, Any] | None,
        cost: JobCost | None,
        wall_time_ms: int,
    ) -> None:
        cost = cost or JobCost()

        async def fn(session: AsyncSession) -> None:
            await session.execute(
                SUCCEEDED_SQL,
                {
                    "id": job_id,
                    "m": municipality_id,
                    "result": json.dumps(result if result is not None else {}, default=str),
                    "wall_time_ms": wall_time_ms,
                    "llm_model": cost.llm_model,
                    "tokens_in": cost.tokens_in,
                    "tokens_out": cost.tokens_out,
                    "cost": cost.estimated_cost_eur,
                },
            )

        await self._run(fn)

    async def mark_retrying(
        self,
        job_id: int,
        municipality_id: str,
        *,
        error: str,
        next_retry_at: datetime,
        wall_time_ms: int,
    ) -> None:
        async def fn(session: AsyncSession) -> None:
            await session.execute(
                RETRYING_SQL,
                {
                    "id": job_id,
                    "m": municipality_id,
                    "error": error,
                    "next_retry_at": next_retry_at,
                    "wall_time_ms": wall_time_ms,
                },
            )

        await self._run(fn)

    async def mark_failed(
        self, job_id: int, municipality_id: str, *, error: str, wall_time_ms: int
    ) -> None:
        async def fn(session: AsyncSession) -> None:
            await session.execute(
                FAILED_SQL,
                {"id": job_id, "m": municipality_id, "error": error, "wall_time_ms": wall_time_ms},
            )

        await self._run(fn)

    async def set_progress(
        self, job_id: int, municipality_id: str, progress: dict[str, Any]
    ) -> None:
        async def fn(session: AsyncSession) -> None:
            await session.execute(
                PROGRESS_SQL,
                {"id": job_id, "m": municipality_id, "progress": json.dumps(progress, default=str)},
            )

        await self._run(fn)


# --- lifecycle -----------------------------------------------------------------------------------


def _normalise(value: Any) -> JobResult:
    if value is None:
        return JobResult()
    if isinstance(value, JobResult):
        return value
    if isinstance(value, dict):
        return JobResult(result=value)
    return JobResult(result={"value": value})


async def run_job_async(
    store: AsyncJobStore,
    job_id: int,
    municipality_id: str,
    work: Callable[[JobContext], Any],
    *,
    task_id: str | None = None,
    retry_base_seconds: int = 30,
    retry_max_seconds: int = 900,
    transient: tuple[type[BaseException], ...] = TRANSIENT_EXCEPTIONS,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JobOutcome:
    """Run ``work`` under the job lifecycle (see the module docstring); never raises."""
    job = await store.load(job_id, municipality_id)
    if job is None:
        return JobOutcome(job_id=job_id, status="failed", error="job not found")
    if job.status in FINAL_STATUSES:  # a re-delivered message: nothing to do again
        return JobOutcome(job_id=job_id, status="skipped", attempts=job.attempts, result=job.result)
    job = await store.mark_running(job_id, municipality_id, task_id) or job

    async def report(progress: dict[str, Any]) -> None:
        await store.set_progress(job_id, municipality_id, progress)

    job.report = report
    started = perf_counter()
    try:
        value = work(job)
        if asyncio.iscoroutine(value):
            value = await value
    except transient as exc:
        wall = int((perf_counter() - started) * 1000)
        error = f"{type(exc).__name__}: {exc}"
        if job.attempts < job.max_attempts:
            wait = backoff_seconds(job.attempts, retry_base_seconds, retry_max_seconds)
            await store.mark_retrying(
                job_id,
                municipality_id,
                error=error,
                next_retry_at=clock() + timedelta(seconds=wait),
                wall_time_ms=wall,
            )
            log.warning(
                "job %s attempt %s/%s failed transiently (%s); retry in %ss",
                job_id,
                job.attempts,
                job.max_attempts,
                error,
                wait,
            )
            return JobOutcome(
                job_id=job_id,
                status="retrying",
                error=error,
                retry_in_seconds=wait,
                attempts=job.attempts,
            )
        await store.mark_failed(job_id, municipality_id, error=error, wall_time_ms=wall)
        log.warning("job %s failed after %s attempts: %s", job_id, job.attempts, error)
        return JobOutcome(job_id=job_id, status="failed", error=error, attempts=job.attempts)
    except Exception as exc:  # noqa: BLE001 - a hard failure is recorded, never retried
        wall = int((perf_counter() - started) * 1000)
        error = f"{type(exc).__name__}: {exc}"
        await store.mark_failed(job_id, municipality_id, error=error, wall_time_ms=wall)
        log.warning("job %s failed: %s", job_id, error)
        return JobOutcome(job_id=job_id, status="failed", error=error, attempts=job.attempts)
    wall = int((perf_counter() - started) * 1000)
    outcome = _normalise(value)
    await store.mark_succeeded(
        job_id, municipality_id, result=outcome.result, cost=outcome.cost, wall_time_ms=wall
    )
    return JobOutcome(
        job_id=job_id, status="succeeded", attempts=job.attempts, result=outcome.result
    )


def run_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine from synchronous code: directly when no loop runs in this thread, on a
    private loop in a helper thread otherwise (eager Celery called from inside an event loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=runner, name="urbanview-job", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


# --- configuration -------------------------------------------------------------------------------

_store: AsyncJobStore | None = None
_retry_policy: dict[str, int] = {}


def configure_job_store(store: AsyncJobStore | None) -> None:
    """Replace the process-wide store (``None`` = back to the SQL store of ``DATABASE_URL``)."""
    global _store
    _store = store


def configure_retry_policy(
    *, base_seconds: int | None = None, max_seconds: int | None = None
) -> None:
    """Override the backoff (``None`` = back to the ``JOB_RETRY_*`` settings)."""
    _retry_policy.clear()
    if base_seconds is not None:
        _retry_policy["base"] = int(base_seconds)
    if max_seconds is not None:
        _retry_policy["max"] = int(max_seconds)


def get_job_store() -> AsyncJobStore:
    global _store
    if _store is None:
        from core.config import get_settings

        _store = SqlJobStore(database_url=get_settings().database_url)
    return _store


def retry_policy() -> tuple[int, int]:
    if "base" in _retry_policy and "max" in _retry_policy:
        return _retry_policy["base"], _retry_policy["max"]
    from core.config import get_settings

    settings = get_settings()
    return (
        _retry_policy.get("base", settings.job_retry_base_seconds),
        _retry_policy.get("max", settings.job_retry_max_seconds),
    )


# --- the base task -------------------------------------------------------------------------------


class JobTask(Task):
    """Base class of every job task. A task function takes ``(self, job_id, municipality_id)``,
    the message the API sends, and hands its body to :meth:`execute`::

        @celery_app.task(bind=True, base=JobTask, name="jobs.tasks.extraction.extract_document")
        def extract_document(self, job_id: int, municipality_id: str) -> dict:
            return self.execute(job_id, municipality_id, _extract_document)

    ``execute`` runs the lifecycle around ``work(job)`` and, when the outcome is ``retrying``,
    re-delivers the message itself with a countdown (``apply_async`` rather than ``self.retry``,
    so eager mode and a broker take the same path and the row is the single source of truth).
    """

    abstract = True

    def execute(
        self, job_id: int, municipality_id: str, work: Callable[[JobContext], Any]
    ) -> dict[str, Any]:
        base, cap = retry_policy()
        outcome = run_sync(
            run_job_async(
                get_job_store(),
                int(job_id),
                municipality_id,
                work,
                task_id=getattr(self.request, "id", None),
                retry_base_seconds=base,
                retry_max_seconds=cap,
            )
        )
        if outcome.status == "retrying":
            self.apply_async(
                args=[int(job_id), municipality_id], countdown=outcome.retry_in_seconds
            )
        return outcome.as_dict()
