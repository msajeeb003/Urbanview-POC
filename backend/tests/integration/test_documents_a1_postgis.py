"""The A1 check of the admin Documents and files screen through the staff API on PostGIS: the
short code and the document edit route with its audit row, the municipality on the list, the
"needs QGIS redraw" pages of a PDF, the geometry job staging a document's GIS drawing (and what
it answers for a PDF drawing or a file that is no drawing), and the zone GeoPackage import."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from sqlalchemy import text

from jobs.base import SqlJobStore, configure_job_store
from jobs.tasks.extraction import configure_preprocess
from jobs.tasks.ingestion import configure_ingestion
from tests.helpers import make_app, make_client, make_settings
from tests.integration import test_admin_pipeline_postgis as pipeline
from tests.integration.test_preprocess_postgis import PreprocessStorage

storage = pipeline.storage
dispatcher = pipeline.dispatcher
admin_app = pipeline.admin_app
_clean_admin_rows = pipeline._clean_admin_rows
PDF_A, PDF_B = pipeline.PDF_A, pipeline.PDF_B
auth, upload, register = pipeline.auth, pipeline.upload, pipeline.register

pytestmark = pytest.mark.integration

M = "podgorica"
# rows these tests create beyond the pipeline clean-up: everything newer than the test's start
NEWER = (
    ("staging_zone_documents", "dataset_id IN (SELECT id FROM zone_datasets WHERE id > :zd)"),
    ("zone_datasets", "id > :zd"),
    ("georef_datasets", "id > :gd"),
    ("staging_geometry", "batch_id > :gb"),
    ("geometry_batches", "id > :gb"),
)


async def sql(app, statement: str, **params):
    async with app.state.session_factory() as session:
        result = await session.execute(text(statement), params)
        await session.commit()
        return result


@pytest.fixture
async def baseline(postgis_url):
    """Remove what the jobs staged (newer than the test's start) before the pipeline clean-up
    deletes the documents those rows point at."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(postgis_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as session:
            ids = (
                await session.execute(
                    text(
                        "SELECT (SELECT COALESCE(max(id), 0) FROM zone_datasets),"
                        " (SELECT COALESCE(max(id), 0) FROM georef_datasets),"
                        " (SELECT COALESCE(max(id), 0) FROM geometry_batches)"
                    )
                )
            ).one()
        yield
        async with async_sessionmaker(engine)() as session:
            for table, where in NEWER:
                await session.execute(
                    text(f"DELETE FROM {table} WHERE {where}"),
                    {"zd": ids[0], "gd": ids[1], "gb": ids[2]},
                )
            await session.commit()
    finally:
        await engine.dispose()


@pytest.fixture
def eager_env(postgis_url, monkeypatch, baseline):
    """An app whose jobs run inline against the test database, with a storage that reads back."""
    from jobs.celery_app import celery_app
    from jobs.enqueue import CeleryDispatcher

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    configure_job_store(SqlJobStore(database_url=postgis_url))
    store = PreprocessStorage()
    settings = make_settings(
        location_resolver="postgis",
        database_url=postgis_url,
        rate_limit_requests=100_000,
        admin_api_tokens=f"{pipeline.TOKEN}:admin:ops",
    )
    configure_preprocess(database_url=postgis_url, storage=store, settings=settings)
    configure_ingestion(database_url=postgis_url, storage=store)
    yield make_app(settings, storage=store, admin_dispatcher=CeleryDispatcher()), store
    configure_job_store(None)
    configure_preprocess(database_url=None, storage=None, settings=None)
    configure_ingestion(database_url=None, storage=None)


# --- documents: short code, edit, municipality ----------------------------------------------------


async def test_short_code_edit_route_and_municipality(admin_app):
    app = admin_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        file_a = (await upload(client, PDF_A, "a.pdf")).json()["file"]
        file_b = (await upload(client, PDF_B, "b.pdf")).json()["file"]
        v1 = await register(client, file_a["id"], short_code=" DUP-T1 ", zone_id=1)
        assert v1.status_code == 201, v1.text
        doc = v1.json()
        taken = await register(client, file_b["id"], name="Other", short_code="dup-t1")
        v2 = await client.post(
            "/v1/admin/documents",
            json={
                "name": "DUP Test v2",
                "type": "DUP",
                "status": "in_progress",
                "replaces_document_id": doc["id"],
            },
            headers=auth(),
        )
        listed = await client.get("/v1/admin/documents", headers=auth())

        new = v2.json()
        edit = await client.patch(
            f"/v1/admin/documents/{new['id']}",
            json={
                "status": "adopted",
                "short_code": "DUP-T2",
                "source_url": "https://x.test/1",
                "adopted_on": "2019-05-12",
                "licence_note": None,
            },
            headers=auth(),
        )
        same = await client.patch(
            f"/v1/admin/documents/{new['id']}", json={"status": "adopted"}, headers=auth()
        )
        old = await client.patch(
            f"/v1/admin/documents/{doc['id']}", json={"status": "adopted"}, headers=auth()
        )
        empty = await client.patch(f"/v1/admin/documents/{new['id']}", json={}, headers=auth())
        cleared = await client.patch(
            f"/v1/admin/documents/{new['id']}", json={"name": None}, headers=auth()
        )
        no_zone = await client.patch(
            f"/v1/admin/documents/{new['id']}", json={"zone_id": 999999}, headers=auth()
        )
        missing = await client.patch(
            "/v1/admin/documents/999999", json={"status": "adopted"}, headers=auth()
        )
        audit = (
            (
                await sql(
                    app,
                    "SELECT action, before, after, details FROM audit_log WHERE entity_type = "
                    "'planning_document' AND entity_id = :id AND action = 'document.update' "
                    "ORDER BY id",
                    id=new["id"],
                )
            )
            .mappings()
            .all()
        )

    assert doc["short_code"] == "DUP-T1" and doc["municipality_id"] == M
    assert taken.status_code == 409 and taken.json()["error"]["details"]["reason"] == (
        "short_code_taken"
    )
    assert v2.status_code == 201 and new["short_code"] == "DUP-T1"  # carried to the new version
    assert new["zone_id"] == 1
    body = listed.json()
    assert body["municipality"] == {"id": M, "name": "Podgorica"}
    assert all(d["municipality_id"] == M for d in body["items"])

    assert edit.status_code == 200, edit.text
    edited = edit.json()
    assert edited["status"] == "adopted" and edited["short_code"] == "DUP-T2"
    assert edited["source_url"] == "https://x.test/1" and edited["adopted_on"] == "2019-05-12"
    assert same.status_code == 200
    assert len(audit) == 1  # the unchanged status wrote nothing
    row = audit[0]
    assert row["before"] == {
        "status": "in_progress",
        "short_code": "DUP-T1",
        "source_url": None,
        "adopted_on": None,
    }
    assert row["after"] == {
        "status": "adopted",
        "short_code": "DUP-T2",
        "source_url": "https://x.test/1",
        "adopted_on": "2019-05-12",
    }
    assert row["details"]["fields"] == ["adopted_on", "short_code", "source_url", "status"]
    assert old.status_code == 409
    assert old.json()["error"]["details"]["reason"] == "not_current_version"
    assert empty.status_code == 422 and cleared.status_code == 422
    assert no_zone.status_code == 422 and missing.status_code == 404


async def test_reviewers_cannot_edit_documents(postgis_url, storage, dispatcher):
    settings = make_settings(
        location_resolver="postgis",
        database_url=postgis_url,
        rate_limit_requests=100_000,
        admin_api_tokens=f"{pipeline.TOKEN}:admin:ops,reviewer-token-5678:reviewer:rev",
    )
    app = make_app(settings, storage=storage, admin_dispatcher=dispatcher)
    async with app.router.lifespan_context(app), make_client(app) as client:
        file = (await upload(client, PDF_A, "a.pdf")).json()["file"]
        doc = (await register(client, file["id"])).json()
        denied = await client.patch(
            f"/v1/admin/documents/{doc['id']}",
            json={"status": "adopted"},
            headers=auth("reviewer-token-5678"),
        )
        zones = await client.post(
            "/v1/admin/zones/import",
            json={"file_id": file["id"]},
            headers=auth("reviewer-token-5678"),
        )
    assert denied.status_code == 403 and zones.status_code == 403


# --- the geometry job ---------------------------------------------------------------------------


def _feature(geometry: dict, **props) -> dict:
    return {"type": "Feature", "properties": props, "geometry": geometry}


async def _parcel_1001(app) -> dict:
    raw = (
        await sql(app, "SELECT ST_AsGeoJSON(geom) FROM cadastral_parcels WHERE id = 1001")
    ).scalar_one()
    return json.loads(raw)


def _zip(files: dict[str, dict]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, collection in files.items():
            archive.writestr(name, json.dumps(collection))
    return buffer.getvalue()


async def test_geometry_job_stages_a_gis_drawing_of_the_document(eager_env):
    app, store = eager_env
    async with app.router.lifespan_context(app), make_client(app) as client:
        parcel = await _parcel_1001(app)
        drawing = _zip(
            {
                "plan_boundary.geojson": {
                    "type": "FeatureCollection",
                    "features": [_feature(parcel)],
                },
                "urban_parcels.geojson": {
                    "type": "FeatureCollection",
                    "features": [_feature(parcel, URBAN_PARCEL_NUMBER=7, block_ref="B1")],
                },
            }
        )
        pdf = (await upload(client, PDF_A, "a.pdf")).json()["file"]
        gis = (
            await upload(client, drawing, "redraw.zip", kind="gis", mime="application/zip")
        ).json()["file"]
        loose = (
            await upload(
                client,
                _zip({"x.geojson": {"type": "FeatureCollection", "features": []}}),
                "loose.zip",
                kind="gis",
                mime="application/zip",
            )
        ).json()["file"]
        doc = (
            await client.post(
                "/v1/admin/documents",
                json={
                    "name": "DUP Geo",
                    "type": "DUP",
                    "status": "adopted",
                    "files": [
                        {"file_id": pdf["id"], "role": "text"},
                        {"file_id": gis["id"], "role": "drawing"},
                    ],
                },
                headers=auth(),
            )
        ).json()
        job = await client.post(f"/v1/admin/files/{gis['id']}/jobs/geo", headers=auth())
        not_drawing = await client.post(f"/v1/admin/files/{loose['id']}/jobs/geo", headers=auth())
        document = (await client.get(f"/v1/admin/documents/{doc['id']}", headers=auth())).json()
        staged = (
            (
                await sql(
                    app,
                    "SELECT layer_id, feature_key, properties FROM staging_geometry s "
                    "WHERE s.properties->>'document_id' = :d ORDER BY layer_id",
                    d=str(doc["id"]),
                )
            )
            .mappings()
            .all()
        )

    assert job.status_code == 202, job.text
    body = job.json()
    assert body["status"] == "succeeded", body["error"]
    result = body["result"]["documents"][0]
    assert result["status"] == "staged" and result["crs"] == "EPSG:4326"
    assert result["features"] == {"plan_boundary": 1, "urban_parcels": 1}
    assert set(result["batches"]) == {"document_coverage", "urban_parcels"}
    assert [(r["layer_id"], r["feature_key"]) for r in staged] == [
        ("document_coverage", str(doc["id"])),
        ("urban_parcels", f"{doc['id']}|UP 7"),
    ]
    assert staged[1]["properties"]["block_ref"] == "B1"
    geo = document["georeference"]
    assert geo["method"] == "native" and geo["source"] == "gis_file"
    assert geo["rmse_m"] is None and geo["points_used"] == 0 and geo["status"] == "staged"
    drawing_file = next(f for f in document["files"] if f["file_id"] == gis["id"])
    assert drawing_file["geometry_job"]["status"] == "succeeded"

    assert not_drawing.status_code == 202
    assert not_drawing.json()["status"] == "failed"
    assert "not a drawing of any current planning document" in not_drawing.json()["error"]


async def test_geometry_job_on_a_pdf_drawing_names_what_it_needs(eager_env):
    from tests.pdf_synthetic import planning_pdf

    app, _ = eager_env
    async with app.router.lifespan_context(app), make_client(app) as client:
        pdf = (await upload(client, planning_pdf(), "sheets.pdf")).json()["file"]
        vector = (await upload(client, PDF_B, "vector.pdf")).json()["file"]
        doc = (
            await client.post(
                "/v1/admin/documents",
                json={
                    "name": "DUP Sheets",
                    "type": "DUP",
                    "status": "adopted",
                    "files": [
                        {"file_id": pdf["id"], "role": "both"},
                        {"file_id": vector["id"], "role": "drawing"},
                    ],
                },
                headers=auth(),
            )
        ).json()
        scanned = await client.post(f"/v1/admin/files/{pdf['id']}/jobs/geo", headers=auth())
        plain = await client.post(f"/v1/admin/files/{vector['id']}/jobs/geo", headers=auth())
        document = (await client.get(f"/v1/admin/documents/{doc['id']}", headers=auth())).json()

    assert scanned.json()["status"] == "failed"
    assert "pages 3 are scanned sheets" in scanned.json()["error"]
    assert "redraw them in QGIS" in scanned.json()["error"]
    assert plain.json()["status"] == "failed"
    assert "python -m core.gis.georef" in plain.json()["error"]
    by_id = {f["file_id"]: f for f in document["files"]}
    assert by_id[pdf["id"]]["redraw_pages"] == [3]
    assert by_id[pdf["id"]]["preprocessing"]["redraw_pages"] == [3]
    assert by_id[vector["id"]]["redraw_pages"] == []


# --- the zone import ------------------------------------------------------------------------------


async def _zone_gpkg(app, tmp_path, *, invalid: bool = False) -> bytes:
    import shapely

    from core.zones import gpkg, schema

    box = (
        await sql(
            app,
            "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) FROM (SELECT ST_Extent(geom) "
            "AS e FROM cadastral_parcels WHERE municipality_id = :m) x",
            m=M,
        )
    ).one()
    x0, y0, x1, y1 = box[0] - 0.001, box[1] - 0.001, box[2] + 0.001, box[3] + 0.001
    mid = (x0 + x1) / 2
    west = shapely.box(x0, y0, mid, y1)
    if invalid:  # a bow tie: self-intersecting, refused by the validity check
        west = shapely.Polygon([(x0, y0), (mid, y1), (mid, y0), (x0, y1)])
    east = shapely.box(mid, y0, x1, y1)
    path = tmp_path / "zones.gpkg"
    conn = gpkg.create(path)
    gpkg.add_feature_table(conn, "zones", fields=schema.ZONE_FIELDS, srs=gpkg.srs_for(4326))
    gpkg.insert_features(
        conn,
        "zones",
        [
            (west, {"zone_id": "zapad", "name": "Zapad", "zone_type": "residential"}),
            (east, {"zone_id": "istok", "name": "Istok", "zone_type": "mixed"}),
        ],
        srs_id=4326,
    )
    gpkg.add_attribute_table(conn, "zone_documents", fields=schema.DOCUMENT_FIELDS)
    gpkg.insert_rows(
        conn,
        "zone_documents",
        [
            {
                "zone_id": "zapad",
                "document_name": "DUP Stari Aerodrom",
                "document_type": "DUP",
                "status": "adopted",
                "confirmed": 1,
            },
            {
                "zone_id": "istok",
                "document_name": "PUP Glavni grad (izvod)",
                "document_type": "PUP",
                "status": "adopted",
                "confirmed": 1,
            },
        ],
    )
    conn.commit()
    conn.close()
    return path.read_bytes()


async def test_zone_geopackage_import_validates_and_stages(eager_env, tmp_path):
    app, _ = eager_env
    async with app.router.lifespan_context(app), make_client(app) as client:
        good = await _zone_gpkg(app, tmp_path)
        (tmp_path / "zones.gpkg").unlink()
        bad = await _zone_gpkg(app, tmp_path, invalid=True)
        mime = "application/geopackage+sqlite3"
        good_file = (await upload(client, good, "zones.gpkg", kind="gis", mime=mime)).json()
        bad_file = (await upload(client, bad, "zones-invalid.gpkg", kind="gis", mime=mime)).json()
        pdf = (await upload(client, PDF_A, "a.pdf")).json()["file"]
        dry = await client.post(
            "/v1/admin/zones/import",
            json={"file_id": good_file["file"]["id"], "dry_run": True},
            headers=auth(),
        )
        real = await client.post(
            "/v1/admin/zones/import", json={"file_id": good_file["file"]["id"]}, headers=auth()
        )
        refused = await client.post(
            "/v1/admin/zones/import", json={"file_id": bad_file["file"]["id"]}, headers=auth()
        )
        not_gpkg = await client.post(
            "/v1/admin/zones/import", json={"file_id": pdf["id"]}, headers=auth()
        )
        listed = await client.get("/v1/admin/jobs", params={"type": "import_zones"}, headers=auth())
        dataset = (
            (
                await sql(
                    app,
                    "SELECT dataset_version, status, report IS NOT NULL AS has_report "
                    "FROM zone_datasets ORDER BY id DESC LIMIT 1",
                )
            )
            .mappings()
            .one()
        )
        audit = (
            (
                await sql(
                    app,
                    "SELECT actor, details FROM audit_log WHERE action = 'zones.import' "
                    "ORDER BY id DESC LIMIT 1",
                )
            )
            .mappings()
            .one()
        )

    assert dry.status_code == 202 and dry.json()["status"] == "succeeded", dry.json()["error"]
    assert dry.json()["result"]["status"] == "valid" and dry.json()["result"]["dry_run"] is True
    assert real.status_code == 202 and real.json()["status"] == "succeeded", real.json()["error"]
    result = real.json()["result"]
    assert result["status"] == "staged" and result["zones"] == 2 and result["documents"] == 2
    assert dataset["dataset_version"] == result["dataset_version"]
    assert dataset["status"] == "staged" and dataset["has_report"]
    assert audit["actor"] == "worker:import_zones" and audit["details"]["requested_by"] == "ops"
    assert refused.json()["status"] == "failed"
    error = refused.json()["error"]
    assert "import refused" in error and "zone_invalid_geometry" in error
    assert not_gpkg.status_code == 409
    assert not_gpkg.json()["error"]["details"]["reason"] == "not_a_geopackage"
    assert listed.json()["total"] == 3
