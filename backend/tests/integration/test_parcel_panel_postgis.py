"""The display-shaped panels on PostGIS: every Group 1 value of a test parcel opens its cited page
through the source viewer; a plan that omits max height gives null with ``not_in_document`` while
Group 2 still computes with that input flagged; the split, document-fallback, cadastral-basis and
uncovered cases; Group 2 equal to ``GET /v1/panel``, to the Python engine and to the TypeScript
engine on the exposed inputs; the zone panel; the per-version cache (hit, 304, new keys after an
assumptions change, a coverage switch or a version flip; one statement per hit); index-backed plans;
p95 under 200 ms; and ``rejected`` after a publish that records an expert rejection."""

from __future__ import annotations

import json
import shutil
import statistics
import subprocess
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, text

from api.services.parcel_panel_sql import PARCEL_PANEL_SQL, ZONE_PANEL_SQL
from core.engine import shared
from jobs.base import SqlJobStore, configure_job_store
from jobs.tasks.publish import configure_publish
from tests.helpers import make_app, make_client, make_settings
from tests.integration.test_panel_postgis import _assert_indexed_and_fast, _explain, _nodes
from tests.integration.test_publish_postgis import (
    FakeTileBuilder,
    PublishStorage,
    reject_seeded_pending_item,
    reset_publish_state,
)
from tests.integration.test_review_postgis import insert_item
from tests.test_source import FakeStorage as SourceStorage

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
ENGINE_BUNDLE = REPO_ROOT / "packages" / "feasibility-engine" / "dist" / "index.cjs"
NODE_CALCULATE = (
    "const e=require(process.argv[1]);let s='';process.stdin.on('data',d=>s+=d)"
    ".on('end',()=>process.stdout.write(JSON.stringify(e.calculate(JSON.parse(s)))))"
)
TOKEN = "admin-token-1234"


def parcel_url(parcel_id: int) -> str:
    return f"/v1/parcels/{parcel_id}/panel"


@pytest.fixture
def app(pg_settings):
    return make_app(pg_settings, storage=SourceStorage())


@pytest_asyncio.fixture
async def client(app):
    async with app.router.lifespan_context(app), make_client(app) as c:
        yield c


async def panel(client, parcel_id: int) -> dict[str, Any]:
    r = await client.get(parcel_url(parcel_id))
    assert r.status_code == 200, r.text
    return r.json()


def by_key(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in items}


async def execute(conn, sql: str, **params: Any) -> None:
    await conn.execute(text(sql), params)
    await conn.commit()


# --- Group 1 and the header -----------------------------------------------------------------------


async def test_every_group1_value_opens_the_cited_page(client):
    body = await panel(client, 1001)
    fields = body["group1"]["fields"]
    assert len(fields) == 11 and all(f["status"] == "stated" for f in fields)
    for field in fields:
        source = field["source"]
        assert set(source) >= {"document", "page", "bbox", "file_id", "value_id", "viewer_url"}
        assert source["document_id"] == 2 and source["document"] == "DUP Centar – Zona C2"
        assert source["file_id"] is None  # the seeded document has no stored_files record
        r = await client.get(source["viewer_url"])
        assert r.status_code == 200, (field["key"], r.text)
        page = r.json()
        assert (page["document_id"], page["page"]) == (source["document_id"], source["page"])
        assert page["kind"] == "pdf_page" and page["url"].endswith(f"#page={source['page']}")
        assert page["value"]["value_id"] == source["value_id"]
        assert page["value"]["field_key"] == field["key"]
        assert page["value"]["bbox"] == source["bbox"]
    assert by_key(fields)["max_far"]["value"] == 3.2

    header = body["header"]
    assert (header["ko"], header["parcel_number"], header["title"]) == (
        "Podgorica I",
        "1042",
        "KO Podgorica I, 1042",
    )
    assert header["zone"]["name"] == "Centar"
    assert [(d["id"], d["role"], d["status"]) for d in header["documents"]] == [
        (2, "governing", "adopted"),
        (3, "amendment", "in_progress"),
    ]
    areas = header["areas"]
    assert (areas["cadastral_m2"], areas["planned_m2"], areas["mismatch"]) == (1370.9, 959.6, True)
    assert areas["planned_stated_m2"] == 959.6 and areas["delta_pct"] == -30.0
    basis = header["calculation_basis"]
    assert (basis["basis"], basis["area_m2"], basis["reason"], basis["split"]) == (
        "urban",
        959.6,
        "planned_parcel",
        False,
    )
    # UP 13 touches the parcel with 8.2 m² (< 2 % of it): below the link threshold, not listed
    assert [link["urban_parcel_number"] for link in basis["links"]] == ["UP 12"]
    assert basis["links_source"] == "parcel_links" and "UP 12" in basis["explanation_en"]
    assert body["covered"] is True and body["data_version"] == "sample-2026-09-22"
    assert body["version_id"] == 1 and body["formula_version"] == "poc-1"
    assert body["client_validated"] is False and body["type"] == "parcel"


