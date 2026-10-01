"""Prepared planning values on PostGIS (``core.extraction.prepared``): the values of a parameter
table staged as approved items of one ``table-reader`` run; the model's pending reading of the
same file superseded and compared; a parcel without geometry reported; the same data loaded
twice changing nothing; a corrected data file retiring the unpublished decision it replaces;
the items eligible for the publish job; the audit row; a dry run and the refusals."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from core.extraction.prepared import (
    ACTION,
    NOTE,
    PreparedLoadError,
    build_prepared,
    load_prepared,
)
from tests.extraction_script import Transcriber
from tests.helpers import make_client
from tests.integration.test_extraction_job_postgis import (  # noqa: F401 - fixtures
    _clean,
    auth,
    env,
    extract,
    items,
    register,
)
from tests.test_prepared_values import synthetic_prepared

pytestmark = pytest.mark.integration

pymupdf = pytest.importorskip("pymupdf")

DRAWN = ("UP 1", "UP 2", "UP 3", "UP 4")  # UP 5 is in the table, not in the geometry


async def draw_parcels(app, document_id: int) -> None:
    async with app.state.session_factory() as session:
        for number in DRAWN:
            await session.execute(
                text(
                    "INSERT INTO urban_parcels (municipality_id, urban_parcel_number, geom, "
                    "area_m2, document_id) VALUES ('podgorica', :n, "
                    "ST_Multi(ST_MakeEnvelope(19.26, 42.44, 19.261, 42.441, 4326)), 100, :d)"
                ),
                {"n": number, "d": document_id},
            )
        await session.commit()


async def one(app, sql: str, **params):
    async with app.state.session_factory() as session:
        return (await session.execute(text(sql), params)).mappings().first()


async def test_prepared_values_replace_the_pending_reading(env):  # noqa: F811
    from jobs.publish_pipeline import ELIGIBLE_ITEMS_SQL
    from tests.pdf_synthetic import planning_pdf

    pdf = planning_pdf()
    doc, gold, pages = synthetic_prepared(pdf)
    prepared, problems = build_prepared(doc, gold, pages, municipality="podgorica")
    assert problems == []
    prepared.parcels[1].values["max_far"].value = 1.9  # the model read 1,8 for UP 2

    app = env(Transcriber())
    async with app.router.lifespan_context(app), make_client(app) as client:
        document_id = await register(client, pdf)
        await draw_parcels(app, document_id)
        job = await extract(client, document_id)
        assert job.status_code == 202, job.text
        model_items = await items(app, document_id)
        assert len(model_items) == 25 and {r["state"] for r in model_items} == {"pending_review"}
        model_run = model_items[0]["run_id"]
        factory = app.state.session_factory
        load = dict(municipality_id="podgorica", document_id=document_id, by="cli")

        dry = await load_prepared(
            factory, prepared=prepared, data_sha256="a" * 64, dry_run=True, **load
        )
        assert dry.items_written == 20 and len(await items(app, document_id)) == 25

        summary = await load_prepared(factory, prepared=prepared, data_sha256="a" * 64, **load)
        document = (await client.get(f"/v1/admin/documents/{document_id}", headers=auth())).json()
        again = await load_prepared(factory, prepared=prepared, data_sha256="a" * 64, **load)

        # a corrected data file: one value differs, the rest is already approved
        prepared.parcels[0].values["max_floors"].value = "Po+P+7"
        corrected = await load_prepared(factory, prepared=prepared, data_sha256="b" * 64, **load)

        with pytest.raises(PreparedLoadError, match="no file with the checksum"):
            other = prepared.model_copy(deep=True)
            other.source.sha256 = "0" * 64
            await load_prepared(factory, prepared=other, data_sha256="c" * 64, **load)
        with pytest.raises(PreparedLoadError, match="no planning document"):
            await load_prepared(
                factory,
                prepared=prepared,
                data_sha256="c" * 64,
                municipality_id="podgorica",
                document_id=987654,
                by="cli",
            )

    # what the load says it did
    assert (summary.parcels_in_data, summary.parcels_matched) == (5, 4)
    assert summary.unmatched_parcels == ["UP 5"]
    assert (summary.values_in_data, summary.items_written, summary.items_kept) == (25, 20, 0)
    assert summary.by_change == {"same": 19, "changed": 1}
    assert summary.changed == [
        {
            "parcel": "UP 2",
            "field": "max_far",
            "previous": 1.8,
            "previous_state": "pending_review",
            "prepared": 1.9,
        }
    ]
    assert summary.superseded_items == 25 and summary.superseded_runs == [model_run]
    assert summary.superseded_not_in_data == 0 and summary.retired_decisions == 0
    assert summary.review_before["pending"] == 25
    assert summary.review_after == {"pending": 0, "approved": 20, "amended": 0, "rejected": 0}

    # the items: approved, on their parcels, citing page and cell; the model's reading superseded
    rows = await items(app, document_id)
    loaded = [r for r in rows if r["run_id"] == summary.run_id]
    assert len(loaded) == 20 and {r["state"] for r in loaded} == {"approved"}
    assert {r["target_label"] for r in loaded} == set(DRAWN)
    assert all(r["urban_parcel_id"] and r["previous_item_id"] for r in loaded)
    assert {r["extracted_by"] for r in loaded} == {f"table-reader:{prepared.preprocess_version}"}
    assert all(r["source_page"] in (1, 2) and r["raw_text"] and r["flags"] == [] for r in loaded)
    assert all(r["confidence"] is None and r["schema_version"] is None for r in loaded)
    replaced = [r for r in rows if r["run_id"] == model_run]
    assert len(replaced) == 25
    assert all(r["superseded_at"] and r["superseded_by_run_id"] == summary.run_id for r in replaced)
    item = await one(
        app,
        "SELECT reviewer, review_note, reviewed_at, source_bbox, source_note, extraction_method "
        "FROM planning_parameter_extractions WHERE run_id = :run AND target_label = 'UP 4' "
        "AND field_key = 'max_far'",
        run=summary.run_id,
    )
    assert (item["reviewer"], item["review_note"]) == ("cli", NOTE) and item["reviewed_at"]
    assert len(item["source_bbox"]) == 4 and item["extraction_method"] == "table"
    assert item["source_note"] == "UP 4 – Indeks izgrađenosti"

    # the run and what the console shows of it
    run = await one(app, "SELECT * FROM extraction_runs WHERE id = :id", id=summary.run_id)
    assert (run["model"], run["status"], run["job_id"]) == (
        "table-reader",
        "ready_for_review",
        None,
    )
    assert (run["items_written"], run["items_superseded"]) == (20, 25)
    assert run["summary"]["data_sha256"] == "a" * 64 and run["estimated_cost_eur"] == 0
    assert run["summary"]["unmatched_parcels"] == ["UP 5"]
    assert document["state"] == "reviewed"
    assert document["review"]["pending"] == 0 and document["review"]["approved"] == 20
    assert document["files"][0]["extraction_state"] == "ready_for_review"
    assert document["files"][0]["extraction"]["id"] == summary.run_id

    # the audit row of the load
    audit = await one(
        app,
        "SELECT actor, action, note, before, after, details FROM audit_log "
        "WHERE entity_type = 'extraction_run' AND entity_id = :id",
        id=summary.run_id,
    )
    assert (audit["actor"], audit["action"], audit["note"]) == ("cli", ACTION, NOTE)
    assert audit["before"]["pending"] == 25 and audit["after"]["approved"] == 20
    assert audit["details"]["items_written"] == 20 and audit["details"]["reader"] == "table-reader"

    # the same data again changes nothing; a corrected file writes only what differs
    assert again.already_loaded and again.run_id == summary.run_id
    assert (corrected.items_written, corrected.items_kept) == (1, 19)
    assert corrected.retired_decisions == 1 and corrected.by_change == {"changed": 1}
    assert corrected.review_after["approved"] == 20 and corrected.superseded_items == 0

    # what the publish job would copy: one value per parcel and field, the corrected one
    async with app.state.session_factory() as session:
        eligible = [
            dict(r)
            for r in (
                await session.execute(text(ELIGIBLE_ITEMS_SQL), {"m": "podgorica"})
            ).mappings()
            if r["document_id"] == document_id
        ]
    assert len(eligible) == 20
    assert {e["source_file_id"] for e in eligible} == {document["files"][0]["file_id"]}
    floors = [e for e in eligible if e["field_key"] == "max_floors"]
    assert sorted(e["value_text"] for e in floors) == ["P+2", "P+3", "Po+P+7", "S+P+4+Pk"]


async def test_a_document_without_its_geometry_takes_nothing(env):  # noqa: F811
    from tests.pdf_synthetic import planning_pdf

    pdf = planning_pdf()
    doc, gold, pages = synthetic_prepared(pdf)
    prepared, _ = build_prepared(doc, gold, pages, municipality="podgorica")
    app = env(Transcriber())
    async with app.router.lifespan_context(app), make_client(app) as client:
        document_id = await register(client, pdf)
        with pytest.raises(PreparedLoadError, match="none of the 5 parcels"):
            await load_prepared(
                app.state.session_factory,
                municipality_id="podgorica",
                document_id=document_id,
                prepared=prepared,
                data_sha256="a" * 64,
                by="cli",
            )
    assert await items(app, document_id) == []
    assert (
        await one(app, "SELECT id FROM extraction_runs WHERE document_id = :d", d=document_id)
        is None
    )
