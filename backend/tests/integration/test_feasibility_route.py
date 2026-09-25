"""POST /v1/feasibility against PostGIS: same data path and same engine as the panel."""

from __future__ import annotations

import pytest
from sqlalchemy import event

from core.engine import shared
from tests.integration.test_panel_postgis import _assert_feasibility_equals, _fixture_case

pytestmark = pytest.mark.integration

URL = "/v1/feasibility"
EDITS = {"saleable_share": 0.8, "construction_cost_per_m2": 700, "selling_price_per_m2": 2600}


async def _post(client, body):
    r = await client.post(URL, json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def test_urban_parcel_with_default_assumptions_matches_the_shared_fixture(pg_client):
    body = await _post(pg_client, {"parcel_id": 1, "type": "urban"})
    assert body["covered"] is True
    assert body["calculation_basis"] == "urban"
    assert body["basis_area_m2"] == pytest.approx(959.6, abs=0.05)
    _assert_feasibility_equals(body["feasibility"], _fixture_case("full_up12_centar")["expected"])

    fields = {f["key"]: f for f in body["feasibility"]["fields"]}
    assert list(fields) == [
        "max_gfa_m2",
        "max_coverage_area_m2",
        "saleable_area_m2",
        "construction_cost_eur",
        "revenue_eur",
        "profit_eur",
        "roi_pct",
    ]
    assert fields["saleable_area_m2"]["expected"] == 2149.5  # explicit, not only internal
    assert [r["key"] for r in body["feasibility"]["cost_rows"]] == [
        "land_value_eur",
        "design_documentation_eur",
        "construction_cost_eur",
        "total_cost_eur",
    ]
    for field in (*body["feasibility"]["fields"], *body["feasibility"]["cost_rows"]):
        assert field["low"] <= field["expected"] <= field["high"]

    assumptions = body["assumptions"]
    assert assumptions["saleable_share"] == 0.7
    assert (assumptions["construction_cost_eur_m2"], assumptions["sale_price_eur_m2"]) == (
        860,
        2450,
    )
    assert assumptions["overrides"] == {
        "saleable_share": False,
        "construction_cost_eur_m2": False,
        "sale_price_eur_m2": False,
    }
    assert assumptions["sources"] == {
        "construction_cost_eur_m2": "market",
        "sale_price_eur_m2": "market",
    }

    inputs = body["planning_inputs"]
    assert inputs == {
        "calculation_basis": "urban",
        "plot_area_m2": body["basis_area_m2"],
        "far": 3.2,
        "site_coverage_pct": 55.0,
        "max_gfa_m2": 3070.72,
        "max_coverage_area_m2": 527.78,
    }
    assert body["engine"] == {
        "name": "@urbanview/feasibility-engine",
        "engine_version": shared.ENGINE_VERSION,
        "formula_version": "poc-1",
        "range_derivation": "pessimistic-pairing-v1",
        "deterministic": True,
    }
    assert body["data_version"] == "sample-2026-09-22"
    assert body["data_version_date"] == "2026-09-22"
    assert body["client_validated"] is False
    assert body["disclaimer"]["status"] == "placeholder" and body["disclaimer"]["en"]
    assert "not investment" in body["disclaimer"]["en"]


async def test_edited_assumptions_are_merged_over_the_defaults(pg_client):
    body = await _post(pg_client, {"parcel_id": 1, "type": "urban", "assumptions": EDITS})
    _assert_feasibility_equals(body["feasibility"], _fixture_case("user_overrides")["expected"])
    assumptions = body["assumptions"]
    assert assumptions["saleable_share"] == 0.8
    assert (assumptions["construction_cost_eur_m2"], assumptions["sale_price_eur_m2"]) == (
        700,
        2600,
    )
    assert assumptions["overrides"] == {
        "saleable_share": True,
        "construction_cost_eur_m2": True,
        "sale_price_eur_m2": True,
    }
    assert assumptions["sources"] == {
        "construction_cost_eur_m2": "user",
        "sale_price_eur_m2": "user",
    }
    # the market rates that were not edited are still the zone's
    assert (assumptions["land_rate_eur_m2"], assumptions["design_documentation_eur_m2"]) == (
        1350,
        90,
    )


async def test_matches_the_panel_field_for_field(pg_client):
    for parcel_type, parcel_id in (("urban", 1), ("cadastral", 1001), ("cadastral", 1007)):
        posted = await _post(
            pg_client, {"parcel_id": parcel_id, "type": parcel_type, "assumptions": EDITS}
        )
        r = await pg_client.get(
            "/v1/panel",
            params={
                "type": parcel_type,
                "id": parcel_id,
                "saleable_share": EDITS["saleable_share"],
                "construction_cost_eur_m2": EDITS["construction_cost_per_m2"],
                "sale_price_eur_m2": EDITS["selling_price_per_m2"],
            },
        )
        assert r.status_code == 200
        panel = r.json()
        assert posted["feasibility"] == panel["feasibility"]
        assert posted["assumptions"] == panel["assumptions"]
        assert posted["calculation_basis"] == panel["calculation_basis"]
        assert posted["basis_area_m2"] == panel["basis_area_m2"]


async def test_cadastral_parcel_uses_the_primary_urban_parcel_or_its_own_area(pg_client):
    via_cadastral = await _post(pg_client, {"parcel_id": 1001, "type": "cadastral"})
    via_urban = await _post(pg_client, {"parcel_id": 1, "type": "urban"})
    assert via_cadastral["calculation_basis"] == "urban"
    assert via_cadastral["feasibility"] == via_urban["feasibility"]

    fallback = await _post(pg_client, {"parcel_id": 1007, "type": "cadastral"})
    assert fallback["calculation_basis"] == "cadastral"
    assert fallback["basis_area_m2"] == pytest.approx(1371.0, abs=0.05)
    assert fallback["planning_inputs"]["far"] == 1.5
    fields = {f["key"]: f for f in fallback["feasibility"]["fields"]}
    assert fields["max_gfa_m2"]["expected"] == pytest.approx(1.5 * 1371.0, abs=0.05)
    assert fields["roi_pct"]["status"] == "ok"


async def test_missing_plan_values_are_reported_never_invented(pg_client):
    body = await _post(pg_client, {"parcel_id": 4, "type": "urban"})  # UP 21: FAR not stated
    fields = {f["key"]: f for f in body["feasibility"]["fields"]}
    assert fields["max_gfa_m2"]["status"] == "cannot_calculate"
    assert fields["max_gfa_m2"]["reason_code"] == "far_not_stated"
    assert fields["revenue_eur"]["reason_code"] == "requires_gfa"
    assert fields["max_coverage_area_m2"]["status"] == "ok"
    assert body["planning_inputs"]["far"] is None


async def test_uncovered_parcel_is_200_without_figures(pg_client):
    body = await _post(
        pg_client, {"parcel_id": 1004, "type": "cadastral"}
    )  # #3005, in-progress only
    assert body["covered"] is False
    assert body["feasibility"] is None and body["assumptions"] is None
    assert body["planning_inputs"] is None
    assert body["coverage_note_en"]
    assert body["disclaimer"]["en"] and body["data_version"] == "sample-2026-09-22"


async def test_unknown_parcel_is_404(pg_client):
    for parcel_type in ("urban", "cadastral"):
        r = await pg_client.post(URL, json={"parcel_id": 999_999, "type": parcel_type})
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    "body",
    [
        {"parcel_id": 1, "type": "zone"},
        {"parcel_id": 0, "type": "urban"},
        {"type": "urban"},
        {"parcel_id": 1, "type": "urban", "assumptions": {"saleable_share": 0}},
        {"parcel_id": 1, "type": "urban", "assumptions": {"saleable_share": 1.5}},
        {"parcel_id": 1, "type": "urban", "assumptions": {"construction_cost_per_m2": -1}},
        {"parcel_id": 1, "type": "urban", "assumptions": {"selling_price_per_m2": 0}},
        {"parcel_id": 1, "type": "urban", "assumptions": {"sale_price_eur_m2": 2600}},
        {"parcel_id": 1, "type": "urban", "unexpected": 1},
    ],
    ids=[
        "zone type",
        "parcel_id 0",
        "missing parcel_id",
        "share 0",
        "share above 1",
        "negative cost",
        "price 0",
        "unknown assumption key",
        "unknown top-level key",
    ],
)
async def test_invalid_requests_are_422(pg_client, body):
    r = await pg_client.post(URL, json=body)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_error"


