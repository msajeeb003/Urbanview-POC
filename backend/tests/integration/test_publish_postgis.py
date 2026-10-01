"""The publish pipeline on PostGIS, driven through the API with Celery in eager mode (a fake tile
builder and storage): refusal while items are pending (naming the document), an amended value
reaching the serving table, the panel and the tile layer, heatmap cells and parcel links, staged
geometry landing in the serving tables and the archive, the status screen with per-step
progress, idempotent enqueueing, retention."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.parcel_links import recompute_parcel_links
from jobs.base import SqlJobStore, configure_job_store
from jobs.tasks.publish import configure_publish
from jobs.tiles import TileBuildReport
from tests.helpers import make_app, make_client, make_settings
from tests.integration.test_admin_pipeline_postgis import FakeDispatcher, FakeStorage
from tests.integration.test_review_postgis import RESTORE, insert_item

pytestmark = pytest.mark.integration

ADMIN = "admin-token-1234"
REVIEWER = "reviewer-token-1234"
EXPERT = "expert-token-1234"
TOKENS = f"{ADMIN}:admin:ops,{REVIEWER}:reviewer:vesna,{EXPERT}:expert:marko"
CLEANUP = (
    "DELETE FROM parcel_links WHERE publish_version_id <> 1",
    "DELETE FROM choropleth_cells WHERE municipality_id = 'podgorica'",
    "DELETE FROM choropleth_classes WHERE municipality_id = 'podgorica'",
    "DELETE FROM layer_features WHERE municipality_id = 'podgorica'",
    "DELETE FROM staging_geometry WHERE municipality_id = 'podgorica'",
    "DELETE FROM geometry_batches WHERE municipality_id = 'podgorica'",
    "DELETE FROM cadastral_parcels WHERE ko_name = 'Test KO'",
    *RESTORE,
    "DELETE FROM planning_parameter_values WHERE publish_version_id <> 1",
    "DELETE FROM pipeline_jobs WHERE municipality_id = 'podgorica'",
    "DELETE FROM publish_versions WHERE id <> 1",
    "UPDATE publish_versions SET is_current = true, archive_pruned_at = NULL WHERE id = 1",
)
SQUARE = (
    "MULTIPOLYGON(((19.4400 42.5400, 19.4410 42.5400, 19.4410 42.5410, 19.4400 42.5410, "
    "19.4400 42.5400)))"
)
SQUARE_MOVED = (
    "MULTIPOLYGON(((19.4400 42.5400, 19.4412 42.5400, 19.4412 42.5412, 19.4400 42.5412, "
    "19.4400 42.5400)))"
)


class PublishStorage(FakeStorage):
    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[str] = []

    def put_file(self, key, path, content_type="application/octet-stream"):
        self.objects[key] = (Path(path).read_bytes(), content_type)
        return key

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)


class FakeTileBuilder:
    """Reads every exported layer (newline-delimited GeoJSON) and writes a small archive."""

    def __init__(self) -> None:
        self.runs: list[dict[str, list[dict]]] = []

    @property
    def layers(self) -> dict[str, list[dict]]:
        return self.runs[-1]

    def build(self, layers, archive):
        captured: dict[str, list[dict]] = {}
        for layer in layers:
            with layer.path.open(encoding="utf-8") as handle:
                captured[layer.layer_id] = [json.loads(line) for line in handle if line.strip()]
            assert len(captured[layer.layer_id]) == layer.feature_count
        self.runs.append(captured)
        payload = json.dumps({k: len(v) for k, v in captured.items()}).encode()
        archive.write_bytes(payload)
        return TileBuildReport(
            archive=archive,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            layers=tuple(layers),
            tool="fake",
        )


async def reset_publish_state(postgis_url: str) -> None:
    """Back to the seeded state: only version 1 (current, with its links), no staged geometry,
    the seeded review items restored. Retention may have pruned version 1's links; the parcel
    panel needs them (the seed loader computes them once), so they are recomputed when missing."""
    engine = create_async_engine(postgis_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as session:
            for statement in CLEANUP:
                await session.execute(text(statement))
            missing = (
                await session.execute(
                    text(
                        "SELECT NOT EXISTS (SELECT 1 FROM parcel_links "
                        "WHERE publish_version_id = 1)"
                    )
                )
            ).scalar_one()
            if missing:
                await recompute_parcel_links(session, municipality_id="podgorica", version_id=1)
            await session.commit()
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean(postgis_url):
    await reset_publish_state(postgis_url)
    yield
    await reset_publish_state(postgis_url)


@pytest.fixture
def storage():
    return PublishStorage()


@pytest.fixture
def tiles():
    return FakeTileBuilder()


@pytest.fixture
def publish_env(postgis_url, storage, tiles, monkeypatch):
    """``build(**overrides)`` -> an app whose publish job runs inline (eager Celery) against the
    test database with the fake storage and tile builder."""
    from jobs.celery_app import celery_app
    from jobs.enqueue import CeleryDispatcher

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    configure_job_store(SqlJobStore(database_url=postgis_url))

    def build(dispatcher=None, **overrides):
        settings = make_settings(
            location_resolver="postgis",
            database_url=postgis_url,
            rate_limit_requests=100_000,
            admin_api_tokens=TOKENS,
            **overrides,
        )
        configure_publish(
            database_url=postgis_url, storage=storage, tile_builder=tiles, settings=settings
        )
        return make_app(
            settings, storage=storage, admin_dispatcher=dispatcher or CeleryDispatcher()
        )

    yield build
    configure_job_store(None)
    configure_publish(database_url=None, storage=None, tile_builder=None, settings=None)


def auth(token: str = REVIEWER) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def reject_seeded_pending_item(app) -> None:
    """Test setup, not a review decision: the seeded pending item would block every publish.
    Done in SQL so no audit row lands on entity 1 (audit_log is append-only and the review tests
    assert entity 1's exact audit trail); RESTORE puts the row back after the test."""
    async with app.state.session_factory() as session:
        await session.execute(
            text(
                "UPDATE planning_parameter_extractions SET review_state = 'rejected', "
                "reviewer = 'test-setup', review_note = 'sample staging row' WHERE id = 1"
            )
        )
        await session.commit()


async def publish(client, label: str, token: str = REVIEWER):
    r = await client.post("/v1/admin/publish", json={"label": label}, headers=auth(token))
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "succeeded", job
    return job


async def fields_of(client, parcel_id: int) -> tuple[dict[str, dict], dict]:
    r = await client.get("/v1/panel", params={"type": "urban", "id": parcel_id})
    assert r.status_code == 200, r.text
    body = r.json()
    return {f["key"]: f for f in body["planning"]["fields"]}, body


async def approve_geometry(client, token: str = REVIEWER) -> list[int]:
    """Geometry review (0033): staged batches publish only once a reviewer approved them."""
    listing = await client.get(
        "/v1/admin/geometry", params={"status": "pending", "limit": 200}, headers=auth(token)
    )
    assert listing.status_code == 200, listing.text
    approved = [draft["id"] for draft in listing.json()["items"]]
    for batch_id in approved:
        r = await client.post(f"/v1/admin/geometry/{batch_id}/approve", headers=auth(token))
        assert r.status_code == 200, r.text
    return approved


async def rows(app, sql: str, **params) -> list[dict]:
    async with app.state.session_factory() as session:
        return [dict(r) for r in (await session.execute(text(sql), params)).mappings()]


# --- refusal ------------------------------------------------------------------------------------


async def test_publish_is_refused_while_items_are_pending(publish_env):
    app = publish_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        refused = await client.post("/v1/admin/publish", json={}, headers=auth())
        status = await client.get("/v1/admin/publish", headers=auth())
        forbidden = await client.post("/v1/admin/publish", json={}, headers=auth(EXPERT))
        anonymous = await client.post("/v1/admin/publish", json={})
    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["details"]["reason"] == "pending_review"
    assert error["details"]["documents"] == [
        {
            "document_id": 2,
            "document_name": status.json()["blockers"][0]["document_name"],
            "pending": 1,
        }
    ]
    assert "pending review" in error["message"]
    body = status.json()
    assert body["can_publish"] is False and body["active_job"] is None
    assert (
        body["current"]["label"] == "sample-2026-09-22" and body["current"]["archive_url"] is None
    )
    assert forbidden.status_code == 403 and anonymous.status_code == 401


# --- the amended value ---------------------------------------------------------------------------


async def test_an_amended_value_reaches_the_panel_and_the_tile_layer(publish_env, storage, tiles):
    app = publish_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        await reject_seeded_pending_item(app)
        item = await insert_item(
            app, urban_parcel_id=1, field_key="max_far", value_number=3.9, source_page=13
        )
        amended = await client.post(
            f"/v1/admin/review/{item}/amend",
            json={"value": 3.5, "note": "table 3 states 3.5"},
            headers=auth(),
        )
        assert amended.status_code == 200, amended.text
        before_fields, _ = await fields_of(client, 1)
        job = await publish(client, "test-publish-1")
        after_fields, after = await fields_of(client, 1)
        tiles_now = await client.get("/v1/tiles/current")
        status = await client.get("/v1/admin/publish", headers=auth())
        closed = await client.post(
            f"/v1/admin/review/{item}/amend",
            json={"value": 3.6, "note": "too late"},
            headers=auth(),
        )
        job_row = await client.get(job["status_url"], headers=auth(ADMIN))

    # the job
    result = job["result"]
    assert result["label"] == "test-publish-1" and result["archive_key"].endswith(
        "/test-publish-1.pmtiles"
    )
    counts = result["counts"]
    assert counts["values_published"] == 1 and counts["items_skipped"] == 0
    assert counts["values_carried"] == 39  # 40 seeded values minus the overridden one
    heat = counts["choropleth_cells"]
    assert counts["parcel_links"] > 0 and heat["far"] >= 2 and heat["sale_price"] >= 2
    assert {layer["id"] for layer in result["layers"]} >= {
        "urban_parcels",
        "heat_far",
        "heat_sale_price",
        "land_use",
    }

    # the panel: the corrected value, cited from the item's page, under the new data version
    assert before_fields["max_far"]["value"] == 3.2 and after_fields["max_far"]["value"] == 3.5
    assert after_fields["max_far"]["source"]["page"] == 13
    assert after_fields["max_far"]["status"] == "stated"
    assert after_fields["max_site_coverage_pct"]["value"] == 55  # carried forward
    assert after["data_version"] == "test-publish-1"
    assert after_fields["max_gfa_m2"]["value"] == pytest.approx(3.5 * 959.6, abs=0.05)

    # the tile layer: the same corrected value on the parcel feature
    urban = {f["id"]: f["properties"] for f in tiles.layers["urban_parcels"]}
    assert urban[1]["max_far"] == 3.5 and urban[1]["max_gfa_m2"] == pytest.approx(3358.6)
    assert urban[3]["max_far"] == 2.4 and urban[2]["max_far"] is None
    assert len(tiles.layers["zones"]) == 2 and len(tiles.layers["cadastral_parcels"]) >= 7
    assert "land_use" not in tiles.layers  # empty layers are left out of the build

    # the archive and the public pointer
    key = result["archive_key"]
    assert key in storage.objects and storage.objects[key][1] == "application/vnd.pmtiles"
    pointer = tiles_now.json()
    assert pointer["status"] == "published" and pointer["data_version"] == "test-publish-1"
    assert key in pointer["archive_url"] and pointer["expires_at"] is not None
    # the pilot scope's pointer: the current version's tiles key and number (the seed is 1)
    assert pointer["archive_key"] == key and pointer["version_no"] == 2
    assert {layer["id"]: layer["features"] for layer in pointer["layers"]}["urban_parcels"] >= 6
    assert (pointer["min_zoom"], pointer["max_zoom"]) == (8, 16)

    # the status screen
    body = status.json()
    assert body["current"]["label"] == "test-publish-1" and body["current"]["archive_url"]
    assert body["current"]["published_by"] == "vesna" and body["current"]["counts"] == counts
    assert [(v["version_no"], v["label"]) for v in body["versions"]] == [
        (2, "test-publish-1"),
        (1, "sample-2026-09-22"),
    ]
    assert body["versions"][0]["archive_key"] == key
    assert body["active_job"] is None and body["last_job"]["id"] == job["id"]
    steps = {s["name"]: s["status"] for s in job_row.json()["progress"]["steps"]}
    assert set(steps.values()) == {"done"} and job_row.json()["progress"]["step"] == "prune"
    assert body["can_publish"] is True

    # the item is closed, the old version's rows are untouched, the audit trail is complete
    assert closed.status_code == 409
    (version_rows,) = await rows(
        app,
        "SELECT count(*) AS n FROM planning_parameter_values WHERE publish_version_id = 1",
    )
    assert version_rows["n"] == 40
    (published,) = await rows(
        app,
        "SELECT v.value_number, v.source_page, v.urban_parcel_id, e.published_value_id, "
        "e.published_version_id FROM planning_parameter_extractions e "
        "JOIN planning_parameter_values v ON v.id = e.published_value_id WHERE e.id = :id",
        id=item,
    )
    assert (published["value_number"], published["source_page"], published["urban_parcel_id"]) == (
        3.5,
        13,
        1,
    )
    assert published["published_version_id"] == result["version_id"]
    # this run's rows (audit_log is append-only: earlier tests' publishes stay in it)
    actions = await rows(
        app,
        "SELECT action, actor, before, after FROM audit_log WHERE municipality_id = 'podgorica' "
        "AND action LIKE 'publish.%' AND ((entity_type = 'pipeline_job' AND entity_id = :job) "
        "OR (entity_type = 'publish_version' AND entity_id = :v)) ORDER BY id",
        job=job["id"],
        v=result["version_id"],
    )
    assert [a["action"] for a in actions] == ["publish.request", "publish.complete"]
    assert {a["actor"] for a in actions} == {"vesna"}
    assert actions[1]["before"] == {"current_version_id": 1, "current_label": "sample-2026-09-22"}
    assert actions[1]["after"] == {
        "current_version_id": result["version_id"],
        "current_label": "test-publish-1",
    }

    # heatmap cells: block SA-01 holds UP 7 alone (FAR 2.4, P+5+Pk = 7 floors), the zones'
    # sale prices exactly as their assumptions state them, in the profile's bands
    cells = {
        (r["layer"], r["cell_id"]): r
        for r in await rows(
            app,
            "SELECT * FROM choropleth_cells WHERE publish_version_id = :v",
            v=result["version_id"],
        )
    }
    far = cells[("far", 2)]
    assert (far["value"], far["parcel_count"], far["source_kind"]) == (2.4, 1, "planning")
    gfa = cells[("gfa", 2)]["value"]
    # the engine's formula on the area the plan states for UP 7 (1370.9; drawn: 1371.1)
    assert gfa == pytest.approx(2.4 * 1370.9, abs=0.01)
    assert (cells[("height", 2)]["value"], cells[("height", 2)]["label"]) == (7, "P+5+Pk")
    for zone_id, rate, band_no in ((1, 2450, 4), (2, 1650, 2)):
        zone = cells[("sale_price", zone_id)]
        assert (zone["value"], zone["value_band"], zone["unit"]) == (rate, band_no, "€/m²")
        assert zone["source_kind"] == "assumptions" and zone["assumptions_id"] is not None
    heat_far = {f["id"]: f["properties"] for f in tiles.layers["heat_far"]}
    assert heat_far[2]["value"] == 2.4 and "band" in heat_far[2]

    # parcel links: one rank-1 row per cadastral parcel (its primary link or its none row),
    # shares within (0, 1]
    links = await rows(
        app,
        "SELECT cadastral_parcel_id, urban_parcel_id, rank, overlap_ratio_of_cadastral AS ratio, "
        "relation FROM parcel_links "
        "WHERE publish_version_id = :v AND cadastral_parcel_id IN (1001, 1002, 1003)",
        v=result["version_id"],
    )
    assert any(link["cadastral_parcel_id"] == 1001 and link["rank"] == 1 for link in links)
    linked = [link for link in links if link["urban_parcel_id"] is not None]
    assert linked and all(0 < link["ratio"] <= 1.0001 for link in linked)
    assert all(link["relation"] == "none" for link in links if link["urban_parcel_id"] is None)
    primaries = [link for link in links if link["rank"] == 1]
    assert len({link["cadastral_parcel_id"] for link in primaries}) == len(primaries)
    cadastral = {f["id"]: f["properties"] for f in tiles.layers["cadastral_parcels"]}
    assert cadastral[1001]["has_urban_parcel"] is True
    assert cadastral[1001]["primary_urban_parcel_id"] == next(
        link["urban_parcel_id"] for link in primaries if link["cadastral_parcel_id"] == 1001
    )


async def test_a_failed_publish_leaves_no_archive_behind(publish_env, storage, monkeypatch):
    """The flip fails after the upload: the version is rolled back with the transaction and the
    archive it uploaded is deleted again, so the bucket holds nothing the map could never serve."""
    import jobs.publish_pipeline as pipeline

    async def failing_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(pipeline, "write_audit", failing_audit)
    app = publish_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        await reject_seeded_pending_item(app)
        failed = await client.post("/v1/admin/publish", json={"label": "test-fail"}, headers=auth())
        status = (await client.get("/v1/admin/publish", headers=auth())).json()
        versions = await rows(app, "SELECT id, is_current FROM publish_versions ORDER BY id")

    job = failed.json()
    assert failed.status_code == 202 and job["status"] == "failed"
    assert job["error"] == "RuntimeError: audit unavailable"
    steps = {s["name"]: s["status"] for s in job["progress"]["steps"]}
    assert steps["upload"] == "done" and steps["flip"] == "failed"
    key = next(
        s["detail"]["archive_key"] for s in job["progress"]["steps"] if s["name"] == "upload"
    )
    assert storage.deleted == [key] and key not in storage.objects
    assert versions == [{"id": 1, "is_current": True}]  # nothing left behind
    assert status["current"]["label"] == "sample-2026-09-22"


async def test_published_values_refuse_update(publish_env):
    """Serving rows are never updated in place (migration 0034): the next version carries its own
    rows, so a published value can only be read."""
    from sqlalchemy.exc import DBAPIError

    app = publish_env()
    async with app.router.lifespan_context(app), app.state.session_factory() as session:
        with pytest.raises(DBAPIError, match="immutable once published"):
            await session.execute(
                text(
                    "UPDATE planning_parameter_values SET value_number = 99 "
                    "WHERE id = (SELECT min(id) FROM planning_parameter_values)"
                )
            )
        await session.rollback()


# --- staged geometry ------------------------------------------------------------------------------


async def stage(app, layer_id: str, features: list[tuple[str, str, dict]]) -> int:
    async with app.state.session_factory() as session:
        batch = (
            await session.execute(
                text(
                    "INSERT INTO geometry_batches (municipality_id, layer_id, feature_count, "
                    "produced_by) VALUES ('podgorica', :layer, :n, 'test') RETURNING id"
                ),
                {"layer": layer_id, "n": len(features)},
            )
        ).scalar_one()
        for key, wkt, properties in features:
            await session.execute(
                text(
                    "INSERT INTO staging_geometry (municipality_id, batch_id, layer_id, "
                    "feature_key, geom, properties) VALUES ('podgorica', :batch, :layer, :key, "
                    "ST_GeomFromText(:wkt, 4326), CAST(:props AS jsonb))"
                ),
                {
                    "batch": batch,
                    "layer": layer_id,
                    "key": key,
                    "wkt": wkt,
                    "props": json.dumps(properties),
                },
            )
        await session.commit()
    return int(batch)


async def test_staged_geometry_lands_in_the_serving_tables_and_the_archive(publish_env, tiles):
    app = publish_env()
    parcel = {
        "ko_name": "Test KO",
        "parcel_number": "77",
        "sub_number": "",
        "street_address": "Nova 1",
        "public_ownership": True,
    }
    async with app.router.lifespan_context(app), make_client(app) as client:
        await reject_seeded_pending_item(app)
        cadastral_batch = await stage(app, "cadastral_parcels", [("Test KO|77|", SQUARE, parcel)])
        land_use_batch = await stage(
            app,
            "land_use",
            [("lu-1", SQUARE, {"code": "S", "name": "Stanovanje", "category": "residential"})],
        )
        assert sorted(await approve_geometry(client)) == [cadastral_batch, land_use_batch]
        first = (await publish(client, "test-geo-1"))["result"]
        (inserted,) = await rows(
            app,
            "SELECT id, street_address, area_m2, public_ownership FROM cadastral_parcels "
            "WHERE ko_name = 'Test KO'",
        )
        # a second batch updates the same parcel in place: the Parcel ID must not change
        await stage(
            app,
            "cadastral_parcels",
            [("Test KO|77|", SQUARE_MOVED, {**parcel, "street_address": "Nova 2"})],
        )
        assert len(await approve_geometry(client)) == 1
        second = (await publish(client, "test-geo-2"))["result"]
        (updated,) = await rows(
            app,
            "SELECT id, street_address, area_m2 FROM cadastral_parcels WHERE ko_name = 'Test KO'",
        )
        batches = await rows(
            app,
            "SELECT id, status, published_version_id FROM geometry_batches ORDER BY id",
        )
        features = await rows(
            app,
            "SELECT publish_version_id, layer_id, feature_key, properties FROM layer_features "
            "ORDER BY publish_version_id, id",
        )

    assert first["counts"]["batches_published"] == 2
    assert first["counts"]["geometry"] == {"cadastral_parcels": 1, "land_use": 1}
    assert first["counts"]["cadastral_unmatched"] >= 1  # the corner parcel has no plan
    assert inserted["street_address"] == "Nova 1" and inserted["area_m2"] > 0
    first_run = tiles.runs[-2]
    cadastral = {f["id"]: f["properties"] for f in first_run["cadastral_parcels"]}
    assert cadastral[inserted["id"]]["has_urban_parcel"] is False
    assert cadastral[inserted["id"]]["primary_urban_parcel_id"] is None
    # the flag is stored on the parcel, never drawn: no ownership layer in the POC
    assert inserted["public_ownership"] is True
    assert "public_ownership" not in cadastral[inserted["id"]]
    land_use = first_run["land_use"]
    assert len(land_use) == 1 and land_use[0]["properties"]["code"] == "S"
    assert land_use[0]["properties"]["feature_key"] == "lu-1"
    assert land_use[0]["geometry"]["type"] == "MultiPolygon"

    assert updated["id"] == inserted["id"] and updated["street_address"] == "Nova 2"
    assert updated["area_m2"] > inserted["area_m2"]
    assert second["counts"]["geometry"] == {"cadastral_parcels": 1, "land_use_carried": 1}
    assert len(tiles.layers["land_use"]) == 1  # carried forward without a new batch
    assert [(b["status"], b["published_version_id"] is not None) for b in batches] == [
        ("published", True),
        ("published", True),
        ("published", True),
    ]
    assert batches[0]["id"] == cadastral_batch and batches[1]["id"] == land_use_batch
    assert [(f["publish_version_id"] == first["version_id"], f["layer_id"]) for f in features] == [
        (True, "land_use"),
        (False, "land_use"),
    ]
    assert features[1]["publish_version_id"] == second["version_id"]


# --- idempotency and retention --------------------------------------------------------------------


async def test_a_second_publish_request_returns_the_active_job(publish_env):
    dispatcher = FakeDispatcher()  # records the message; the job stays queued
    app = publish_env(dispatcher=dispatcher)
    async with app.router.lifespan_context(app), make_client(app) as client:
        await reject_seeded_pending_item(app)
        first = await client.post("/v1/admin/publish", json={"notes": "go"}, headers=auth())
        second = await client.post("/v1/admin/publish", json={}, headers=auth(ADMIN))
        status = await client.get("/v1/admin/publish", headers=auth())
    assert first.status_code == 202 and second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    job = first.json()
    assert job["type"] == "publish_approved" and job["queue"] == "publish"
    assert job["dedupe_key"] == "publish_approved:publish_run:-" and job["max_attempts"] == 1
    assert job["payload"]["requested_by"] == "vesna" and job["payload"]["notes"] == "go"
    assert dispatcher.calls == [("publish_approved", job["id"], "podgorica")]
    body = status.json()
    assert body["active_job"]["id"] == job["id"] and body["can_publish"] is False


async def test_retention_prunes_archives_beyond_keep_versions(publish_env, storage):
    app = publish_env(publish_keep_versions=2)
    async with app.router.lifespan_context(app), make_client(app) as client:
        await reject_seeded_pending_item(app)
        a = (await publish(client, "keep-a"))["result"]
        b = (await publish(client, "keep-b"))["result"]
        c = (await publish(client, "keep-c"))["result"]
        status = (await client.get("/v1/admin/publish", headers=auth())).json()
        links = await rows(
            app,
            "SELECT publish_version_id AS v, count(*) AS n FROM parcel_links GROUP BY 1 ORDER BY 1",
        )
    assert c["pruned_versions"] == [
        {"version_id": a["version_id"], "label": "keep-a", "archive_key": a["archive_key"]}
    ]
    assert storage.deleted == [a["archive_key"]] and b["archive_key"] in storage.objects
    # every version is listed (the pruned one and the seed too), numbered in publish order
    assert [(v["version_no"], v["label"]) for v in status["versions"]] == [
        (4, "keep-c"),
        (3, "keep-b"),
        (2, "keep-a"),
        (1, "sample-2026-09-22"),
    ]
    by_label = {v["label"]: v for v in status["versions"]}
    assert by_label["keep-a"]["archive_pruned_at"] and by_label["keep-a"]["archive_key"] is None
    assert by_label["keep-b"]["archive_key"] and by_label["keep-c"]["is_current"]
    assert {row["v"] for row in links} == {b["version_id"], c["version_id"]}
