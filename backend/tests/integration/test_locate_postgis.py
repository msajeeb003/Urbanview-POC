"""Location resolution against PostGIS with the Podgorica sample (database/seeds/podgorica_sample).

Sample layout (WGS84):
- zone "Centar" with PUP "Glavni grad" (adopted, large), DUP "Centar – Zona C2" (adopted, inside
  the PUP) and an amendment in progress east of both; zone "Stari Aerodrom" with an adopted DUP
  and a superseded one;
- cadastral #1042 in KO "Podgorica I" whose planned urban parcel UP 12 is ~30% smaller (road
  strip), and cadastral #1042 in KO "Podgorica II" whose urban parcel UP 7 is identical;
- #2001/1 (sub-number, covered, no urban parcel), #3005 (exists but uncovered), #1043 (neighbour
  with a sliver overlap into #1042's urban parcel UP 13);
- plus synthetic volume east of the sample (300 adopted documents, 10k cadastral and 10k planned
  parcels) so plans and latencies are measured at a realistic size.
"""

from __future__ import annotations

import json
import statistics
from time import perf_counter

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from api.services.locate_sql import LOCATE_PARCEL_SQL, LOCATE_POINT_SQL

pytestmark = pytest.mark.integration

LOCATE = "/v1/locate"
PARCEL = "/v1/locate/parcel"

# Query points (lat, lng)
INSIDE_1042_AND_UP12 = {"lat": 42.44115, "lng": 19.2613}
INSIDE_1042_ROAD_STRIP = {"lat": 42.44115, "lng": 19.26105}  # west of UP 12 (19.26115)
INSIDE_UP21_NO_CADASTRE = {"lat": 42.44165, "lng": 19.2642}
PUP_ONLY = {"lat": 42.437, "lng": 19.252}
INSIDE_3005_UNCOVERED = {"lat": 42.4401, "lng": 19.2782}  # in-progress amendment area only
IN_PROGRESS_AREA_EMPTY = {"lat": 42.448, "lng": 19.280}
FAR_AWAY_IN_MUNICIPALITY = {"lat": 42.40, "lng": 19.20}
INSIDE_1042_KO_II = {"lat": 42.4321, "lng": 19.2922}

# Tables large enough (with the synthetic volume) that a sequential scan would be a real defect.
LARGE_TABLES = {"cadastral_parcels", "urban_parcels", "planning_documents"}


