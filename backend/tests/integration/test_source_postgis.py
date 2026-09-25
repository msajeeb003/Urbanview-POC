"""Source viewer against PostGIS: the SQL repository (serving table only, one statement per call),
the seeded document files and the one-click chain from a panel source reference to a signed URL.
Storage is mocked: only the signing is faked, the lookups are real."""

from __future__ import annotations

import pytest
from sqlalchemy import event

from tests.helpers import make_app, make_client
from tests.test_source import FakeStorage

pytestmark = pytest.mark.integration


@pytest.fixture
def source_app(pg_settings):
    return make_app(pg_settings, storage=FakeStorage())  # real SqlSourceRepository


async def test_a_value_resolves_to_the_page_it_cites(source_app):
    async with source_app.router.lifespan_context(source_app), make_client(source_app) as client:
        r = await client.get("/v1/source/value/1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["document_id"], body["page"], body["page_count"]) == (2, 12, 24)
    assert body["document_name"] == "DUP Centar – Zona C2"
    assert body["kind"] == "pdf_page"  # the sample ships placeholder PDFs, no page images
    assert body["url"].endswith(
        "/podgorica/planning-documents/2/document.pdf?X-Amz-Expires=900&X-Amz-Signature=sig#page=12"
    )
    value = body["value"]
    assert value["value_id"] == 1 and value["field_key"] == "land_use"
    assert value["value"] == "Residential – mixed use (ground-floor commercial)"
    assert value["bbox"] == [72, 610, 520, 628]
    assert value["bbox_space"] == "pdf-points-bottom-left"
    assert value["note"] == "table 3 – UP 12"
    assert value["urban_parcel_id"] == 1
    assert value["label_en"] and value["label_me"]


async def test_document_pages_and_the_404s(source_app):
    async with source_app.router.lifespan_context(source_app), make_client(source_app) as client:
        last_page = await client.get("/v1/source/2/page/24")
        beyond = await client.get("/v1/source/2/page/25")
        not_stored = await client.get("/v1/source/3/page/1")  # in-progress amendment, no file
        superseded = await client.get("/v1/source/5/page/12")
        no_document = await client.get("/v1/source/999999/page/1")
        no_value = await client.get("/v1/source/value/999999")
    assert last_page.status_code == 200 and last_page.json()["page"] == 24
    assert beyond.status_code == 404
    assert beyond.json()["error"]["details"] == {"document_id": 2, "page": 25, "page_count": 24}
    assert not_stored.status_code == 404
    assert not_stored.json()["error"]["details"]["reason"] == "not_stored"
    assert superseded.status_code == 200  # superseded documents stay viewable
    assert superseded.json()["document_status"] == "superseded"
    assert no_document.status_code == 404 and no_value.status_code == 404
    assert no_document.json()["error"]["details"] == {"document_id": 999999}
    assert no_value.json()["error"]["details"] == {"value_id": 999999}


async def test_a_panel_source_reference_opens_in_one_click(source_app):
    async with source_app.router.lifespan_context(source_app), make_client(source_app) as client:
        panel = (await client.get("/v1/panel", params={"type": "urban", "id": 1})).json()
        stated = [f for f in panel["planning"]["fields"] if f["status"] == "stated"]
        assert stated
        for field in stated:
            source = field["source"]
            assert source["viewer_url"] == f"/v1/source/value/{source['value_id']}"
            r = await client.get(source["viewer_url"])
            assert r.status_code == 200, (field["key"], r.text)
            body = r.json()
            assert body["document_id"] == source["document_id"]
            assert body["page"] == source["page"]
            assert body["value"]["value_id"] == source["value_id"]
            assert body["value"]["bbox"] == source["bbox"]
            assert body["value"]["field_key"] == field["key"]


async def test_one_statement_per_call(source_app):
    async with source_app.router.lifespan_context(source_app):
        statements: list[str] = []

        @event.listens_for(source_app.state.engine.sync_engine, "before_cursor_execute")
        def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            statements.append(statement)

        async with make_client(source_app) as client:
            assert (await client.get("/v1/source/value/1")).status_code == 200
            assert (await client.get("/v1/source/2/page/3")).status_code == 200
    assert len(statements) == 2
