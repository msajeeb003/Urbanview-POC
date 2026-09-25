"""The publish pipeline on PostGIS, driven through the API with Celery in eager mode (a fake tile
builder and storage): refusal while items are pending (naming the document), an amended value
reaching the serving table, the panel and the tile layer, heatmap cells and parcel links, staged
geometry landing in the serving tables and the archive, the status screen with per-step
progress, rollback as a pointer flip, idempotent enqueueing, retention."""

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
    "DELETE FROM heatmap_cells WHERE municipality_id = 'podgorica'",
    "DELETE FROM layer_features WHERE municipality_id = 'podgorica'",
    "DELETE FROM staging_geometry WHERE municipality_id = 'podgorica'",
    "DELETE FROM geometry_batches WHERE municipality_id = 'podgorica'",
    "DELETE FROM cadastral_parcels WHERE ko_name = 'Test KO'",
    *RESTORE,
    "DELETE FROM planning_parameter_values WHERE publish_version_id <> 1",
    "DELETE FROM pipeline_jobs WHERE municipality_id = 'podgorica'",
    "DELETE FROM publish_versions WHERE id <> 1",
    "UPDATE publish_versions SET is_current = true, rolled_back_at = NULL, rolled_back_by = NULL, "
    "archive_pruned_at = NULL WHERE id = 1",
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
            f"/v1/admin/review/{item}/amend", json={"value": 3.6}, headers=auth()
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
    assert counts["parcel_links"] > 0 and counts["block_cells"] >= 2 and counts["zone_cells"] >= 2
    assert {layer["id"] for layer in result["layers"]} >= {
        "urban_parcels",
        "zone_cells",
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
    assert len(tiles.layers["zones"]) == 3 and len(tiles.layers["cadastral_parcels"]) >= 7
    assert len(tiles.layers["public_ownership"]) >= 1 and len(tiles.layers["legal_burdens"]) >= 1
    assert "land_use" not in tiles.layers  # empty layers are left out of the build

    # the archive and the public pointer
    key = result["archive_key"]
    assert key in storage.objects and storage.objects[key][1] == "application/vnd.pmtiles"
    pointer = tiles_now.json()
    assert pointer["status"] == "published" and pointer["data_version"] == "test-publish-1"
    assert key in pointer["archive_url"] and pointer["expires_at"] is not None
    assert {layer["id"]: layer["features"] for layer in pointer["layers"]}["urban_parcels"] >= 6
    assert (pointer["min_zoom"], pointer["max_zoom"]) == (8, 16)

    # the status screen
    body = status.json()
    assert body["current"]["label"] == "test-publish-1" and body["current"]["archive_url"]
    assert body["current"]["published_by"] == "vesna" and body["current"]["counts"] == counts
    assert [v["label"] for v in body["versions"]] == ["test-publish-1", "sample-2026-09-22"]
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
        "SELECT v.value_number, v.source_page, v.urban_parcel_id, e.published_value_id "
        "FROM planning_parameter_extractions e JOIN planning_parameter_values v "
        "ON v.id = e.published_value_id WHERE e.id = :id",
        id=item,
    )
    assert (published["value_number"], published["source_page"], published["urban_parcel_id"]) == (
        3.5,
        13,
        1,
    )
    actions = await rows(
        app,
        "SELECT action, actor FROM audit_log WHERE municipality_id = 'podgorica' "
        "AND action LIKE 'publish.%' ORDER BY id",
    )
    assert [a["action"] for a in actions] == ["publish.request", "publish.complete"]
    assert {a["actor"] for a in actions} == {"vesna"}

    # heatmap cells: block SA-01 holds UP 7 alone (FAR 2.4 on 1370.9 m2), zone 2's sale rate
    (block,) = await rows(
        app,
        "SELECT * FROM heatmap_cells WHERE cell_type = 'block' AND cell_id = 2 "
        "AND publish_version_id = :v",
        v=result["version_id"],
    )
    assert (block["parcel_count"], block["stated_count"], block["max_far"]) == (1, 1, 2.4)
    (up7,) = await rows(app, "SELECT area_m2 FROM urban_parcels WHERE id = 3")
    gfa = round(2.4 * up7["area_m2"], 2)  # the engine's basis is the planned parcel's area
    assert block["max_gfa_m2"] == pytest.approx(gfa) and block["sale_rate_eur_m2"] == 1650
    assert block["saleable_area_m2"] == pytest.approx(round(0.7 * gfa, 2), abs=0.02)
    assert block["price_band"] == 1
    assert block["market_value_eur"] > 0
    (zone,) = await rows(
        app,
        "SELECT * FROM heatmap_cells WHERE cell_type = 'zone' AND cell_id = 1 "
        "AND publish_version_id = :v",
        v=result["version_id"],
    )
    assert (
        zone["parcel_count"] >= 4 and zone["sale_rate_eur_m2"] == 2450 and zone["price_band"] == 2
    )
    block_cells = {f["id"]: f["properties"] for f in tiles.layers["block_cells"]}
    assert block_cells[2]["max_far"] == 2.4 and block_cells[2]["price_band"] == 1

    # parcel links: one primary per linked cadastral parcel, fractions within (0, 1]
    links = await rows(
        app,
        "SELECT cadastral_parcel_id, urban_parcel_id, rank, overlap_fraction FROM parcel_links "
        "WHERE publish_version_id = :v AND cadastral_parcel_id IN (1001, 1002, 1003)",
        v=result["version_id"],
    )
    assert any(link["cadastral_parcel_id"] == 1001 and link["rank"] == 1 for link in links)
    assert all(0 < link["overlap_fraction"] <= 1.0001 for link in links)
    primaries = [link for link in links if link["rank"] == 1]
    assert len({link["cadastral_parcel_id"] for link in primaries}) == len(primaries)
    cadastral = {f["id"]: f["properties"] for f in tiles.layers["cadastral_parcels"]}
    assert cadastral[1001]["has_urban_parcel"] is True
    assert cadastral[1001]["primary_urban_parcel_id"] == next(
        link["urban_parcel_id"] for link in primaries if link["cadastral_parcel_id"] == 1001
    )


