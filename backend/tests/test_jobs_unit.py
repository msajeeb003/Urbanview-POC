"""Job infrastructure without a database: the backoff schedule, the cost estimate, idempotency
keys, the lifecycle on the in-memory store (transient errors retried up to ``max_attempts`` then
failed with the message; hard failures never retried; cost recorded on success; finished jobs
skipped on re-delivery), and a real Celery task driven through the base class in eager mode."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobs.base import (
    JobCost,
    JobResult,
    JobTask,
    MemoryJobStore,
    RateLimited,
    TransientError,
    backoff_seconds,
    configure_job_store,
    configure_retry_policy,
    run_job_async,
)
from jobs.cost import estimate_cost_eur, parse_price_table
from jobs.enqueue import JOB_TYPES, QUEUES, dedupe_key

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def test_backoff_is_exponential_and_capped():
    assert [backoff_seconds(a, 30, 900) for a in range(1, 8)] == [30, 60, 120, 240, 480, 900, 900]
    assert backoff_seconds(0, 30, 900) == 30
    assert backoff_seconds(3, 5, 10) == 10


def test_cost_estimate_uses_the_configured_prices():
    table = parse_price_table("claude-sonnet-5=3:15, claude-haiku-4-5-20251001=1:5")
    assert table == {"claude-sonnet-5": (3.0, 15.0), "claude-haiku-4-5-20251001": (1.0, 5.0)}
    kwargs = {"default_in": 2.0, "default_out": 10.0, "table": table}
    assert estimate_cost_eur("claude-sonnet-5", 1_000_000, 100_000, **kwargs) == 4.5
    assert estimate_cost_eur("unknown-model", 500_000, 0, **kwargs) == 1.0
    assert estimate_cost_eur(None, 0, 0, **kwargs) == 0.0
    assert parse_price_table("") == {} and parse_price_table(None) == {}
    with pytest.raises(ValueError):
        parse_price_table("claude-sonnet-5=3")
    with pytest.raises(ValueError):
        parse_price_table("claude-sonnet-5=a:b")


def test_dedupe_keys_name_the_target_and_the_checksum():
    assert dedupe_key("extract_document", "document", 12) == "extract_document:document:12"
    assert (
        dedupe_key("process_geometry", "file", 7, "abc123")
        == "process_geometry:file:7:sha256:abc123"
    )
    assert dedupe_key("publish_approved", "publish_run", None) == "publish_approved:publish_run:-"


def test_job_types_cover_the_four_tasks_and_queues():
    assert set(JOB_TYPES) == {
        "extract_document",
        "process_geometry",
        "publish_approved",
        "send_email",
    }
    assert {t.queue for t in JOB_TYPES.values()} == {"extraction", "geo", "publish", "email"}
    assert set(QUEUES) == {"default", "extraction", "geo", "publish", "email"}
    for spec in JOB_TYPES.values():
        assert spec.task.endswith(f".{spec.name}")


async def test_transient_errors_are_retried_with_backoff_then_failed():
    store = MemoryJobStore()
    job_id = store.add(type="extract_document", max_attempts=3)
    seen: list[int] = []

    def work(job):
        seen.append(job.attempts)
        raise TransientError("LLM timeout")

    outcomes = [
        await run_job_async(
            store,
            job_id,
            "podgorica",
            work,
            retry_base_seconds=30,
            retry_max_seconds=900,
            clock=lambda: NOW,
        )
        for _ in range(3)
    ]
    assert [o.status for o in outcomes] == ["retrying", "retrying", "failed"]
    assert [o.retry_in_seconds for o in outcomes] == [30, 60, None]
    assert [o.attempts for o in outcomes] == [1, 2, 3]
    assert seen == [1, 2, 3]
    row = store.jobs[job_id]
    assert row["status"] == "failed" and row["attempts"] == 3
    assert row["error"] == "TransientError: LLM timeout"
    assert row["finished_at"] is not None and row["wall_time_ms"] is not None
    assert row["history"] == ["running", "retrying", "running", "retrying", "running", "failed"]
    # a stale re-delivery of the failed job does nothing
    again = await run_job_async(store, job_id, "podgorica", work)
    assert again.status == "skipped" and seen == [1, 2, 3]


async def test_rate_limits_and_timeouts_count_as_transient():
    store = MemoryJobStore()
    job_id = store.add(type="extract_document")

    def limited(job):
        raise RateLimited("429 Too Many Requests")

    def timeout(job):
        raise TimeoutError("read timed out")

    first = await run_job_async(store, job_id, "podgorica", limited, retry_base_seconds=5)
    second = await run_job_async(store, job_id, "podgorica", timeout, retry_base_seconds=5)
    assert (first.status, first.retry_in_seconds) == ("retrying", 5)
    assert (second.status, second.retry_in_seconds) == ("retrying", 10)
    assert store.jobs[job_id]["next_retry_at"] is not None
    assert store.jobs[job_id]["error"] == "TimeoutError: read timed out"


async def test_hard_failures_are_not_retried():
    store = MemoryJobStore()
    job_id = store.add(type="process_geometry", max_attempts=3)

    def broken(job):
        raise ValueError("no vector layer in the PDF")

    outcome = await run_job_async(store, job_id, "podgorica", broken)
    assert outcome.status == "failed" and outcome.attempts == 1
    assert outcome.retry_in_seconds is None
    row = store.jobs[job_id]
    assert row["status"] == "failed" and row["error"] == "ValueError: no vector layer in the PDF"
    assert row["history"] == ["running", "failed"]


async def test_success_records_result_wall_time_and_cost():
    store = MemoryJobStore()
    job_id = store.add(type="extract_document", payload={"document_id": 12})
    cost = JobCost(
        llm_model="claude-sonnet-5", tokens_in=12_000, tokens_out=800, estimated_cost_eur=0.048
    )

    async def extract(job):
        assert job.payload == {"document_id": 12} and job.attempts == 1
        return JobResult(result={"extracted": 3}, cost=cost)

    outcome = await run_job_async(store, job_id, "podgorica", extract, task_id="celery-1")
    assert outcome.status == "succeeded" and outcome.result == {"extracted": 3}
    row = store.jobs[job_id]
    assert row["status"] == "succeeded" and row["result"] == {"extracted": 3}
    assert row["cost"] == {
        "llm_model": "claude-sonnet-5",
        "tokens_in": 12_000,
        "tokens_out": 800,
        "estimated_cost_eur": 0.048,
    }
    assert row["wall_time_ms"] >= 0 and row["celery_task_id"] == "celery-1"
    # plain dicts and None are accepted results too
    other = store.add(type="publish_approved")
    assert (await run_job_async(store, other, "podgorica", lambda job: None)).status == "succeeded"
    assert store.jobs[other]["result"] is None and store.jobs[other]["cost"] is None
    missing = await run_job_async(store, 999, "podgorica", lambda job: None)
    assert missing.status == "failed" and missing.error == "job not found"


def test_eager_celery_task_runs_the_lifecycle_and_redispatches_itself(monkeypatch):
    from jobs.celery_app import celery_app

    store = MemoryJobStore()
    configure_job_store(store)
    configure_retry_policy(base_seconds=1, max_seconds=4)
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    attempts: list[int] = []

    def flaky(job):
        attempts.append(job.attempts)
        if job.attempts < 3:
            raise TransientError("provider 429")
        return JobResult(
            result={"ok": True},
            cost=JobCost(
                llm_model="test-model", tokens_in=10, tokens_out=5, estimated_cost_eur=0.01
            ),
        )

    @celery_app.task(bind=True, base=JobTask, name="tests.jobs.flaky")
    def flaky_task(self, job_id: int, municipality_id: str) -> dict:
        return self.execute(job_id, municipality_id, flaky)

    try:
        job_id = store.add(type="extract_document", max_attempts=3)
        first = flaky_task.apply_async(args=[job_id, "podgorica"], queue="extraction").get()
        assert first["status"] == "retrying" and first["retry_in_seconds"] == 1
        assert attempts == [1, 2, 3]
        row = store.jobs[job_id]
        assert row["status"] == "succeeded" and row["attempts"] == 3
        assert row["result"] == {"ok": True} and row["cost"]["estimated_cost_eur"] == 0.01
        assert row["celery_task_id"]  # the first delivery's task id sticks

        hard = store.add(type="extract_document")
        outcome = flaky_task.apply_async(args=[hard, "podgorica"]).get()
        # attempts continue counting on a fresh row: 1 -> transient -> 2 -> transient -> 3 -> ok
        assert outcome["status"] == "retrying" and store.jobs[hard]["status"] == "succeeded"
    finally:
        configure_job_store(None)
        configure_retry_policy()
        celery_app.tasks.pop("tests.jobs.flaky", None)


def test_eager_task_marks_the_exhausted_job_failed(monkeypatch):
    from jobs.celery_app import celery_app

    store = MemoryJobStore()
    configure_job_store(store)
    configure_retry_policy(base_seconds=1, max_seconds=1)
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    attempts: list[int] = []

    def always_down(job):
        attempts.append(job.attempts)
        raise TransientError("LLM timeout")

    @celery_app.task(bind=True, base=JobTask, name="tests.jobs.always_down")
    def down_task(self, job_id: int, municipality_id: str) -> dict:
        return self.execute(job_id, municipality_id, always_down)

    try:
        job_id = store.add(type="extract_document", max_attempts=3)
        down_task.apply_async(args=[job_id, "podgorica"]).get()
        assert attempts == [1, 2, 3]
        row = store.jobs[job_id]
        assert row["status"] == "failed" and row["error"] == "TransientError: LLM timeout"
    finally:
        configure_job_store(None)
        configure_retry_policy()
        celery_app.tasks.pop("tests.jobs.always_down", None)
