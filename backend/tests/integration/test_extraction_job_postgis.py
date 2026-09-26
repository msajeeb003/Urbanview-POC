"""The extract_document job on PostGIS, through the API with eager Celery, fake storage and a
scripted model that transcribes the parameter table (``tests/extraction_script.Transcriber``):
items for every parcel of the table, pending; idempotency (the same file read the same way is not
read again; an identical job still running is returned); one page whose answer never fits is
listed while the rest is ready for review; a transient outage retried by the job, resuming from
the checkpointed steps; re-extraction with another model keeping decisions, superseding pending
items and linking what changed; a new file version superseding the old version's open items;
the audit rows and the run summary on the job."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from core.extraction.llm import ModelUnavailable
from core.extraction.prompts import PROMPT_VERSION
from jobs.base import SqlJobStore, configure_job_store
from jobs.tasks.extraction import configure_extraction, configure_preprocess
from tests.extraction_script import Transcriber
from tests.helpers import make_app, make_client, make_settings
from tests.integration.test_admin_pipeline_postgis import CLEANUP, FakeStorage

pytestmark = pytest.mark.integration

pymupdf = pytest.importorskip("pymupdf")

TOKEN = "admin-token-1234"
REVIEWER = "reviewer-token-1234"
PARCELS = {"UP 1", "UP 2", "UP 3", "UP 4", "UP 5"}
TABLE_FIELDS = {
    "planned_parcel_area_m2",
    "land_use",
    "max_floors",
    "max_site_coverage_pct",
    "max_far",
}


class Storage(FakeStorage):
    def get_bytes(self, key: str) -> bytes:
        return self.objects[key][0]


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture(autouse=True)
async def _clean(postgis_url):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    async def clean() -> None:
        engine = create_async_engine(postgis_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine)() as session:
                for statement in CLEANUP:
                    await session.execute(text(statement))
                await session.commit()
        finally:
            await engine.dispose()

    await clean()
    yield
    await clean()


@pytest.fixture
def env(postgis_url, monkeypatch):
    """``build(model, **settings)`` -> app: the job runs inline against the test database."""
    from jobs.celery_app import celery_app
    from jobs.enqueue import CeleryDispatcher

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    configure_job_store(SqlJobStore(database_url=postgis_url))
    storage = Storage()

    def build(model, **overrides):
        values = dict(
            location_resolver="postgis",
            database_url=postgis_url,
            rate_limit_requests=100_000,
            admin_api_tokens=f"{TOKEN}:admin:ops,{REVIEWER}:reviewer:rev",
            preprocess_page_image_dpi=40,
            extraction_model=model.name,
            extraction_retry_base_seconds=0,  # eager Celery ignores the job's countdown
        )
        values.update(overrides)
        settings = make_settings(**values)
        configure_preprocess(database_url=postgis_url, storage=storage, settings=settings)
        configure_extraction(
            database_url=postgis_url,
            storage=storage,
            settings=settings,
            model=model,
            sleep=_no_sleep,
        )
        return make_app(settings, storage=storage, admin_dispatcher=CeleryDispatcher())

    yield build
    configure_job_store(None)
    configure_preprocess(database_url=None, storage=None, settings=None)
    configure_extraction(database_url=None, storage=None, settings=None, model=None, sleep=None)


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def changed_pdf() -> bytes:
    """The same pages with other bytes (another file checksum)."""
    from tests.pdf_synthetic import planning_pdf

    doc = pymupdf.open(stream=planning_pdf(), filetype="pdf")
    doc.set_metadata({"title": "DUP Test, izmjena"})
    return doc.tobytes()


async def register(client, pdf: bytes, *, replaces: int | None = None) -> int:
    up = await client.post(
        "/v1/admin/files",
        files={"file": ("plan.pdf", pdf, "application/pdf")},
        data={"kind": "planning_document"},
        headers=auth(),
    )
    assert up.status_code in (200, 201), up.text
    body = {
        "file_id": up.json()["file"]["id"],
        "name": "DUP Test",
        "type": "DUP",
        "status": "adopted",
    }
    if replaces is not None:
        body["replaces_document_id"] = replaces
    doc = await client.post("/v1/admin/documents", json=body, headers=auth())
    assert doc.status_code == 201, doc.text
    return doc.json()["id"]


async def extract(client, document_id: int, **params):
    return await client.post(
        f"/v1/admin/documents/{document_id}/jobs/extract", params=params, headers=auth()
    )


async def items(app, document_id: int, **where) -> list[dict]:
    clauses = " ".join(f"AND {k} = :{k}" for k in where)
    async with app.state.session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT id, target_label, target_key, field_key, review_state::text AS state, "
                "value_text, value_number, flags, run_id, previous_item_id, change, "
                "superseded_at, superseded_by_run_id, prompt_version, schema_version, "
                "extracted_by, urban_parcel_id, source_page, raw_text, confidence "
                f"FROM planning_parameter_extractions WHERE document_id = :document_id {clauses} "
                "ORDER BY id"
            ),
            {"document_id": document_id, **where},
        )
        return [dict(r) for r in rows.mappings()]


async def audit(app, run_id: int) -> list[dict]:
    async with app.state.session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT action, actor, details FROM audit_log WHERE entity_type = 'extraction_run' "
                "AND entity_id = :id ORDER BY id"
            ),
            {"id": run_id},
        )
        return [dict(r) for r in rows.mappings()]


async def test_a_document_is_read_into_pending_items_for_every_parcel(env):
    from tests.pdf_synthetic import planning_pdf

    model = Transcriber()
    app = env(model)
    async with app.router.lifespan_context(app), make_client(app) as client:
        document_id = await register(client, planning_pdf())
        response = await extract(client, document_id)
        assert response.status_code == 202, response.text
        job = (await client.get(response.json()["status_url"], headers=auth())).json()
        again = await extract(client, document_id)
        document = (await client.get(f"/v1/admin/documents/{document_id}", headers=auth())).json()

    assert job["status"] == "succeeded", job
    run = job["extraction_run"]
    summary = job["result"]
    assert run["status"] == "ready_for_review" and run["id"] == summary["run_id"]
    written = await items(app, document_id)
    assert {row["target_label"] for row in written} == PARCELS
    for label in PARCELS:
        assert {r["field_key"] for r in written if r["target_label"] == label} == TABLE_FIELDS
    assert {row["state"] for row in written} == {"pending_review"}
    assert {row["run_id"] for row in written} == {run["id"]}
    assert all("target_unmatched" in row["flags"] for row in written)  # no geometry yet
    assert all(row["urban_parcel_id"] is None and row["change"] == "new" for row in written)
    assert {row["prompt_version"] for row in written} == {PROMPT_VERSION}
    assert {row["schema_version"] for row in written} == {"1.0"}
    assert {row["extracted_by"] for row in written} == {"llm:claude-test-model"}
    assert all(row["source_page"] in (1, 2) and row["raw_text"] for row in written)

    assert summary["items_written"] == len(written) == run["items_written"]
    assert summary["pages_processed"] == [1, 2, 4, 6] and summary["pages_skipped"] == [3]
    assert summary["pages_failed"] == [] and summary["steps_total"] == 10
    assert summary["model_version"] == "claude-test-model"
    assert (summary["prompt_version"], summary["schema_version"]) == (PROMPT_VERSION, "1.0")
    assert summary["tokens_in"] == 1000 * len(model.calls) == job["cost"]["llm_tokens_in"]
    assert job["cost"]["llm_model"] == "claude-test-model"
    assert summary["items_unmatched"] == len(written)

    # the same file read the same way again: the finished run answers, nothing is re-read
    assert again.status_code == 200 and again.json()["id"] == job["id"]
    assert len(await items(app, document_id)) == len(written)
    assert len(model.calls) == summary["llm_requests"]
    assert document["extraction"]["status"] == "ready_for_review"
    assert document["review"]["pending"] == len(written)
    actions = [a["action"] for a in await audit(app, run["id"])]
    assert actions == ["extraction.start", "extraction.finish"]


async def test_one_page_that_never_fits_is_listed_and_the_rest_is_ready(env):
    from tests.pdf_synthetic import planning_pdf

    model = Transcriber(invalid_pages=[2])
    app = env(model)
    async with app.router.lifespan_context(app), make_client(app) as client:
        document_id = await register(client, planning_pdf())
        response = await extract(client, document_id)
        document = (await client.get(f"/v1/admin/documents/{document_id}", headers=auth())).json()

    job = response.json()
    assert job["status"] == "succeeded", job
    failed = job["result"]["pages_failed"]
    assert [(f["page"], f["chunk"], f["task"]) for f in failed] == [(2, "c002", "parameter_table")]
    assert "invalid answer" in failed[0]["error"]
    assert document["extraction"]["status"] == "ready_for_review"
    assert document["extraction"]["pages_failed_count"] == 1
    assert {row["target_label"] for row in await items(app, document_id)} == {
        "UP 1",
        "UP 2",
        "UP 3",
    }
    retried = [call for call in model.calls if call[0] == "parameter_table" and call[1] == [2]]
    assert len(retried) == 2 and "rejected by the validator" in retried[1][2]


async def test_a_transient_outage_is_retried_by_the_job_which_resumes(env):
    from tests.pdf_synthetic import planning_pdf

    # steps 1 and 2 answer; step 3 meets an outage on both its tries -> the job retries
    model = Transcriber(fail_calls=[3, 4], failure=lambda: ModelUnavailable("529 overloaded"))
    app = env(model, extraction_call_retries=1)
    async with app.router.lifespan_context(app), make_client(app) as client:
        document_id = await register(client, planning_pdf())
        job = (await extract(client, document_id)).json()

    assert job["status"] == "succeeded" and job["attempts"] == 2, job
    assert len(model.calls) == 4 + 8  # attempt 1: 2 steps + 2 failed tries; then 8 steps left
    assert len(await items(app, document_id)) == job["result"]["items_written"] > 0
    actions = [a["action"] for a in await audit(app, job["extraction_run"]["id"])]
    assert actions == ["extraction.start", "extraction.start", "extraction.finish"]


async def test_reextraction_keeps_decisions_and_links_what_changed(env):
    from tests.pdf_synthetic import planning_pdf

    first_model = Transcriber(name="model-a")
    app = env(first_model)
    async with app.router.lifespan_context(app), make_client(app) as client:
        document_id = await register(client, planning_pdf())
        first = (await extract(client, document_id)).json()
        old = {(r["target_label"], r["field_key"]): r for r in await items(app, document_id)}
        approved = old[("UP 1", "max_far")]
        decision = await client.post(
            f"/v1/admin/review/{approved['id']}/approve", json={}, headers=auth(REVIEWER)
        )
        assert decision.status_code == 200, decision.text

    second_model = Transcriber(name="model-b", override={("UP 2", "max_far"): "2,4"})
    app = env(second_model)
    async with app.router.lifespan_context(app), make_client(app) as client:
        second = await extract(client, document_id)
        assert second.status_code == 202, second.text
        second = second.json()
        queue = (
            await client.get(
                "/v1/admin/review", params={"document_id": document_id}, headers=auth(REVIEWER)
            )
        ).json()
        history = (
            await client.get(
                "/v1/admin/review",
                params={"document_id": document_id, "include_superseded": "true", "limit": 200},
                headers=auth(REVIEWER),
            )
        ).json()
        rows = {r["id"]: r for r in await items(app, document_id)}
        new = {
            (r["target_label"], r["field_key"]): r
            for r in rows.values()
            if r["run_id"] == second["extraction_run"]["id"]
        }
        closed = await client.post(
            f"/v1/admin/review/{old[('UP 3', 'max_far')]['id']}/approve",
            json={},
            headers=auth(REVIEWER),
        )
        newer = new[("UP 1", "max_far")]
        approve_new = await client.post(
            f"/v1/admin/review/{newer['id']}/approve", json={}, headers=auth(REVIEWER)
        )
        after = {r["id"]: r for r in await items(app, document_id)}

    run_a, run_b = first["extraction_run"]["id"], second["extraction_run"]["id"]
    assert run_a != run_b and second["result"]["model_version"] == "model-b"
    # the pending items of the first run are superseded; the approved one stays
    for key, row in old.items():
        if row["id"] == approved["id"]:
            assert rows[row["id"]]["superseded_at"] is None
        else:
            assert rows[row["id"]]["superseded_by_run_id"] == run_b, key
    assert second["result"]["items_superseded"] == len(old) - 1
    # every new item points at the previous reading of its target, and says whether it changed
    assert newer["previous_item_id"] == approved["id"] and newer["change"] == "same"
    assert new[("UP 2", "max_far")]["change"] == "changed"
    assert new[("UP 2", "max_far")]["previous_item_id"] == old[("UP 2", "max_far")]["id"]
    assert all(r["change"] == "same" for k, r in new.items() if k != ("UP 2", "max_far"))
    # the queue shows the current items only; history on request
    listed = {item["id"] for item in queue["items"]}
    assert listed == {r["id"] for r in new.values()} | {approved["id"]}
    changed_item = next(i for i in queue["items"] if i["id"] == new[("UP 2", "max_far")]["id"])
    assert (
        changed_item["change"] == "changed" and changed_item["previous"]["value"]["number"] == 1.8
    )
    assert changed_item["target"]["label"] == "UP 2" and changed_item["target"]["matched"] is False
    assert history["total"] == len(rows)
    assert closed.status_code == 409 and closed.json()["error"]["details"]["reason"] == "superseded"
    # approving the newer reading retires the older approved item
    assert approve_new.status_code == 200
    assert after[approved["id"]]["superseded_by_run_id"] == run_b


async def test_a_new_file_version_supersedes_the_old_versions_open_items(env):
    from tests.pdf_synthetic import planning_pdf

    model = Transcriber()
    app = env(model)
    async with app.router.lifespan_context(app), make_client(app) as client:
        v1 = await register(client, planning_pdf())
        first = (await extract(client, v1)).json()
        old = await items(app, v1)
        await client.post(f"/v1/admin/review/{old[0]['id']}/approve", json={}, headers=auth())
        v2 = await register(client, changed_pdf(), replaces=v1)
        second = await extract(client, v2)
        assert second.status_code == 202, second.text
        second = second.json()
        summary = (
            await client.get(
                "/v1/admin/review/summary", params={"document_id": v1}, headers=auth(REVIEWER)
            )
        ).json()
        v1_doc = (await client.get(f"/v1/admin/documents/{v1}", headers=auth())).json()

    run_b = second["extraction_run"]["id"]
    assert second["extraction_run"]["file_sha256"] != first["extraction_run"]["file_sha256"]
    superseded = await items(app, v1)
    assert all(r["superseded_by_run_id"] == run_b for r in superseded)  # approved one too
    assert second["result"]["superseded_runs"] == [first["extraction_run"]["id"]]
    assert {r["target_label"] for r in await items(app, v2)} == PARCELS
    assert summary == [] or summary[0]["pending"] == 0
    assert v1_doc["review"]["pending"] == 0 and v1_doc["review"]["total"] == 0
    assert v1_doc["extraction"]["superseded_by_run_id"] == run_b


async def test_an_identical_request_while_the_job_is_queued_returns_it(postgis_url):
    from tests.integration.test_admin_pipeline_postgis import FakeDispatcher
    from tests.pdf_synthetic import planning_pdf

    dispatcher = FakeDispatcher()
    settings = make_settings(
        location_resolver="postgis",
        database_url=postgis_url,
        rate_limit_requests=100_000,
        admin_api_tokens=f"{TOKEN}:admin:ops",
    )
    app = make_app(settings, storage=Storage(), admin_dispatcher=dispatcher)
    async with app.router.lifespan_context(app), make_client(app) as client:
        document_id = await register(client, planning_pdf())
        first = await extract(client, document_id)
        second = await extract(client, document_id)
        forced = await extract(client, document_id, force="true")
    assert first.status_code == 202 and second.status_code == 200
    assert second.json()["id"] == first.json()["id"] == forced.json()["id"]
    assert first.json()["extraction_run"]["status"] == "queued"
    assert first.json()["dedupe_key"].endswith(f":prompt:{PROMPT_VERSION}:schema:1.0")
    assert len(dispatcher.calls) == 1