async def test_deterministic_and_one_statement(pg_app):
    async with pg_app.router.lifespan_context(pg_app):
        from tests.helpers import make_client

        statements: list[str] = []

        @event.listens_for(pg_app.state.engine.sync_engine, "before_cursor_execute")
        def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            statements.append(statement)

        async with make_client(pg_app) as client:
            body = {"parcel_id": 1, "type": "urban", "assumptions": EDITS}
            first = await client.post(URL, json=body)
            second = await client.post(URL, json=body)
    assert first.status_code == second.status_code == 200
    assert first.content == second.content  # byte-identical
    assert len(statements) == 2  # one statement per call


# --- the public map's in-browser recalculation equals the server ------------------------------

NODE_RECALCULATE = (
    "const e=require(process.argv[1]);let s='';process.stdin.on('data',d=>s+=d)"
    ".on('end',()=>{const a=JSON.parse(s);process.stdout.write(JSON.stringify("
    "a.edits===null?e.calculate(a.inputs):e.recalculate(a.inputs,a.edits)))})"
)


def _typescript(inputs, edits):
    import json
    import shutil
    import subprocess

    from tests.integration.test_parcel_panel_postgis import ENGINE_BUNDLE

    node = shutil.which("node")
    if node is None or not ENGINE_BUNDLE.is_file():
        pytest.skip("the TypeScript engine bundle is not built or node is missing")
    completed = subprocess.run(
        [node, "-e", NODE_RECALCULATE, str(ENGINE_BUNDLE)],
        input=json.dumps({"inputs": inputs, "edits": edits}),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(completed.stdout)


def _assert_same_figures(feasibility, engine_result):
    from core.engine.feasibility import SHARED_KEY

    for figure in [*feasibility["fields"], *feasibility["cost_rows"]]:
        engine = engine_result["fields"][SHARED_KEY[figure["key"]]]
        assert (figure["status"], figure["low"], figure["expected"], figure["high"]) == (
            engine["status"],
            engine["low"],
            engine["expected"],
            engine["high"],
        ), figure["key"]


async def test_browser_recalculation_equals_the_feasibility_route(pg_client):
    panel = (await pg_client.get("/v1/panel", params={"type": "urban", "id": 1})).json()
    engine = panel["engine"]
    assert engine["formula_version"] == "poc-1" and engine["deterministic"] is True
    assert engine["edit_keys"] == {
        "construction_cost_eur_m2": "construction_cost_per_m2",
        "saleable_share": "saleable_share",
        "sale_price_eur_m2": "market_value_per_m2",
    }
    # no edits: the payload's inputs give the payload's figures (what "Reset" shows)
    _assert_same_figures(panel["feasibility"], _typescript(engine["inputs"], None))

    # the visitor's edits, through the edit keys, give what POST /v1/feasibility answers
    visitor = {"construction_cost_eur_m2": 700, "sale_price_eur_m2": 2600, "saleable_share": 0.8}
    edits = {engine["edit_keys"][key]: value for key, value in visitor.items()}
    server = await _post(pg_client, {"parcel_id": 1, "type": "urban", "assumptions": EDITS})
    _assert_same_figures(server["feasibility"], _typescript(engine["inputs"], edits))


async def test_cadastral_and_uncovered_panels_carry_the_engine_inputs(pg_client):
    cadastral = (await pg_client.get("/v1/panel", params={"type": "cadastral", "id": 1001})).json()
    assert cadastral["engine"]["inputs"]["planning"]["plot_area"] == pytest.approx(959.6)
    uncovered = (await pg_client.get("/v1/panel", params={"type": "cadastral", "id": 1004})).json()
    assert uncovered["engine"] is None
