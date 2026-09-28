"""Is a worker listening on the extraction queue? (the AI extraction page's checklist)

:class:`CeleryWorkerProbe` asks the broker which queues the running workers consume
(``celery inspect active_queues``, one broadcast with a short timeout, in a thread) and caches
the answer for ``cache_seconds``; one probe runs at a time. Eager mode (tests) counts as a
worker. The API never fails because of the probe: an unreachable broker is a state.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from starlette.concurrency import run_in_threadpool


@dataclass(frozen=True, slots=True)
class WorkerState:
    state: str  # ready | no_worker | unreachable | eager
    workers: int
    detail_en: str | None
    checked_at: datetime


class WorkerProbe(Protocol):
    async def probe(self) -> WorkerState: ...


class CeleryWorkerProbe:
    def __init__(
        self,
        *,
        queue: str = "extraction",
        timeout_seconds: float = 1.0,
        cache_seconds: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.queue = queue
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self.monotonic = monotonic
        self.now = now
        self._cached: tuple[float, WorkerState] | None = None
        self._lock = asyncio.Lock()

    def _fresh(self) -> WorkerState | None:
        if self._cached is None:
            return None
        at, state = self._cached
        return state if self.monotonic() - at < self.cache_seconds else None

    async def probe(self) -> WorkerState:
        cached = self._fresh()
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._fresh()
            if cached is not None:
                return cached
            state = await self._probe()
            self._cached = (self.monotonic(), state)
            return state

    async def _probe(self) -> WorkerState:
        from jobs.celery_app import celery_app

        if celery_app.conf.task_always_eager:
            return WorkerState(
                "eager", 1, "Tasks run inline (CELERY_TASK_ALWAYS_EAGER)", self.now()
            )

        def inspect() -> object:
            return celery_app.control.inspect(timeout=self.timeout_seconds).active_queues()

        try:
            replies = await asyncio.wait_for(
                run_in_threadpool(inspect), timeout=self.timeout_seconds + 2
            )
        except Exception as exc:  # noqa: BLE001 - broker down, timeout: a state, not an error
            return WorkerState(
                "unreachable",
                0,
                f"Could not reach the job queue (Redis): {type(exc).__name__}",
                self.now(),
            )
        if not replies:
            return WorkerState(
                "no_worker",
                0,
                "No worker answered: start the worker (docker compose … up -d worker)",
                self.now(),
            )
        answered = len(replies)
        consuming = sum(
            1
            for queues in replies.values()  # type: ignore[union-attr]
            if any(isinstance(q, dict) and q.get("name") == self.queue for q in queues or [])
        )
        if consuming == 0:
            return WorkerState(
                "no_worker",
                0,
                f"{answered} worker(s) answered but none consumes the {self.queue} queue",
                self.now(),
            )
        return WorkerState(
            "ready", consuming, f"{consuming} worker(s) on the {self.queue} queue", self.now()
        )