async def test_a_plan_without_max_height_is_null_and_group2_still_computes(client):
    body = await panel(client, 1002)
    height = by_key(body["group1"]["fields"])["max_height_m"]
    assert height["value"] is None and height["status"] == "not_stated"
    assert height["reason"] == "not_in_document" and height["source"] is None
    assert height["reason_en"] and height["reason_me"]
    group2 = body["group2"]
    assert group2["status"] == "ok"
    assert all(f["status"] == "ok" for f in group2["fields"])
    assert [f["key"] for f in group2["fields"]] == [
        "land_value_eur",
        "design_documentation_eur",
        "construction_cost_eur",
        "revenue_eur",
        "saleable_area_m2",
        "profit_eur",
        "roi_pct",
    ]
    flags = group2["input_flags"]
    assert [(f["key"], f["reason"], f["used_by_formulas"]) for f in flags] == [
        ("max_height_m", "not_in_document", False)
    ]
    header = body["header"]
    assert header["zone"]["name"] == "Stari Aerodrom" and header["ko"] == "Podgorica II"
    assert header["calculation_basis"]["links"][0]["urban_parcel_number"] == "UP 7"
    market = body["market"]
    assert market["scope"] == "zone" and market["zone"]["name"] == "Stari Aerodrom"
    assert market["sale_price_eur_m2"] == {
        "low": 1419.0,
        "expected": 1650.0,
        "high": 1897.5,
        "kind": "multiplier",
    }
    assert market["source"] and market["source_date"] == "2026-08-01"
    items = by_key(body["assumptions"]["items"])
    assert items["construction_cost_eur_m2"]["value"] == 780
    assert items["saleable_share"]["value"] == 0.7
    assert body["assumptions"]["formula_version"] == "poc-1"


async def test_split_document_fallback_cadastral_basis_and_uncovered(client):
    split = await panel(client, 1006)
    basis = split["header"]["calculation_basis"]
    assert basis["reason"] == "split" and basis["split"] is True and basis["area_m2"] == 1233.8
    assert [(li["urban_parcel_number"], li["rank"], li["primary"]) for li in basis["links"]] == [
        ("UP 31", 1, True),
        ("UP 32", 2, False),
    ]
    assert "UP 31, UP 32" in basis["explanation_en"]
    assert split["group1"]["urban_parcel_number"] == "UP 31"
    assert split["header"]["areas"]["linked_planned_total_m2"] == 2138.6

    fallback = await panel(client, 1005)  # UP 13: no parcel-level values, no FAR anywhere
    fields = by_key(fallback["group1"]["fields"])
    assert fields["min_green_area_pct"]["value"] == 15
    assert fields["min_green_area_pct"]["scope"] == "document"
    assert fields["max_far"]["reason"] == "not_in_document"
    group2 = by_key(fallback["group2"]["fields"])
    assert fallback["group2"]["status"] == "partial"
    assert group2["land_value_eur"]["status"] == "ok"
    assert group2["revenue_eur"]["reason"] == "requires_gfa"
    flags = by_key(fallback["group2"]["input_flags"])
    assert flags["max_far"]["used_by_formulas"] and "roi_pct" in flags["max_far"]["affects"]
    assert by_key(fallback["header"]["flags"])["restitution_or_legal_burden"]["value"] is True

    cadastral = await panel(client, 1007)  # covered by the PUP, no planned parcel over it
    basis = cadastral["header"]["calculation_basis"]
    assert (basis["basis"], basis["reason"], basis["links"]) == (
        "cadastral",
        "no_planned_parcel",
        [],
    )
    assert basis["area_m2"] == cadastral["header"]["areas"]["cadastral_m2"]
    assert by_key(cadastral["group1"]["fields"])["max_far"]["value"] == 1.5
    assert cadastral["group1"]["document"]["type"] == "PUP"
    assert cadastral["group2"]["status"] == "ok"

    uncovered = await panel(client, 1004)
    assert uncovered["covered"] is False and uncovered["coverage_note_en"]
    assert (uncovered["group1"], uncovered["group2"], uncovered["market"]) == (None, None, None)
    assert uncovered["header"]["calculation_basis"]["reason"] == "not_covered"
    assert by_key(uncovered["header"]["flags"])["public_ownership"]["value"] is True


