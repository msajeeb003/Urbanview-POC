"""Staff pipeline API on PostGIS with storage and Celery mocked: upload de-duplication, document
versions, job enqueueing and status, the coverage switch and its effect on location resolution,
staff sessions as principals, the audit log, and the job lifecycle helper."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import text

from core.seeds import placeholder_pdf
from core.staff import create_user, issue_session, revoke_sessions
from jobs.base import SqlJobStore, run_job_async
from tests.helpers import make_app, make_client, make_settings

pytestmark = pytest.mark.integration

TOKEN = "admin-token-1234"
PDF_A = placeholder_pdf("DUP Test A", 3)
PDF_B = placeholder_pdf("DUP Test B", 5)
PDF_STAFF = placeholder_pdf("DUP Staff upload", 2)
ZIP = b"PK\x03\x04" + b"\x00" * 64
CLEANUP = (
    "DELETE FROM pipeline_jobs WHERE municipality_id = 'podgorica'",
    "DELETE FROM planning_documents WHERE registered_by IS NOT NULL",
    "DELETE FROM stored_files WHERE municipality_id = 'podgorica'",
    "DELETE FROM staff_sessions",
    "DELETE FROM staff_users WHERE municipality_id = 'podgorica'",
)


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = (data, content_type)
        return key

    def presigned_get_url(self, key, expires_in=900, *, content_type=None, inline=False):
        return f"https://minio.test/urbanview-dev/{key}?X-Amz-Signature=sig"

    def ensure_bucket(self) -> None:
        pass


class FakeDispatcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, int, str]] = []
        self.fail = fail

    def enqueue(self, job_type, job_id, municipality_id):
        self.calls.append((job_type, job_id, municipality_id))
        if self.fail:
            raise ConnectionError("broker down")
        return f"task-{job_id}"


def build(postgis_url, storage, dispatcher, **overrides):
    settings = make_settings(
        location_resolver="postgis",
        database_url=postgis_url,
        rate_limit_requests=100_000,
        admin_api_tokens=f"{TOKEN}:admin:ops",
        **overrides,
    )
    return make_app(settings, storage=storage, admin_dispatcher=dispatcher)


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
async def _clean_admin_rows(postgis_url):
    """Each test starts and ends without staff rows, uploads, registered documents or jobs, so
    the seeded zones, documents and coverages the locate / panel tests count stay untouched."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

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


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def upload(
    client, data, filename, kind="planning_document", mime="application/pdf", token=TOKEN
):
    return await client.post(
        "/v1/admin/files",
        files={"file": (filename, data, mime)},
        data={"kind": kind},
        headers=auth(token),
    )


async def register(client, file_id, name="DUP Test", **fields):
    body = {"file_id": file_id, "name": name, "type": "dup", "status": "in_progress", **fields}
    return await client.post("/v1/admin/documents", json=body, headers=auth())


async def audit_actions(app, entity_type, entity_id):
    async with app.state.session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT action, actor, details FROM audit_log WHERE municipality_id = 'podgorica' "
                "AND entity_type = :t AND entity_id = :i ORDER BY id"
            ),
            {"t": entity_type, "i": entity_id},
        )
        return [dict(r) for r in rows.mappings()]


# --- files ----------------------------------------------------------------------------------------