# --- rollback -------------------------------------------------------------------------------------


async def test_rollback_flips_the_pointer_without_recomputing(publish_env, tiles):
    app = publish_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        await reject_seeded_pending_item(app)
        first = await insert_item(
            app, urban_parcel_id=1, field_key="max_far", value_number=3.5, source_page=13
        )
        assert (
            await client.post(f"/v1/admin/review/{first}/approve", headers=auth())
        ).status_code == 200
        version_a = (await publish(client, "test-a"))["result"]
        second = await insert_item(
            app, urban_parcel_id=1, field_key="max_height_m", value_number=30, source_page=14
        )
        assert (
            await client.post(f"/v1/admin/review/{second}/approve", headers=auth())
        ).status_code == 200
        version_b = (await publish(client, "test-b"))["result"]
        fields_b, body_b = await fields_of(client, 1)
        runs_before = len(tiles.runs)

        rolled = await client.post("/v1/admin/publish/rollback", json={}, headers=auth())
        fields_a, body_a = await fields_of(client, 1)
        pointer_a = (await client.get("/v1/tiles/current")).json()
        rolled_again = await client.post("/v1/admin/publish/rollback", json={}, headers=auth())
        fields_seed, body_seed = await fields_of(client, 1)
        pointer_seed = (await client.get("/v1/tiles/current")).json()
        nothing_earlier = await client.post("/v1/admin/publish/rollback", json={}, headers=auth())
        forward = await client.post(
            "/v1/admin/publish/rollback",
            json={"version_id": version_b["version_id"]},
            headers=auth(),
        )
        fields_b_again, _ = await fields_of(client, 1)
        already = await client.post(
            "/v1/admin/publish/rollback",
            json={"version_id": version_b["version_id"]},
            headers=auth(),
        )
        unknown = await client.post(
            "/v1/admin/publish/rollback", json={"version_id": 999_999}, headers=auth()
        )

    assert fields_b["max_far"]["value"] == 3.5 and fields_b["max_height_m"]["value"] == 30
    assert body_b["data_version"] == "test-b"
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["current"]["label"] == "test-a"
    rolled_back = next(v for v in rolled.json()["versions"] if v["label"] == "test-b")
    assert rolled_back["rolled_back_at"] and rolled_back["rolled_back_by"] == "vesna"
    assert fields_a["max_far"]["value"] == 3.5 and fields_a["max_height_m"]["value"] == 27.5
    assert body_a["data_version"] == "test-a"
    assert (
        pointer_a["data_version"] == "test-a"
        and version_a["archive_key"] in pointer_a["archive_url"]
    )
    assert len(tiles.runs) == runs_before  # nothing was rebuilt
    assert (
        rolled_again.status_code == 200
        and rolled_again.json()["current"]["label"] == "sample-2026-09-22"
    )
    assert (
        fields_seed["max_far"]["value"] == 3.2 and body_seed["data_version"] == "sample-2026-09-22"
    )
    assert pointer_seed["status"] == "published" and pointer_seed["archive_url"] is None
    assert nothing_earlier.status_code == 409
    assert nothing_earlier.json()["error"]["details"]["reason"] == "no_previous_version"
    assert forward.status_code == 200 and forward.json()["current"]["label"] == "test-b"
    assert fields_b_again["max_height_m"]["value"] == 30
    assert already.status_code == 409 and unknown.status_code == 404
    actions = await rows(
        app,
        "SELECT action, before, after FROM audit_log WHERE municipality_id = 'podgorica' "
        "AND action = 'publish.rollback' ORDER BY id",
    )
    assert len(actions) == 3
    assert (
        actions[0]["before"]["current_label"] == "test-b"
        and actions[0]["after"]["current_label"] == "test-a"
    )


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
        first = (await publish(client, "test-geo-1"))["result"]
        (inserted,) = await rows(
            app,
            "SELECT id, street_address, area_m2 FROM cadastral_parcels WHERE ko_name = 'Test KO'",
        )
        # a second batch updates the same parcel in place: the Parcel ID must not change
        await stage(
            app,
            "cadastral_parcels",
            [("Test KO|77|", SQUARE_MOVED, {**parcel, "street_address": "Nova 2"})],
        )
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
    assert inserted["id"] in {f["id"] for f in first_run["public_ownership"]}
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
        pruned = await client.post(
            "/v1/admin/publish/rollback", json={"version_id": a["version_id"]}, headers=auth()
        )
        status = (await client.get("/v1/admin/publish", headers=auth())).json()
        links = await rows(
            app,
            "SELECT publish_version_id AS v, count(*) AS n FROM parcel_links GROUP BY 1 ORDER BY 1",
        )
    assert c["pruned_versions"] == [
        {"version_id": a["version_id"], "label": "keep-a", "archive_key": a["archive_key"]}
    ]
    assert storage.deleted == [a["archive_key"]] and b["archive_key"] in storage.objects
    assert pruned.status_code == 409 and pruned.json()["error"]["details"]["reason"] == "pruned"
    by_label = {v["label"]: v for v in status["versions"]}
    assert by_label["keep-a"]["archive_pruned_at"] and by_label["keep-a"]["archive_key"] is None
    assert by_label["keep-b"]["archive_key"] and by_label["keep-c"]["is_current"]
    assert {row["v"] for row in links} == {b["version_id"], c["version_id"]}
