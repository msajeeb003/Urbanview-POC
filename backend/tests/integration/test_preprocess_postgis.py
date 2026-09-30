"""The PDF pre-processing stage on PostGIS (fake storage; the extraction and geometry jobs run it
first): the manifest persisted on the file record, the scanned pages on the document record, the
checksum cache on a re-run, ``force``, the source viewer serving the PDF page, and the
refusals."""

from __future__ import annotations

import gzip
import json

import pytest
from sqlalchemy import text

from jobs.preprocessing import PreprocessError
from jobs.tasks.extraction import configure_preprocess, run_preprocess
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
def preprocess_env(postgis_url):
    """``build(**settings)`` -> (app, storage): the stage runs against the test database."""
    storage = PreprocessStorage()

    def build(**overrides):
        settings = make_settings(
            location_resolver="postgis",
            database_url=postgis_url,
            rate_limit_requests=100_000,
            admin_api_tokens=f"{TOKEN}:admin:ops",
            **overrides,
        )
        configure_preprocess(database_url=postgis_url, storage=storage, settings=settings)
        return make_app(settings, storage=storage), storage

    yield build
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

        result = await run_preprocess("podgorica", file_id)
        assert result["cached"] is False

        manifest = await stored_manifest(app, file_id)
        assert manifest["sha256"] == up.json()["file"]["sha256"]
        assert [c["pages"] for c in manifest["chunks"]] == [[1], [2], [4], [6]]
        assert manifest["tables"][1]["header_from"] == "p1t1"
        pages = json.loads(gzip.decompress(storage.get_bytes(manifest["page_data_key"])))
        assert pages["pages"][0]["tables"][0]["columns"][2] == "Površina UP"

        again_up = await client.post(
            "/v1/admin/files",
            files={"file": ("plan.pdf", pdf, "application/pdf")},
            data={"kind": "planning_document"},
            headers=auth(),
        )
        assert again_up.status_code == 200, again_up.text
        file_out = again_up.json()["file"]
        document_out = (
            await client.get(f"/v1/admin/documents/{document_id}", headers=auth())
        ).json()
        viewer = await client.get(f"/v1/source/{document_id}/page/1")

        # the same file again: nothing is re-read or re-written
        puts = len(storage.puts)
        again = await run_preprocess("podgorica", file_id)
        assert again["cached"] is True and len(storage.puts) == puts
        forced = await run_preprocess("podgorica", file_id, force=True)
        assert forced["cached"] is False

    for record in (file_out["preprocessing"], document_out["preprocessing"]):
        assert (record["scanned_pages"], record["unread_pages"]) == ([3], [3])
        assert (record["vector_pages"], record["page_count"], record["tables"]) == (4, 6, 2)
    assert "infrastructure" in document_out["preprocessing"]["sections"]
    assert viewer.json()["kind"] == "pdf_page" and viewer.json()["url"].endswith("#page=1")


async def test_only_stored_planning_pdfs_are_preprocessed(preprocess_env):
    app, _ = preprocess_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        gis = await client.post(
            "/v1/admin/files",
            files={"file": ("layers.zip", b"PK\x03\x04" + b"\x00" * 64, "application/zip")},
            data={"kind": "gis"},
            headers=auth(),
        )
        assert gis.status_code == 201, gis.text
        with pytest.raises(PreprocessError, match="no stored file"):
            await run_preprocess("podgorica", 999999)
        with pytest.raises(PreprocessError, match="not a planning PDF"):
            await run_preprocess("podgorica", gis.json()["file"]["id"])