async def test_uploads_are_deduplicated_by_checksum(admin_app, storage):
    app = admin_app
    sha = hashlib.sha256(PDF_A).hexdigest()
    async with app.router.lifespan_context(app), make_client(app) as client:
        first = await upload(client, PDF_A, "DUP Test A.pdf")
        second = await upload(client, PDF_A, "same-bytes-other-name.pdf")
        listed = await client.get("/v1/admin/files", headers=auth())
        rejected = await upload(client, b"<html>", "not-a.pdf")
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["created"] is True
    file = body["file"]
    assert file["sha256"] == sha
    assert file["page_count"] == 3
    assert file["original_filename"] == "DUP_Test_A.pdf"
    assert file["kind"] == "planning_document"
    assert file["mime_type"] == "application/pdf"
    assert file["size_bytes"] == len(PDF_A)
    assert file["uploaded_by"] == "ops"
    assert file["document_ids"] == [] and file["jobs"] == []
    key = f"podgorica/uploads/planning_document/{sha}/DUP_Test_A.pdf"
    assert storage.objects == {key: (PDF_A, "application/pdf")}
    assert first.headers["Cache-Control"] == "no-store"

    assert second.status_code == 200, second.text
    assert second.json()["created"] is False
    assert second.json()["file"]["id"] == file["id"]
    assert len(storage.objects) == 1  # nothing stored twice

    assert listed.status_code == 200
    assert file["id"] in [item["id"] for item in listed.json()["items"]]
    assert rejected.status_code == 422

    actions = await audit_actions(app, "stored_file", file["id"])
    assert [a["action"] for a in actions] == ["file.upload", "file.upload_duplicate"]
    assert actions[0]["actor"] == "ops" and actions[0]["details"]["sha256"] == sha


async def test_upload_size_limit(postgis_url, storage, dispatcher):
    app = build(postgis_url, storage, dispatcher, admin_upload_max_mb=1)
    big = b"%PDF-1.4\n" + b"\x00" * (1024 * 1024 + 1)
    async with app.router.lifespan_context(app), make_client(app) as client:
        r = await upload(client, big, "big.pdf")
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"
    assert storage.objects == {}


# --- documents ------------------------------------------------------------------------------------


async def test_registering_again_creates_a_new_version_and_keeps_the_old(admin_app):
    app = admin_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        file_a = (await upload(client, PDF_A, "a.pdf")).json()["file"]
        file_b = (await upload(client, PDF_B, "b.pdf")).json()["file"]
        v1 = await register(
            client,
            file_a["id"],
            source="eRegistri",
            source_url="https://lamp.gov.me/PlanningDocument?m=PG",
            zone_id=1,
            licence_note="public planning document; reuse permitted with attribution",
        )
        assert v1.status_code == 201, v1.text
        v1 = v1.json()
        v2 = await register(
            client, file_b["id"], status="adopted", replaces_document_id=v1["id"], zone_id=1
        )
        assert v2.status_code == 201, v2.text
        v2 = v2.json()
        stale = await register(client, file_b["id"], replaces_document_id=v1["id"])
        current_only = await client.get(
            "/v1/admin/documents", params={"lineage_id": v1["id"]}, headers=auth()
        )
        all_versions = await client.get(
            "/v1/admin/documents",
            params={"lineage_id": v1["id"], "include_previous": "true"},
            headers=auth(),
        )
        v1_again = await client.get(f"/v1/admin/documents/{v1['id']}", headers=auth())
        bad_type = await register(client, file_a["id"], type="XYZ")
        bad_zone = await register(client, file_a["id"], zone_id=999_999)
        gis = (await upload(client, ZIP, "layers.zip", kind="gis", mime="application/zip")).json()
        bad_file = await register(client, gis["file"]["id"])
        no_file = await register(client, 999_999)
        no_previous = await register(client, file_a["id"], replaces_document_id=999_999)

    assert v1["version"] == 1 and v1["lineage_id"] == v1["id"] and v1["is_current_version"]
    assert v1["type"] == "DUP" and v1["status"] == "in_progress"
    assert v1["zone_id"] == 1 and v1["zone_name"] == "Centar"
    assert v1["file"]["id"] == file_a["id"] and v1["page_count"] == 3
    assert v1["has_coverage"] is False and v1["coverage_live"] is False
    assert v1["registered_by"] == "ops" and v1["registered_at"]
    assert v1["licence_note"].startswith("public planning document")

    assert v2["version"] == 2 and v2["lineage_id"] == v1["id"] and v2["is_current_version"]
    assert v2["status"] == "adopted" and v2["file"]["id"] == file_b["id"]
    assert [(v["id"], v["version"], v["is_current_version"]) for v in v2["versions"]] == [
        (v1["id"], 1, False),
        (v2["id"], 2, True),
    ]
    assert stale.status_code == 409
    assert stale.json()["error"]["details"]["current_version_id"] == v2["id"]

    assert [d["id"] for d in current_only.json()["items"]] == [v2["id"]]
    assert sorted(d["id"] for d in all_versions.json()["items"]) == sorted([v1["id"], v2["id"]])
    assert v1_again.json()["is_current_version"] is False  # retained, not deleted

    assert bad_type.status_code == 422 and "type" in bad_type.text
    assert bad_zone.status_code == 422 and "zone" in bad_zone.text
    assert bad_file.status_code == 422 and "planning_document" in bad_file.text
    assert no_file.status_code == 422
    assert no_previous.status_code == 404

    actions = await audit_actions(app, "planning_document", v2["id"])
    assert actions[0]["action"] == "document.register"
    assert actions[0]["details"]["version"] == 2
    assert actions[0]["details"]["replaces_document_id"] == v1["id"]


