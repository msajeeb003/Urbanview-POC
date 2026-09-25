"""Job infrastructure on PostGIS: idempotent enqueueing through the API (document and file
checksum keys), the SQL-backed lifecycle (transient retries with ``next_retry_at``, exhaustion,
cost on success), the job listing and its filters, the cost summary per document, the manual
retry (audited, re-dispatched, refused while active) and the real Celery task in eager mode."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jobs.base import (
    JobResult,
    SqlJobStore,
    TransientError,
    configure_job_store,
    run_job_async,
)
from jobs.cost import cost_for
from tests.helpers import make_client
from tests.integration.test_admin_pipeline_postgis import (
    CLEANUP,
    PDF_A,
    PDF_B,
    ZIP,
    FakeDispatcher,
    FakeStorage,
    audit_actions,
    auth,
    build,
    register,
    upload,
)

pytestmark = pytest.mark.integration


# The fakes and helpers are the staff pipeline test's; the fixtures are declared here because
# pytest registers fixtures per module (an imported fixture name shadows the import: F811).
@pytest.fixture
def storage():
    return FakeStorage()


@pytest.fixture
def dispatcher():
    return FakeDispatcher()


@pytest.fixture
def admin_app(postgis_url, storage, dispatcher):
    return build(postgis_url, storage, dispatcher)


@pytest.fixture(autouse=True)
async def _clean_job_rows(postgis_url):
    """Same per-test cleanup as the staff pipeline tests: no staff rows, uploads, registered
    documents or jobs survive a test, so the seeded counts other tests rely on stay intact."""

    async def clean() -> None:
        engine = create_async_engine(postgis_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine)() as session:
                for statement in CLEANUP:
                    await session.execute(text(statement))
                await session.execute(
                    text("UPDATE planning_documents SET coverage_live = true WHERE id IN (1, 2, 4)")
                )
                await session.commit()
        finally:
            await engine.dispose()

    await clean()
    yield
    await clean()


async def test_enqueueing_the_same_document_twice_returns_the_existing_job(admin_app, dispatcher):
    app = admin_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        file = (await upload(client, PDF_A, "a.pdf")).json()["file"]
        doc = (await register(client, file["id"])).json()
        first = await client.post(f"/v1/admin/documents/{doc['id']}/jobs/extract", headers=auth())
        second = await client.post(f"/v1/admin/documents/{doc['id']}/jobs/extract", headers=auth())
        by_target = await client.get(
            "/v1/admin/jobs", params={"target": f"document:{doc['id']}"}, headers=auth()
        )
        by_type = await client.get(
            "/v1/admin/jobs",
            params={"type": "extract_document", "status": "queued"},
            headers=auth(),
        )
        other_type = await client.get(
            "/v1/admin/jobs", params={"type": "process_geometry"}, headers=auth()
        )
        bad_target = await client.get(
            "/v1/admin/jobs", params={"target": "document"}, headers=auth()
        )
        by_document = await client.get(
            "/v1/admin/jobs", params={"document_id": doc["id"]}, headers=auth()
        )
    assert first.status_code == 202, first.text
    assert second.status_code == 200, second.text
    job = first.json()
    assert second.json() == job
    assert job["type"] == "extract_document" and job["queue"] == "extraction"
    assert job["target_type"] == "document" and job["target_id"] == doc["id"]
    assert job["payload"] == {"document_id": doc["id"], "file_id": file["id"]}
    assert job["dedupe_key"] == f"extract_document:document:{doc['id']}:sha256:{file['sha256']}"
    assert job["attempts"] == 0 and job["max_attempts"] == 3 and job["manual_retries"] == 0
    assert job["cost"] == {
        "wall_time_ms": None,
        "llm_model": None,
        "llm_tokens_in": None,
        "llm_tokens_out": None,
        "estimated_cost_eur": None,
    }
    assert dispatcher.calls == [("extract_document", job["id"], "podgorica")]
    assert by_target.json()["total"] == 1 and by_target.json()["items"][0]["id"] == job["id"]
    assert [j["id"] for j in by_type.json()["items"]] == [job["id"]]
    assert other_type.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}
    assert bad_target.status_code == 422
    assert by_document.json()["total"] == 1
    assert [a["action"] for a in await audit_actions(app, "pipeline_job", job["id"])] == [
        "job.enqueue"
    ]


async def test_geometry_jobs_are_keyed_on_the_file_checksum(admin_app, dispatcher):
    app = admin_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        one = (await upload(client, ZIP, "layers.zip", kind="gis", mime="application/zip")).json()
        two = (await upload(client, ZIP, "again.zip", kind="gis", mime="application/zip")).json()
        assert one["file"]["id"] == two["file"]["id"]  # same content, same stored file
        file_id = one["file"]["id"]
        first = await client.post(f"/v1/admin/files/{file_id}/jobs/geo", headers=auth())
        second = await client.post(f"/v1/admin/files/{file_id}/jobs/geo", headers=auth())
        # once the job has finished, the same content may be processed again on request
        store = SqlJobStore(session_factory=app.state.session_factory)

        def broken(job):
            raise ValueError("no layers")

        outcome = await run_job_async(store, first.json()["id"], "podgorica", broken)
        third = await client.post(f"/v1/admin/files/{file_id}/jobs/geo", headers=auth())
        listing = await client.get(
            "/v1/admin/jobs", params={"target": f"file:{file_id}"}, headers=auth()
        )
    assert first.status_code == 202 and second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert first.json()["dedupe_key"] == (
        f"process_geometry:file:{file_id}:sha256:{one['file']['sha256']}"
    )
    assert outcome.status == "failed"
    assert third.status_code == 202 and third.json()["id"] != first.json()["id"]
    assert [c[1] for c in dispatcher.calls] == [first.json()["id"], third.json()["id"]]
    assert listing.json()["total"] == 2
    assert [j["status"] for j in listing.json()["items"]] == ["queued", "failed"]


async def test_sql_lifecycle_retries_transient_errors_then_fails(admin_app):
    app = admin_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        file = (await upload(client, PDF_A, "a.pdf")).json()["file"]
        doc = (await register(client, file["id"])).json()
        job = (
            await client.post(f"/v1/admin/documents/{doc['id']}/jobs/extract", headers=auth())
        ).json()
        store = SqlJobStore(session_factory=app.state.session_factory)

        def down(job):
            raise TransientError("LLM timeout")

        first = await run_job_async(store, job["id"], "podgorica", down, retry_base_seconds=30)
        after_first = (await client.get(job["status_url"], headers=auth())).json()
        second = await run_job_async(store, job["id"], "podgorica", down, retry_base_seconds=30)
        third = await run_job_async(store, job["id"], "podgorica", down, retry_base_seconds=30)
        final = (await client.get(job["status_url"], headers=auth())).json()
        failed_listing = await client.get(
            "/v1/admin/jobs", params={"status": "failed"}, headers=auth()
        )
        retrying_listing = await client.get(
            "/v1/admin/jobs", params={"status": "retrying"}, headers=auth()
        )
    assert (first.status, first.retry_in_seconds) == ("retrying", 30)
    assert after_first["status"] == "retrying" and after_first["attempts"] == 1
    assert after_first["next_retry_at"] is not None and after_first["finished_at"] is None
    assert after_first["error"] == "TransientError: LLM timeout"
    assert after_first["cost"]["wall_time_ms"] is not None
    assert (second.status, second.retry_in_seconds) == ("retrying", 60)
    assert (third.status, third.retry_in_seconds, third.attempts) == ("failed", None, 3)
    assert final["status"] == "failed" and final["attempts"] == 3
    assert final["error"] == "TransientError: LLM timeout"
    assert final["finished_at"] is not None and final["next_retry_at"] is None
    assert [j["id"] for j in failed_listing.json()["items"]] == [job["id"]]
    assert retrying_listing.json()["total"] == 0


async def test_cost_is_recorded_and_summed_per_document(admin_app):
    app = admin_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        file = (await upload(client, PDF_A, "a.pdf")).json()["file"]
        doc = (await register(client, file["id"])).json()
        first = (
            await client.post(f"/v1/admin/documents/{doc['id']}/jobs/extract", headers=auth())
        ).json()
        store = SqlJobStore(session_factory=app.state.session_factory)

        def extract_big(job):
            return JobResult(result={"extracted": 3}, cost=cost_for("claude-sonnet-5", 12_000, 800))

        def extract_small(job):
            return JobResult(result={"extracted": 1}, cost=cost_for("claude-sonnet-5", 4_000, 0))

        await run_job_async(store, first["id"], "podgorica", extract_big)
        first_row = (await client.get(first["status_url"], headers=auth())).json()
        second = (
            await client.post(f"/v1/admin/documents/{doc['id']}/jobs/extract", headers=auth())
        ).json()
        await run_job_async(store, second["id"], "podgorica", extract_small)
        listing = await client.get(
            "/v1/admin/jobs", params={"target": f"document:{doc['id']}"}, headers=auth()
        )
        costs = await client.get(
            "/v1/admin/jobs/costs", params={"target": f"document:{doc['id']}"}, headers=auth()
        )
        all_costs = await client.get("/v1/admin/jobs/costs", headers=auth())
        document = await client.get(f"/v1/admin/documents/{doc['id']}", headers=auth())
    assert first_row["status"] == "succeeded" and first_row["result"] == {"extracted": 3}
    cost = first_row["cost"]
    assert cost["llm_model"] == "claude-sonnet-5"
    assert (cost["llm_tokens_in"], cost["llm_tokens_out"]) == (12_000, 800)
    assert cost["estimated_cost_eur"] == 0.048  # 12k x 3 EUR/MTok + 800 x 15 EUR/MTok (defaults)
    assert cost["wall_time_ms"] is not None and cost["wall_time_ms"] >= 0
    assert second["id"] != first["id"]  # the first job had finished: not deduplicated
    items = listing.json()["items"]
    assert [j["id"] for j in items] == [second["id"], first["id"]]
    assert [j["cost"]["estimated_cost_eur"] for j in items] == [0.012, 0.048]
    summary = costs.json()
    assert len(summary["rows"]) == 1
    row = summary["rows"][0]
    assert (row["target_type"], row["target_id"]) == ("document", doc["id"])
    assert (row["jobs"], row["succeeded"], row["failed"]) == (2, 2, 0)
    assert (row["llm_tokens_in"], row["llm_tokens_out"]) == (16_000, 800)
    assert row["estimated_cost_eur"] == 0.06 and row["last_finished_at"] is not None
    assert summary["total_estimated_cost_eur"] == 0.06 and summary["total_jobs"] == 2
    assert any(
        r["target_id"] == doc["id"] and r["estimated_cost_eur"] == 0.06
        for r in all_costs.json()["rows"]
    )
    # the document listing's job history shows the cost too
    doc_jobs = document.json()["jobs"]
    assert [j["cost"]["estimated_cost_eur"] for j in doc_jobs] == [0.012, 0.048]


async def test_manual_retry_requeues_a_failed_job(admin_app, dispatcher):
    app = admin_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        file = (await upload(client, PDF_A, "a.pdf")).json()["file"]
        doc = (await register(client, file["id"])).json()
        job = (
            await client.post(f"/v1/admin/documents/{doc['id']}/jobs/extract", headers=auth())
        ).json()
        while_queued = await client.post(f"/v1/admin/jobs/{job['id']}/retry", headers=auth())
        store = SqlJobStore(session_factory=app.state.session_factory)

        def broken(job):
            raise ValueError("malformed PDF")

        await run_job_async(store, job["id"], "podgorica", broken)
        retried = await client.post(f"/v1/admin/jobs/{job['id']}/retry", headers=auth())
        again = await client.post(f"/v1/admin/documents/{doc['id']}/jobs/extract", headers=auth())
        succeeded = await run_job_async(store, job["id"], "podgorica", lambda j: {"ok": True})
        done = await client.post(f"/v1/admin/jobs/{job['id']}/retry", headers=auth())
        missing = await client.post("/v1/admin/jobs/999999/retry", headers=auth())
    assert while_queued.status_code == 409
    assert while_queued.json()["error"]["details"] == {"job_id": job["id"], "status": "queued"}
    assert retried.status_code == 202, retried.text
    body = retried.json()
    assert body["status"] == "queued" and body["attempts"] == 0 and body["manual_retries"] == 1
    assert body["error"] is None and body["finished_at"] is None
    assert body["celery_task_id"] == f"task-{job['id']}"
    assert dispatcher.calls == [
        ("extract_document", job["id"], "podgorica"),
        ("extract_document", job["id"], "podgorica"),
    ]
    # the re-queued job holds the idempotency key again
    assert again.status_code == 200 and again.json()["id"] == job["id"]
    assert succeeded.status == "succeeded" and succeeded.attempts == 1
    assert done.status_code == 409 and done.json()["error"]["details"]["status"] == "succeeded"
    assert missing.status_code == 404
    actions = await audit_actions(app, "pipeline_job", job["id"])
    assert [a["action"] for a in actions] == ["job.enqueue", "job.retry"]
    assert actions[1]["details"] == {"type": "extract_document", "manual_retries": 1}


async def test_eager_celery_task_runs_through_the_api(postgis_url, storage, monkeypatch):
    """With ``task_always_eager`` the real task runs inside the request: the enqueue reply already
    shows the stub's outcome (failed with a clear not-implemented error, one attempt, wall time)."""
    from jobs.celery_app import celery_app
    from jobs.enqueue import CeleryDispatcher

    configure_job_store(SqlJobStore(database_url=postgis_url))
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    app = build(postgis_url, storage, CeleryDispatcher())
    try:
        async with app.router.lifespan_context(app), make_client(app) as client:
            file = (await upload(client, PDF_B, "b.pdf")).json()["file"]
            doc = (await register(client, file["id"], name="DUP Eager")).json()
            extract = await client.post(
                f"/v1/admin/documents/{doc['id']}/jobs/extract", headers=auth()
            )
            gis = (await upload(client, ZIP, "l.zip", kind="gis", mime="application/zip")).json()
            geo = await client.post(f"/v1/admin/files/{gis['file']['id']}/jobs/geo", headers=auth())
            status = await client.get(extract.json()["status_url"], headers=auth())
    finally:
        configure_job_store(None)
    assert extract.status_code == 202, extract.text
    job = extract.json()
    assert job["status"] == "failed" and job["attempts"] == 1
    assert job["error"] == (
        "NotImplementedError: LLM extraction lands with the AI track item; nothing was extracted"
    )
    assert job["celery_task_id"] and job["started_at"] and job["finished_at"]
    assert job["cost"]["wall_time_ms"] is not None
    assert status.json() == job
    assert geo.status_code == 202 and geo.json()["status"] == "failed"
    assert "geometry extraction lands with the GIS track item" in geo.json()["error"]