async def test_unknown_and_invalid_ids(client):
    missing = await client.get(parcel_url(999_999))
    assert missing.status_code == 404
    assert missing.json()["error"]["details"] == {"type": "parcel", "id": 999_999}
    assert (await client.get(parcel_url(0))).status_code == 422
    assert (await client.get("/v1/zones/999999/panel")).status_code == 404
    assert (await client.get("/v1/zones/abc/panel")).status_code == 422


# --- Group 2 equals both engines ------------------------------------------------------------------


def typescript_calculate(inputs: dict[str, Any]) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None or not ENGINE_BUNDLE.is_file():
        pytest.skip(
            "the TypeScript engine bundle is not built "
            "(npm run build -w @urbanview/feasibility-engine) or node is missing"
        )
    completed = subprocess.run(
        [node, "-e", NODE_CALCULATE, str(ENGINE_BUNDLE)],
        input=json.dumps(inputs),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize("parcel_id", [1001, 1002, 1005, 1006, 1007])
async def test_group2_equals_the_panel_route_and_both_engines(client, parcel_id):
    body = await panel(client, parcel_id)
    inputs = body["engine"]["inputs"]
    python = shared.calculate(inputs)
    typescript = typescript_calculate(inputs)
    # the two engines agree on the whole result (the shared fixtures' contract)
    assert json.loads(shared.to_json(python))["fields"] == typescript["fields"]
    legacy = (await client.get("/v1/panel", params={"type": "cadastral", "id": parcel_id})).json()
    legacy_figures = by_key([*legacy["feasibility"]["fields"], *legacy["feasibility"]["cost_rows"]])
    for field in body["group2"]["fields"]:
        engine = typescript["fields"][field["engine_key"]]
        assert (field["status"], field["low"], field["expected"], field["high"]) == (
            engine["status"],
            engine["low"],
            engine["expected"],
            engine["high"],
        ), field["key"]
        old = legacy_figures[field["key"]]
        assert (field["low"], field["expected"], field["high"]) == (
            old["low"],
            old["expected"],
            old["high"],
        ), field["key"]


# --- zone panel ----------------------------------------------------------------------------------


async def test_zone_panel(client, pg_conn):
    r = await client.get("/v1/zones/1/panel")
    assert r.status_code == 200, r.text
    body = r.json()
    summary = (
        await pg_conn.execute(text("SELECT general_planning_summary FROM zones WHERE id = 1"))
    ).scalar_one()
    assert (body["title"], body["zone"], body["summary"]) == (
        "Centar",
        {"id": 1, "name": "Centar"},
        summary,
    )
    assert body["summary_label_en"] and body["summary_label_me"] and body["subtitle_me"]
    assert [(d["id"], d["status"], d["covered"]) for d in body["documents"]] == [
        (2, "adopted", True),  # adopted first, then by name: "DUP Centar…" < "PUP Glavni…"
        (1, "adopted", True),
        (3, "in_progress", False),
    ]
    in_progress = next(d for d in body["documents"] if d["id"] == 3)
    assert in_progress["status_label_en"] == "in progress" and in_progress["covered"] is False
    assert in_progress["type_name"].startswith("Detaljni")
    assert body["counts"]["adopted"] == 2 and body["counts"]["in_progress"] == 1
    assert body["typical_parameters"]["max_far"] is not None
    assert r.headers["X-Panel-Cache"] in {"miss", "hit"} and r.headers["ETag"]


# --- cache ---------------------------------------------------------------------------------------


async def test_cache_hit_revalidation_and_new_keys_after_changes(client, pg_conn):
    first = await client.get(parcel_url(1001))
    second = await client.get(parcel_url(1001))
    etag = first.headers["ETag"]
    assert first.headers["X-Panel-Cache"] in {"miss", "hit"}
    assert second.headers["X-Panel-Cache"] == "hit" and second.headers["ETag"] == etag
    assert second.content == first.content
    not_modified = await client.get(parcel_url(1001), headers={"If-None-Match": etag})
    assert not_modified.status_code == 304 and not_modified.content == b""
    assert not_modified.headers["X-Panel-Cache"] == "revalidated"
    assert first.headers["Cache-Control"] == "no-cache"

    # an assumptions change (admin config writes a new current version) -> new key, new figures
    await execute(pg_conn, "UPDATE financial_assumptions SET is_current = false WHERE id = 1")
    await execute(
        pg_conn,
        "INSERT INTO financial_assumptions (municipality_id, zone_id, version, supersedes_id, "
        "is_current, land_rate_eur_m2, build_rate_eur_m2, design_rate_eur_m2, sale_rate_eur_m2, "
        "range_low_factor, range_high_factor, source, created_by) VALUES ('podgorica', 1, 2, 1, "
        "true, 1350, 860, 90, 2600, 0.86, 1.15, 'test', 'test')",
    )
    try:
        changed = await client.get(parcel_url(1001), headers={"If-None-Match": etag})
        assert changed.status_code == 200 and changed.headers["X-Panel-Cache"] == "miss"
        assert changed.headers["ETag"] != etag
        assert changed.json()["market"]["sale_price_eur_m2"]["expected"] == 2600
    finally:
        await execute(pg_conn, "DELETE FROM financial_assumptions WHERE created_by = 'test'")
        await execute(pg_conn, "UPDATE financial_assumptions SET is_current = true WHERE id = 1")

    # back to the original state: the key is a function of the state, so the first entry serves
    restored = await client.get(parcel_url(1001))
    assert restored.headers["X-Panel-Cache"] == "hit" and restored.headers["ETag"] == etag
    assert restored.json()["market"]["sale_price_eur_m2"]["expected"] == 2450

    # the coverage switch: the DUP goes offline, the PUP governs, its planned parcel stops counting
    await execute(pg_conn, "UPDATE planning_documents SET coverage_live = false WHERE id = 2")
    try:
        offline = (await client.get(parcel_url(1001))).json()
        basis = offline["header"]["calculation_basis"]
        assert (basis["basis"], basis["reason"]) == ("cadastral", "no_planned_parcel")
        assert offline["header"]["documents"][0]["id"] == 1
    finally:
        await execute(pg_conn, "UPDATE planning_documents SET coverage_live = true WHERE id = 2")

    # a version flip (here: nothing current) -> new key, nothing published
    await execute(pg_conn, "UPDATE publish_versions SET is_current = false WHERE id = 1")
    try:
        unpublished = (await client.get(parcel_url(1001))).json()
        assert unpublished["data_version"] == "unpublished" and unpublished["version_id"] is None
        assert {f["reason"] for f in unpublished["group1"]["fields"]} == {"unpublished"}
    finally:
        await execute(pg_conn, "UPDATE publish_versions SET is_current = true WHERE id = 1")
    assert (await panel(client, 1001))["header"]["calculation_basis"]["reason"] == "planned_parcel"


async def test_one_statement_per_hit_and_two_per_miss(app):
    async with app.router.lifespan_context(app):
        statements: list[str] = []

        @event.listens_for(app.state.engine.sync_engine, "before_cursor_execute")
        def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            statements.append(statement)

        async with make_client(app) as client:
            await app.state.redis.flushall()
            miss = await client.get(parcel_url(1006))
            after_miss = len(statements)
            hit = await client.get(parcel_url(1006))
            after_hit = len(statements)
            revalidated = await client.get(
                parcel_url(1006), headers={"If-None-Match": hit.headers["ETag"]}
            )
    assert (miss.headers["X-Panel-Cache"], hit.headers["X-Panel-Cache"]) == ("miss", "hit")
    assert revalidated.status_code == 304
    assert after_miss == 2  # the stamp + the panel statement
    assert after_hit - after_miss == 1  # the stamp only
    assert len(statements) - after_hit == 1


# --- plans and latency ---------------------------------------------------------------------------


async def _bulk_parcel_ids(conn, n: int) -> list[int]:
    rows = await conn.execute(
        text(
            "SELECT c.id FROM cadastral_parcels c WHERE c.dataset_version <> "
            "'podgorica-sample-2026-09' ORDER BY c.id LIMIT :n"
        ),
        {"n": n},
    )
    return [int(r[0]) for r in rows]


async def test_plans_are_index_backed_and_fast(pg_conn):
    (bulk_id,) = await _bulk_parcel_ids(pg_conn, 1)
    for parcel_id in (1001, 1006, bulk_id):
        root = await _explain(
            pg_conn, PARCEL_PANEL_SQL, {"municipality_id": "podgorica", "id": parcel_id}
        )
        _assert_indexed_and_fast(
            root,
            {
                "cadastral_parcels_pkey",
                "idx_planning_documents_coverage_geom",
                "uq_parcel_links_version_pair",
            },
        )
        seq = {n.get("Relation Name") for n in _nodes(root["Plan"]) if n["Node Type"] == "Seq Scan"}
        assert "parcel_links" not in seq  # 10k rows: the version + parcel index, never a scan
    zone = await _explain(pg_conn, ZONE_PANEL_SQL, {"municipality_id": "podgorica", "id": 1})
    _assert_indexed_and_fast(zone, {"ix_planning_documents_zone_id"})


async def test_value_and_gap_lookups_have_index_paths(pg_conn):
    """The sample's 40 values are scanned (cheaper); with scans disabled every scope of values
    and gaps must still be reachable through its partial unique index of the version."""
    await pg_conn.execute(text("SET LOCAL enable_seqscan = off"))
    try:
        raw = (
            await pg_conn.execute(
                text("EXPLAIN (FORMAT JSON) " + PARCEL_PANEL_SQL),
                {"municipality_id": "podgorica", "id": 1001},
            )
        ).scalar_one()
    finally:
        await pg_conn.rollback()
    plan = json.loads(raw) if isinstance(raw, str | bytes) else raw
    nodes = list(_nodes(plan[0]["Plan"]))
    indexes = {n["Index Name"] for n in nodes if n.get("Index Name")}
    for table in ("planning_parameter_values", "planning_value_gaps"):
        for scope in ("parcel", "block", "zone", "document"):
            name = f"uq_{table}_{scope}"
            assert name in indexes, f"{name} not used: {sorted(indexes)}"
        assert not any(
            n["Node Type"] == "Seq Scan" and n.get("Relation Name") == table for n in nodes
        ), table


async def test_p95_under_200_ms_cold_and_warm(app, client, pg_conn):
    ids = [1001, 1002, 1003, 1005, 1006, 1007, *await _bulk_parcel_ids(pg_conn, 40)]
    await client.get(parcel_url(1004))  # the first request opens the connection

    async def timings() -> list[float]:
        out = []
        for parcel_id in ids:
            started = perf_counter()
            r = await client.get(parcel_url(parcel_id))
            out.append(perf_counter() - started)
            assert r.status_code == 200, r.text
        return out

    await app.state.redis.flushall()
    cold = await timings()
    warm = await timings()
    p95_cold = statistics.quantiles(cold, n=20)[18]
    p95_warm = statistics.quantiles(warm, n=20)[18]
    assert p95_cold < 0.2, f"cold p95 {p95_cold:.3f}s: {sorted(cold)[-5:]}"
    assert p95_warm < 0.2, f"warm p95 {p95_warm:.3f}s"
    assert statistics.median(warm) <= statistics.median(cold)


# --- rejected after a publish ---------------------------------------------------------------------


async def test_a_rejected_value_is_reported_after_a_publish(postgis_url, monkeypatch):
    from jobs.celery_app import celery_app
    from jobs.enqueue import CeleryDispatcher

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    configure_job_store(SqlJobStore(database_url=postgis_url))
    settings = make_settings(
        location_resolver="postgis",
        database_url=postgis_url,
        rate_limit_requests=100_000,
        admin_api_tokens=f"{TOKEN}:admin:ops",
    )
    storage = PublishStorage()
    configure_publish(
        database_url=postgis_url, storage=storage, tile_builder=FakeTileBuilder(), settings=settings
    )
    app = make_app(settings, storage=storage, admin_dispatcher=CeleryDispatcher())
    await reset_publish_state(postgis_url)
    try:
        async with app.router.lifespan_context(app), make_client(app) as client:
            before = await panel(client, 1002)
            await reject_seeded_pending_item(app)
            await insert_item(
                app,
                document_id=4,
                urban_parcel_id=3,
                field_key="max_height_m",
                value_number=18,
                source_page=5,
                review_state="rejected",
            )
            published = await client.post(
                "/v1/admin/publish",
                json={"label": "gap-test"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert published.status_code == 202, published.text
            job = published.json()
            assert job["status"] == "succeeded", job
            after = await client.get(parcel_url(1002))
    finally:
        configure_job_store(None)
        configure_publish(database_url=None, storage=None, tile_builder=None, settings=None)
        await reset_publish_state(postgis_url)

    assert by_key(before["group1"]["fields"])["max_height_m"]["reason"] == "not_in_document"
    assert job["result"]["counts"]["values_rejected"] == 1
    body = after.json()
    assert after.headers["X-Panel-Cache"] == "miss" and body["data_version"] == "gap-test"
    height = by_key(body["group1"]["fields"])["max_height_m"]
    assert (height["value"], height["reason"]) == (None, "rejected")
    assert "rejected" in height["reason_en"]
    assert body["group2"]["status"] == "ok"
    assert by_key(body["group2"]["input_flags"])["max_height_m"]["reason"] == "rejected"
    # the new version carries the published links and values over: the rest is unchanged
    assert body["header"]["calculation_basis"]["links"][0]["urban_parcel_number"] == "UP 7"
    assert by_key(body["group1"]["fields"])["max_far"]["value"] == 2.4