# --- jobs -----------------------------------------------------------------------------------------


async def test_jobs_are_enqueued_with_celery_mocked(admin_app, dispatcher):
    app = admin_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        file = (await upload(client, PDF_A, "a.pdf")).json()["file"]
        doc = (await register(client, file["id"])).json()
        extract = await client.post(f"/v1/admin/documents/{doc['id']}/jobs/extract", headers=auth())
        assert extract.status_code == 202, extract.text
        job = extract.json()
        status = await client.get(job["status_url"], headers=auth())
        doc_listing = await client.get(f"/v1/admin/documents/{doc['id']}", headers=auth())
        gis = (await upload(client, ZIP, "layers.zip", kind="gis", mime="application/zip")).json()
        geo = await client.post(f"/v1/admin/files/{gis['file']['id']}/jobs/geo", headers=auth())
        file_listing = await client.get(f"/v1/admin/files/{gis['file']['id']}", headers=auth())
        no_file_doc = await client.post("/v1/admin/documents/3/jobs/extract", headers=auth())
        csv = (
            await upload(
                client, b"ko;number\n", "extract.csv", kind="cadastral_extract", mime="text/csv"
            )
        ).json()
        wrong_kind = await client.post(
            f"/v1/admin/files/{csv['file']['id']}/jobs/geo", headers=auth()
        )
        missing = await client.get("/v1/admin/jobs/999999", headers=auth())

    assert job["kind"] == "extract" and job["status"] == "queued"
    assert job["type"] == "extract_document" and job["queue"] == "extraction"
    assert job["document_id"] == doc["id"] and job["file_id"] is None
    assert job["celery_task_id"] == f"task-{job['id']}"
    assert job["status_url"] == f"/v1/admin/jobs/{job['id']}"
    assert job["requested_by"] == "ops"
    assert dispatcher.calls[0] == ("extract_document", job["id"], "podgorica")
    assert status.status_code == 200 and status.json() == job
    assert [j["id"] for j in doc_listing.json()["jobs"]] == [job["id"]]

    assert geo.status_code == 202, geo.text
    assert geo.json()["kind"] == "geo" and geo.json()["file_id"] == gis["file"]["id"]
    assert dispatcher.calls[1] == ("process_geometry", geo.json()["id"], "podgorica")
    assert [j["id"] for j in file_listing.json()["jobs"]] == [geo.json()["id"]]

    assert no_file_doc.status_code == 409  # seeded document 3 has no stored file
    assert wrong_kind.status_code == 409
    assert missing.status_code == 404

    actions = await audit_actions(app, "pipeline_job", job["id"])
    assert [a["action"] for a in actions] == ["job.enqueue"]


async def test_a_dead_queue_records_the_failure(postgis_url, storage):
    dispatcher = FakeDispatcher(fail=True)
    app = build(postgis_url, storage, dispatcher)
    async with app.router.lifespan_context(app), make_client(app) as client:
        file = (await upload(client, PDF_A, "a.pdf")).json()["file"]
        doc = (await register(client, file["id"])).json()
        r = await client.post(f"/v1/admin/documents/{doc['id']}/jobs/extract", headers=auth())
        assert r.status_code == 503, r.text
        job_id = r.json()["error"]["details"]["job_id"]
        job = (await client.get(f"/v1/admin/jobs/{job_id}", headers=auth())).json()
    assert job["status"] == "failed"
    assert "broker down" in job["error"]
    assert dispatcher.calls == [("extract_document", job_id, "podgorica")]
    actions = await audit_actions(app, "pipeline_job", job_id)
    assert [a["action"] for a in actions] == ["job.enqueue", "job.enqueue_failed"]


