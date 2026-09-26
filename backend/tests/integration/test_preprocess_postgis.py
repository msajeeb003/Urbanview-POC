"""PDF pre-processing as a job on PostGIS (eager Celery, fake storage): the manifest persisted on
the file record, the scanned pages on the document record, page images at the source viewer's
keys, the checksum cache on a re-run, ``force``, serving the images, and the refusals."""

from __future__ import annotations

import gzip
import json

import pytest
from sqlalchemy import text

from jobs.base import SqlJobStore, configure_job_store
from jobs.tasks.extraction import configure_preprocess
from tests.helpers import make_app, make_client, make_settings
from tests.integration.test_admin_pipeline_postgis import CLEANUP, FakeStorage

pytestmark = pytest.mark.integration

pymupdf = pytest.importorskip("pymupdf")

TOKEN = "admin-token-1234"


class PreprocessStorage(FakeStorage):
    def __init__(self) -> None:
        super().__init__()
        self.puts: list[str] = []

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.puts.append(key)
        return super().put_bytes(key, data, content_type)

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key][0]


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
def preprocess_env(postgis_url, monkeypatch):
    """``build(**settings)`` -> (app, storage): the job runs inline against the test database."""
    from jobs.celery_app import celery_app
    from jobs.enqueue import CeleryDispatcher

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    configure_job_store(SqlJobStore(database_url=postgis_url))
    storage = PreprocessStorage()

    def build(**overrides):
        settings = make_settings(
            location_resolver="postgis",
            database_url=postgis_url,
            rate_limit_requests=100_000,
            admin_api_tokens=f"{TOKEN}:admin:ops",
            preprocess_page_image_dpi=40,
            **overrides,
        )
        configure_preprocess(database_url=postgis_url, storage=storage, settings=settings)
        return make_app(settings, storage=storage, admin_dispatcher=CeleryDispatcher()), storage

    yield build
    configure_job_store(None)
    configure_preprocess(database_url=None, storage=None, settings=None)


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


async def stored_manifest(app, file_id: int) -> dict:
    async with app.state.session_factory() as session:
        return (
            await session.execute(
                text("SELECT preprocess FROM stored_files WHERE id = :id"), {"id": file_id}
            )
        ).scalar_one()


async def test_preprocessing_persists_the_manifest_and_reports_scanned_pages(preprocess_env):
    from tests.pdf_synthetic import planning_pdf

    app, storage = preprocess_env()
    pdf = planning_pdf()
    async with app.router.lifespan_context(app), make_client(app) as client:
        up = await client.post(
            "/v1/admin/files",
            files={"file": ("plan.pdf", pdf, "application/pdf")},
            data={"kind": "planning_document"},
            headers=auth(),
        )
        assert up.status_code == 201, up.text
        file_id = up.json()["file"]["id"]
        doc = await client.post(
            "/v1/admin/documents",
            json={"file_id": file_id, "name": "DUP Test", "type": "DUP", "status": "adopted"},
            headers=auth(),
        )
        assert doc.status_code == 201, doc.text
        document_id = doc.json()["id"]

        first = await client.post(f"/v1/admin/files/{file_id}/jobs/preprocess", headers=auth())
        assert first.status_code == 202, first.text
        job = first.json()
        assert job["type"] == "preprocess_file" and job["status"] == "succeeded", job
        result = job["result"]
        assert result["cached"] is False and result["images_rendered_for"] == [document_id]

        manifest = await stored_manifest(app, file_id)
        assert manifest["sha256"] == up.json()["file"]["sha256"]
        assert [c["pages"] for c in manifest["chunks"]] == [[1], [2], [4], [6]]
        assert manifest["tables"][1]["header_from"] == "p1t1"
        pages = json.loads(gzip.decompress(storage.get_bytes(manifest["page_data_key"])))
        assert pages["pages"][0]["tables"][0]["columns"][2] == "Površina UP"
        keys = manifest["page_images"][str(document_id)]["keys"]
        assert keys == [
            f"podgorica/planning-documents/{document_id}/pages/{n:04d}.png" for n in range(1, 7)
        ]
        assert all(storage.get_bytes(k).startswith(b"\x89PNG") for k in keys)

        file_out = (await client.get(f"/v1/admin/files/{file_id}", headers=auth())).json()
        document_out = (
            await client.get(f"/v1/admin/documents/{document_id}", headers=auth())
        ).json()
        viewer = await client.get(f"/v1/source/{document_id}/page/1")

        # the same file again: nothing is re-read or re-rendered
        puts = len(storage.puts)
        again = await client.post(f"/v1/admin/files/{file_id}/jobs/preprocess", headers=auth())
        assert again.status_code == 202 and again.json()["result"]["cached"] is True
        assert again.json()["result"]["images_rendered_for"] == [] and len(storage.puts) == puts
        forced = await client.post(
            f"/v1/admin/files/{file_id}/jobs/preprocess", params={"force": True}, headers=auth()
        )
        assert forced.json()["result"]["cached"] is False
        assert forced.json()["result"]["images_rendered_for"] == [document_id]

    for record in (file_out["preprocessing"], document_out["preprocessing"]):
        assert (record["scanned_pages"], record["unread_pages"]) == ([3], [3])
        assert (record["vector_pages"], record["page_count"], record["tables"]) == (4, 6, 2)
        assert record["page_images"] == [document_id]
    assert "infrastructure" in document_out["preprocessing"]["sections"]
    assert viewer.json()["kind"] == "pdf_page"  # images are not served unless switched on


async def test_serving_the_page_images_switches_the_source_viewer(preprocess_env):
    from tests.pdf_synthetic import planning_pdf

    app, _ = preprocess_env(preprocess_serve_page_images=True)
    async with app.router.lifespan_context(app), make_client(app) as client:
        up = await client.post(
            "/v1/admin/files",
            files={"file": ("plan.pdf", planning_pdf(), "application/pdf")},
            data={"kind": "planning_document"},
            headers=auth(),
        )
        file_id = up.json()["file"]["id"]
        doc = await client.post(
            "/v1/admin/documents",
            json={"file_id": file_id, "name": "DUP Test", "type": "DUP", "status": "adopted"},
            headers=auth(),
        )
        document_id = doc.json()["id"]
        job = await client.post(f"/v1/admin/files/{file_id}/jobs/preprocess", headers=auth())
        assert job.json()["status"] == "succeeded", job.text
        viewer = await client.get(f"/v1/source/{document_id}/page/3")
    body = viewer.json()
    assert viewer.status_code == 200 and body["kind"] == "page_image"
    assert f"planning-documents/{document_id}/pages/0003.png" in body["url"]


async def test_only_stored_planning_pdfs_are_preprocessed(preprocess_env):
    app, _ = preprocess_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        missing = await client.post("/v1/admin/files/999999/jobs/preprocess", headers=auth())
        gis = await client.post(
            "/v1/admin/files",
            files={"file": ("layers.zip", b"PK\x03\x04" + b"\x00" * 64, "application/zip")},
            data={"kind": "gis"},
            headers=auth(),
        )
        assert gis.status_code == 201, gis.text
        refused = await client.post(
            f"/v1/admin/files/{gis.json()['file']['id']}/jobs/preprocess", headers=auth()
        )
        anonymous = await client.post("/v1/admin/files/1/jobs/preprocess")
    assert missing.status_code == 404 and refused.status_code == 409
    assert anonymous.status_code == 401