async def _get(client, path, params):
    r = await client.get(path, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# --- GET /v1/locate -----------------------------------------------------------------------------


async def test_point_inside_cadastral_and_urban_parcel(pg_client):
    body = await _get(pg_client, LOCATE, INSIDE_1042_AND_UP12)
    assert body["covered"] is True
    assert body["coverage"]["status"] == "covered" and body["coverage"]["reason"] is None

    # the most specific adopted plan governs: the DUP, not the PUP that also covers the point
    doc = body["planning_document"]
    assert doc["name"] == "DUP Centar – Zona C2" and doc["status"] == "adopted"
    assert doc["type"] == "DUP" and doc["source"] == "eRegistri"

    cad = body["cadastral_parcel"]
    assert cad["parcel_id"] == 1001
    assert (cad["parcel_number"], cad["sub_number"], cad["ko_name"]) == (
        "1042",
        None,
        "Podgorica I",
    )
    assert cad["street_address"] == "Bulevar Save Kovačevića 12"
    assert 1300 < cad["area_m2"] < 1450
    assert cad["geometry"]["type"] == "MultiPolygon"
    assert cad["public_ownership"] is False and cad["restitution_or_legal_burden"] is False

    up = body["urban_parcel"]
    assert up["urban_parcel_number"] == "UP 12" and up["match"] == "point"
    assert up["planning_document"]["name"] == "DUP Centar – Zona C2"
    assert up["block_ref"] == "C2-01"
    assert 60 < up["overlap_pct"] < 80
    # UP 13 touches #1042 with a sliver far below the 2% overlap threshold: not "corresponding"
    assert [u["urban_parcel_number"] for u in body["urban_parcels"]] == ["UP 12"]

    assert body["urban_block"]["block_ref"] == "C2-01"
    assert body["zone"]["name"] == "Centar"
    docs = {(d["name"], d["status"]) for d in body["zone"]["planning_documents"]}
    assert docs == {
        ("PUP Glavni grad (izvod)", "adopted"),
        ("DUP Centar – Zona C2", "adopted"),
        ("Izmjene i dopune DUP-a Centar 2023", "in_progress"),
    }
    assert body["zone"]["planning_documents"][0]["status"] == "adopted"

    # cadastral vs planned: the planned parcel is ~30% smaller and drives the calculation
    assert body["calculation_basis"] == "urban"
    cmp = body["area_comparison"]
    assert cmp["differs"] is True and -35 < cmp["delta_pct"] < -25
    assert cmp["cadastral_area_m2"] == cad["area_m2"]
    assert cmp["urban_parcel_area_m2"] == up["area_m2"]

    assert body["centroid"]["lat"] == pytest.approx(42.44115, abs=1e-4)
    assert body["centroid"]["lng"] == pytest.approx(19.26125, abs=1e-4)
    assert body["query"] == {
        "mode": "point",
        "municipality_id": "podgorica",
        "lat": INSIDE_1042_AND_UP12["lat"],
        "lng": INSIDE_1042_AND_UP12["lng"],
        "ko": None,
        "parcel_number": None,
        "sub_number": None,
    }


async def test_point_in_land_taken_by_the_plan_still_finds_the_corresponding_urban_parcel(
    pg_client,
):
    body = await _get(pg_client, LOCATE, INSIDE_1042_ROAD_STRIP)
    assert body["covered"] is True
    assert body["cadastral_parcel"]["parcel_number"] == "1042"
    up = body["urban_parcel"]
    assert up["urban_parcel_number"] == "UP 12"
    assert up["match"] == "overlap"  # the point itself lies in the road strip
    assert 60 < up["overlap_pct"] < 80
    assert [u["urban_parcel_number"] for u in body["urban_parcels"]] == ["UP 12"]
    assert body["area_comparison"]["differs"] is True


async def test_point_with_urban_parcel_but_no_cadastral_parcel(pg_client):
    body = await _get(pg_client, LOCATE, INSIDE_UP21_NO_CADASTRE)
    assert body["covered"] is True
    assert body["cadastral_parcel"] is None and body["centroid"] is None
    assert body["urban_parcel"]["urban_parcel_number"] == "UP 21"
    assert body["urban_parcel"]["match"] == "point"
    assert (
        body["urban_parcel"]["overlap_m2"] is None and body["urban_parcel"]["overlap_pct"] is None
    )
    assert body["urban_block"]["block_ref"] == "C2-01"  # by geometry: UP 21 has no block_id
    assert body["calculation_basis"] == "urban"
    assert body["area_comparison"] is None


async def test_point_covered_by_the_general_plan_only(pg_client):
    body = await _get(pg_client, LOCATE, PUP_ONLY)
    assert body["covered"] is True
    assert body["planning_document"]["name"] == "PUP Glavni grad (izvod)"
    assert body["planning_document"]["type"] == "PUP"
    assert body["zone"]["name"] == "Centar"
    assert body["cadastral_parcel"] is None
    assert body["urban_parcel"] is None and body["urban_parcels"] == []
    assert body["urban_block"] is None
    assert body["calculation_basis"] is None


async def test_point_covered_only_by_a_document_in_progress_is_uncovered(pg_client):
    body = await _get(pg_client, LOCATE, INSIDE_3005_UNCOVERED)
    assert body["covered"] is False
    assert body["coverage"] == {
        "status": "uncovered",
        "reason": "no_adopted_plan",
        "message": "No adopted planning document covers this location.",
    }
    # the point lies inside zone "Centar"'s geometry, but without an adopted document there is no
    # zone, document, block or urban parcel to show
    assert body["zone"] is None and body["planning_document"] is None
    assert body["urban_block"] is None and body["urban_parcel"] is None
    assert body["urban_parcels"] == []
    assert body["calculation_basis"] is None
    # the cadastral parcel is still a fact and is returned
    cad = body["cadastral_parcel"]
    assert cad["parcel_number"] == "3005" and cad["public_ownership"] is True
    assert body["centroid"] is not None
    assert body["query"]["lat"] == INSIDE_3005_UNCOVERED["lat"]


async def test_point_with_no_data_at_all_is_uncovered_with_base_coordinates(pg_client):
    for point in (IN_PROGRESS_AREA_EMPTY, FAR_AWAY_IN_MUNICIPALITY):
        body = await _get(pg_client, LOCATE, point)
        assert body["covered"] is False
        assert body["coverage"]["reason"] == "no_adopted_plan"
        assert body["cadastral_parcel"] is None and body["zone"] is None
        assert body["query"]["lat"] == point["lat"] and body["query"]["lng"] == point["lng"]


async def test_point_outside_the_municipality(pg_client):
    body = await _get(pg_client, LOCATE, {"lat": 0, "lng": 0})
    assert body["covered"] is False
    assert body["coverage"]["reason"] == "outside_municipality"


async def test_superseded_documents_never_govern(pg_client):
    body = await _get(pg_client, LOCATE, INSIDE_1042_KO_II)
    assert body["planning_document"]["name"] == "DUP Stari Aerodrom"
    assert body["zone"]["name"] == "Stari Aerodrom"
    assert {(d["name"], d["status"]) for d in body["zone"]["planning_documents"]} == {
        ("DUP Stari Aerodrom", "adopted"),
        ("DUP Stari Aerodrom (2009)", "superseded"),
    }
    assert body["cadastral_parcel"]["ko_name"] == "Podgorica II"
    assert body["urban_parcel"]["urban_parcel_number"] == "UP 7"
    cmp = body["area_comparison"]
    assert cmp["differs"] is False and abs(cmp["delta_pct"]) < 0.5


# --- GET /v1/locate/parcel ---------------------------------------------------------------------


async def test_same_parcel_number_in_two_cadastral_municipalities(pg_client):
    first = await _get(pg_client, PARCEL, {"ko": "Podgorica I", "number": "1042"})
    second = await _get(pg_client, PARCEL, {"ko": "Podgorica II", "number": "1042"})

    assert first["cadastral_parcel"]["parcel_id"] == 1001
    assert first["planning_document"]["name"] == "DUP Centar – Zona C2"
    assert first["urban_parcel"]["urban_parcel_number"] == "UP 12"
    assert 60 < first["urban_parcel"]["overlap_pct"] < 80
    assert first["area_comparison"]["differs"] is True

    assert second["cadastral_parcel"]["parcel_id"] == 1002
    assert second["planning_document"]["name"] == "DUP Stari Aerodrom"
    assert second["urban_parcel"]["urban_parcel_number"] == "UP 7"
    assert second["urban_parcel"]["overlap_pct"] == pytest.approx(100, abs=1)
    assert second["area_comparison"]["differs"] is False

    for body in (first, second):
        assert body["covered"] is True
        assert body["query"]["mode"] == "parcel"
        # the centroid is where the map pans; the query echoes it as the base coordinates
        assert body["centroid"] is not None
        assert body["query"]["lat"] == body["centroid"]["lat"]
        assert body["query"]["lng"] == body["centroid"]["lng"]
    assert first["centroid"]["lng"] == pytest.approx(19.26125, abs=1e-4)
    assert second["centroid"]["lng"] == pytest.approx(19.29225, abs=1e-4)


async def test_parcel_lookup_is_case_insensitive_on_ko(pg_client):
    body = await _get(pg_client, PARCEL, {"ko": "podgorica i", "number": "1042"})
    assert body["cadastral_parcel"]["parcel_id"] == 1001


async def test_parcel_lookup_with_sub_number(pg_client):
    body = await _get(pg_client, PARCEL, {"ko": "Podgorica I", "number": "2001", "sub": "1"})
    assert body["cadastral_parcel"]["parcel_id"] == 1003
    assert body["cadastral_parcel"]["sub_number"] == "1"
    assert body["covered"] is True
    # covered, but the plan defines no urban parcel here: the cadastral area is the fallback basis
    assert body["urban_parcel"] is None and body["urban_parcels"] == []
    assert body["calculation_basis"] == "cadastral"
    assert body["area_comparison"] is None
    assert body["urban_block"]["block_ref"] == "C2-01"

    combined = await _get(pg_client, PARCEL, {"ko": "Podgorica I", "number": "2001/1"})
    assert combined["cadastral_parcel"]["parcel_id"] == 1003
    assert combined["query"]["parcel_number"] == "2001" and combined["query"]["sub_number"] == "1"

    # a number alone must not resolve a parcel that only exists with a sub-number
    bare = await _get(pg_client, PARCEL, {"ko": "Podgorica I", "number": "2001"})
    assert bare["covered"] is False and bare["coverage"]["reason"] == "parcel_not_found"


async def test_parcel_lookup_for_an_uncovered_parcel_returns_it_with_centroid(pg_client):
    body = await _get(pg_client, PARCEL, {"ko": "Podgorica I", "number": "3005"})
    assert body["covered"] is False
    assert body["coverage"]["reason"] == "no_adopted_plan"
    assert body["cadastral_parcel"]["parcel_number"] == "3005"
    assert body["centroid"]["lat"] == pytest.approx(42.44015, abs=1e-4)
    assert body["zone"] is None and body["planning_document"] is None


async def test_parcel_lookup_unknown_reference_is_200_not_found(pg_client):
    body = await _get(pg_client, PARCEL, {"ko": "Podgorica I", "number": "9999"})
    assert body["covered"] is False
    assert body["coverage"]["reason"] == "parcel_not_found"
    assert "9999" in body["coverage"]["message"] and "Podgorica I" in body["coverage"]["message"]
    assert body["cadastral_parcel"] is None and body["centroid"] is None


async def test_parcel_lookup_requires_ko(pg_client):
    r = await pg_client.get(PARCEL, params={"number": "1042"})
    assert r.status_code == 422


# --- plans, indexes, round trips, latency -----------------------------------------------------


def _nodes(plan: dict):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


async def _explain(conn, sql: str, params: dict) -> dict:
    """EXPLAIN ANALYZE of the real statement on realistic table sizes (no planner knobs)."""
    await conn.execute(text(sql), params)  # warm caches: measure steady state, not the first hit
    raw = (await conn.execute(text("EXPLAIN (ANALYZE, FORMAT JSON) " + sql), params)).scalar_one()
    plan = json.loads(raw) if isinstance(raw, str | bytes) else raw
    return plan[0]


def _assert_indexed_and_fast(root: dict, expected_indexes: set[str]) -> None:
    nodes = list(_nodes(root["Plan"]))
    index_names = {n["Index Name"] for n in nodes if n.get("Index Name")}
    missing = expected_indexes - index_names
    assert not missing, f"indexes not used: {missing}; used: {sorted(index_names)}"
    seq_scans = {n.get("Relation Name") for n in nodes if n["Node Type"] == "Seq Scan"}
    assert not (seq_scans & LARGE_TABLES), f"sequential scans on {seq_scans & LARGE_TABLES}"
    # far inside the 2 s product budget for query -> populated panel
    assert root["Execution Time"] < 250, root["Execution Time"]
    assert root["Planning Time"] < 250, root["Planning Time"]


COMMON = {"municipality_id": "podgorica", "min_overlap_m2": 1.0, "min_overlap_fraction": 0.02}


async def test_locate_point_plan_uses_spatial_indexes(pg_conn):
    root = await _explain(pg_conn, LOCATE_POINT_SQL, {**COMMON, **INSIDE_1042_AND_UP12})
    _assert_indexed_and_fast(
        root,
        {
            "idx_cadastral_parcels_geom",
            "idx_planning_documents_coverage_geom",
            "idx_urban_parcels_geom",
        },
    )


async def test_locate_parcel_plan_uses_unique_ko_index_and_spatial_indexes(pg_conn):
    params = {**COMMON, "ko": "Podgorica I", "parcel_number": "1042", "sub_number": None}
    root = await _explain(pg_conn, LOCATE_PARCEL_SQL, params)
    _assert_indexed_and_fast(
        root,
        {
            "uq_cadastral_parcels_ko_number",
            "idx_planning_documents_coverage_geom",
            "idx_urban_parcels_geom",
        },
    )


async def test_resolver_issues_one_statement_per_call(pg_app):
    async with pg_app.router.lifespan_context(pg_app):
        statements: list[str] = []

        @event.listens_for(pg_app.state.engine.sync_engine, "before_cursor_execute")
        def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            statements.append(statement)

        resolver = pg_app.state.resolver
        await resolver.resolve_point(**INSIDE_1042_AND_UP12)
        assert len(statements) == 1
        statements.clear()
        await resolver.resolve_parcel(ko="Podgorica I", parcel_number="1042", sub_number=None)
        assert len(statements) == 1


async def test_end_to_end_latency_is_well_within_budget(pg_client):
    for path, params in (
        (LOCATE, INSIDE_1042_AND_UP12),
        (PARCEL, {"ko": "Podgorica I", "number": "1042"}),
    ):
        timings = []
        for _ in range(10):
            started = perf_counter()
            r = await pg_client.get(path, params=params)
            timings.append(perf_counter() - started)
            assert r.status_code == 200
        assert max(timings) < 1.0, f"{path}: {timings}"  # includes the first connection
        assert statistics.median(timings) < 0.25, f"{path}: {timings}"


async def test_parcel_identity_is_ko_plus_number(pg_conn):
    """The same number may exist in two KOs, but never twice within one KO."""
    async with pg_conn.begin():
        count = (
            await pg_conn.execute(
                text("SELECT count(*) FROM cadastral_parcels WHERE parcel_number = '1042'")
            )
        ).scalar_one()
        assert count == 2
    with pytest.raises(IntegrityError):
        async with pg_conn.begin():
            await pg_conn.execute(
                text(
                    "INSERT INTO cadastral_parcels (municipality_id, parcel_number, ko_name, geom, "
                    "area_m2) VALUES ('podgorica', '1042', 'podgorica i', "
                    "ST_Multi(ST_GeomFromText('POLYGON((19.3 42.4,19.31 42.4,19.31 42.41,"
                    "19.3 42.41,19.3 42.4))', 4326)), 1)"
                )
            )