async def test_job_lifecycle_helper(admin_app):
    app = admin_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        file_a = (await upload(client, PDF_A, "a.pdf")).json()["file"]
        file_b = (await upload(client, PDF_B, "b.pdf")).json()["file"]
        doc_a = (await register(client, file_a["id"])).json()
        doc_b = (await register(client, file_b["id"], name="DUP Test B")).json()
        failing = (
            await client.post(f"/v1/admin/documents/{doc_a['id']}/jobs/extract", headers=auth())
        ).json()
        succeeding = (
            await client.post(f"/v1/admin/documents/{doc_b['id']}/jobs/extract", headers=auth())
        ).json()
        store = SqlJobStore(session_factory=app.state.session_factory)

        def not_yet(job):
            raise NotImplementedError("nothing extracted yet")

        failed = await run_job_async(store, failing["id"], "podgorica", not_yet)
        succeeded = await run_job_async(
            store,
            succeeding["id"],
            "podgorica",
            lambda job: {"extracted": 0},
            task_id="task-from-worker",
        )
        failed_row = (await client.get(failing["status_url"], headers=auth())).json()
        succeeded_row = (await client.get(succeeding["status_url"], headers=auth())).json()
    assert failed.status == "failed" and failed.attempts == 1
    assert failed_row["status"] == "failed" and failed_row["attempts"] == 1
    assert failed_row["error"] == "NotImplementedError: nothing extracted yet"
    assert failed_row["started_at"] and failed_row["finished_at"]
    assert succeeded.status == "succeeded" and succeeded.result == {"extracted": 0}
    assert succeeded_row["status"] == "succeeded" and succeeded_row["result"] == {"extracted": 0}
    assert succeeded_row["celery_task_id"] == f"task-{succeeding['id']}"  # the enqueue's id sticks


async def test_staff_sessions_are_principals(admin_app):
    app = admin_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        factory = app.state.session_factory
        await create_user(
            factory, municipality_id="podgorica", email="Ana@Example.com", role="admin"
        )
        await create_user(
            factory, municipality_id="podgorica", email="bob@example.com", role="reviewer"
        )
        ana = await issue_session(
            factory, municipality_id="podgorica", email="ana@example.com", created_via="test"
        )
        bob = await issue_session(
            factory, municipality_id="podgorica", email="bob@example.com", created_via="test"
        )
        expired = await issue_session(
            factory, municipality_id="podgorica", email="ana@example.com", ttl=timedelta(seconds=-1)
        )
        as_ana = await client.get("/v1/admin/files", headers=auth(ana))
        uploaded = await upload(client, PDF_STAFF, "staff.pdf", token=ana)
        as_bob = await client.get("/v1/admin/files", headers=auth(bob))
        as_expired = await client.get("/v1/admin/files", headers=auth(expired))
        revoked = await revoke_sessions(
            factory, municipality_id="podgorica", email="ana@example.com"
        )
        after_revoke = await client.get("/v1/admin/files", headers=auth(ana))
        async with factory() as session:
            uploader = (
                await session.execute(
                    text(
                        "SELECT uploaded_by, uploaded_by_user_id FROM stored_files WHERE id = :id"
                    ),
                    {"id": uploaded.json()["file"]["id"]},
                )
            ).one()
    assert as_ana.status_code == 200
    assert uploaded.status_code == 201, uploaded.text
    assert uploader[0] == "ana@example.com" and uploader[1] is not None
    assert as_bob.status_code == 403
    assert as_expired.status_code == 401
    assert revoked >= 1 and after_revoke.status_code == 401
    with pytest.raises(LookupError):
        await issue_session(factory, municipality_id="podgorica", email="nobody@example.com")
