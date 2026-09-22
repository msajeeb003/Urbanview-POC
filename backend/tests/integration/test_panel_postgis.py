"""Information panel (``GET /v1/panel``) against PostGIS with the Podgorica sample.

Contract: ``docs/specs/panel-payload.md`` section 6. The sample
(``database/seeds/podgorica_sample``) gives every case the panel must handle:

- zone "Centar" (id 1): PUP "Glavni grad" (1, adopted), DUP "Centar – Zona C2" (2, adopted, inside
  the PUP) and "Izmjene i dopune DUP-a Centar 2023" (3, in progress, ``amends_document_id = 2``);
  zone "Stari Aerodrom" (id 2): DUP "Stari Aerodrom" (4, adopted) and its 2009 predecessor (5,
  superseded);
- cadastral #1042 KO Podgorica I (1001) whose planned parcel UP 12 (urban 1) is ~30 % smaller and
  carries all 11 planning fields; #1042 KO Podgorica II (1002) = UP 7 (urban 3, no max height);
  #2001/1 (1003) covered by the DUP but without a planned parcel; #3005 (1004) uncovered;
  #1043 (1005) = UP 13; #1044 (1006) split between UP 31 (5) and UP 32 (6); #2002 (1007) under the
  general plan only, computable from the PUP's document-level provisions; UP 21 (urban 4) has no
  FAR and no cadastral parcel under it;
- one current publish version ``sample-2026-09-22``; market rows for both zones; two staging
  extractions for UP 12 (FAR 9.9 pending, height 99 rejected) that must never surface;
- synthetic volume east of the sample (300 documents, 10k cadastral and 10k planned parcels) so
  plans and latencies are measured at a realistic size.

The three mutation tests (market row withdrawn, document no longer adopted, nothing published)
change the serving tables through ``pg_conn``, read the panel through a fresh app and restore the
rows in ``finally``.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event, text

from api.services.panel_sql import CADASTRAL_SQL, DOCUMENT_SQL, PANEL_SQL, URBAN_SQL, ZONE_SQL
from core.config import Settings
from core.engine import COST_ROW_KEYS, FIELD_KEYS, Assumptions, MarketInputs, compute_feasibility
from core.engine.feasibility import SHARED_KEY
from tests.helpers import make_app, make_client

pytestmark = pytest.mark.integration

PANEL = "/v1/panel"
REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = REPO_ROOT / "packages" / "feasibility-engine" / "fixtures" / "feasibility-cases.json"
# panel test scenario -> (shared fixture case, market reason params the panel adds)
SHARED_CASES: dict[str, tuple[str, dict[str, Any]]] = {
    "full_up12_centar": ("up12_centar_rates", {}),
    "user_overrides": ("user_edits_over_multiplier_bounds", {}),
    "no_market_data_zone_centar": ("no_market_data", {"zone_name": "Centar"}),
}
MARKET_REASONS = {"no_market_data", "no_market_data_zone_unknown"}

DUP_C2 = "DUP Centar – Zona C2"
PUP = "PUP Glavni grad (izvod)"
AMENDMENT = "Izmjene i dopune DUP-a Centar 2023"
DUP_SA = "DUP Stari Aerodrom"
DUP_SA_2009 = "DUP Stari Aerodrom (2009)"
REGISTRY_URL = "https://lamp.gov.me/PlanningDocument?m=PG"
MARKET_SOURCE = "Realitica, Estitor, Monstat (sample)"

# The field dictionary (contract 2.2), in sort order: the 11 Group 1 fields, then the 2 computed.
PLANNING_KEYS = [
    "land_use",
    "max_site_coverage_pct",
    "max_far",
    "max_height_m",
    "max_floors",
    "building_line_m",
    "setback_neighbours_m",
    "parking_requirement",
    "min_green_area_pct",
    "planned_parcel_area_m2",
    "utilities",
    "max_gfa_m2",
    "max_coverage_area_m2",
]
STATED_KEYS = PLANNING_KEYS[:11]
COMPUTED_KEYS = PLANNING_KEYS[11:]
MONEY_KEYS = ["construction_cost_eur", "revenue_eur", "profit_eur", "roi_pct"]

# UP 12 (urban 1, document 2): value and source page of every stated field (seed rows).
UP12_VALUES: dict[str, tuple[Any, int]] = {
    "land_use": ("Residential – mixed use (ground-floor commercial)", 12),
    "max_site_coverage_pct": (55, 13),
    "max_far": (3.2, 13),
    "max_height_m": (27.5, 14),
    "max_floors": ("P+8", 14),
    "building_line_m": (5, 15),
    "setback_neighbours_m": (4, 15),
    "parking_requirement": ("1 space per apartment + 1 per 60 m² commercial", 16),
    "min_green_area_pct": (20, 17),
    "planned_parcel_area_m2": (959.6, 18),
    "utilities": ("water, sewage, electricity, district heating", 19),
}
CENTAR = MarketInputs(
    land_rate_eur_m2=1350, build_rate_eur_m2=860, design_rate_eur_m2=90, sale_rate_eur_m2=2450
)
STARI_AERODROM = MarketInputs(
    land_rate_eur_m2=900, build_rate_eur_m2=780, design_rate_eur_m2=90, sale_rate_eur_m2=1650
)

# Panels that carry a planning block on the unmodified sample.
PLANNING_PANELS = [
    ("cadastral", 1001),
    ("cadastral", 1002),
    ("cadastral", 1003),
    ("cadastral", 1005),
    ("cadastral", 1006),
    ("cadastral", 1007),
    ("urban", 1),
    ("urban", 2),
    ("urban", 3),
    ("urban", 4),
    ("urban", 5),
    ("urban", 6),
]

# Tables large enough (with the synthetic volume) that a sequential scan would be a real defect;
# the 1-40-row tables (zones, publish_versions, planning_fields, ...) are legitimately scanned.
LARGE_TABLES = {"cadastral_parcels", "urban_parcels", "planning_documents"}
COMMON = {"municipality_id": "podgorica", "min_overlap_m2": 1.0, "min_overlap_fraction": 0.02}


# --- helpers ------------------------------------------------------------------------------------


async def _get(client: AsyncClient, **params: Any) -> dict[str, Any]:
    r = await client.get(PANEL, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _by_key(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in items}


def _planning(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return _by_key(body["planning"]["fields"])


def _feasibility(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return _by_key(body["feasibility"]["fields"])


def _cost_rows(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return _by_key(body["feasibility"]["cost_rows"])


def _names(documents: list[dict[str, Any]]) -> list[str]:
    return [d["name"] for d in documents]


def _tolerance(key: str) -> float:
    return 0.005  # every figure is rounded to 2 decimals by the shared engine


def _assert_range_matches(got: dict[str, Any], expected: dict[str, Any]) -> None:
    """One FeasibilityField against an engine ``FieldRange.to_dict()`` (fixture or computed)."""
    for attr in ("key", "status", "reason_code", "reason_params", "range_kind"):
        assert got[attr] == expected[attr], (got["key"], attr, got[attr], expected[attr])
    for bound in ("low", "expected", "high"):
        if expected[bound] is None:
            assert got[bound] is None, (got["key"], bound)
        else:
            assert got[bound] == pytest.approx(expected[bound], abs=_tolerance(got["key"])), (
                got["key"],
                bound,
            )


def _assert_feasibility_equals(block: dict[str, Any], expected: dict[str, Any]) -> None:
    """The payload's feasibility block carries the engine's numbers (``to_dict()`` shape)."""
    assert block["formula_version"] == expected["formula_version"] == "poc-1"
    for section in ("fields", "cost_rows"):
        assert [f["key"] for f in block[section]] == [f["key"] for f in expected[section]]
        for got, exp in zip(block[section], expected[section], strict=True):
            _assert_range_matches(got, exp)


def _fixture_case(name: str) -> dict[str, Any]:
    """A shared-engine fixture case translated to the panel's feasibility shape.

    The shared fixtures (``packages/feasibility-engine``) key the figures by engine name; the
    panel serves them under its own keys and adds the zone name to the market reason. The
    translation here is a pure key mapping, independent of the adapter under test.
    """
    shared_name, market_params = SHARED_CASES[name]
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        cases = json.load(fh)["cases"]
    case = next(case for case in cases if case["name"] == shared_name)
    fields = case["expected"]["fields"]

    def convert(panel_key: str) -> dict[str, Any]:
        field = fields[SHARED_KEY[panel_key]]
        reason = field["reason"]
        params = None if reason is None else (market_params if reason in MARKET_REASONS else {})
        return {
            "key": panel_key,
            "status": field["status"],
            "reason_code": reason,
            "reason_params": params,
            "range_kind": field["range_kind"],
            "low": field["low"],
            "expected": field["expected"],
            "high": field["high"],
        }

    return {
        "expected": {
            "formula_version": case["expected"]["formula_version"],
            "fields": [convert(key) for key in FIELD_KEYS],
            "cost_rows": [convert(key) for key in COST_ROW_KEYS],
        }
    }


def _assert_ranges_ordered(block: dict[str, Any]) -> None:
    """Every ok figure satisfies low <= expected <= high, strictly for range kinds (factors
    0.86 / 1.15 are strictly on either side of 1); cannot_calculate figures carry no numbers."""
    for item in (*block["fields"], *block["cost_rows"]):
        if item["status"] != "ok":
            assert (item["low"], item["expected"], item["high"]) == (None, None, None), item
            assert item["reason_code"] and item["reason_en"] and item["reason_me"], item
            continue
        assert item["reason_code"] is None and item["reason_en"] is None, item
        assert item["low"] <= item["expected"] <= item["high"], item
        if item["range_kind"] == "deterministic":
            assert item["low"] == item["expected"] == item["high"], item
        elif item["expected"] != 0:
            assert item["low"] < item["expected"] < item["high"], item


def _assert_document_ref(ref: dict[str, Any], name: str, status: str) -> None:
    labels = {
        "adopted": ("adopted", "usvojen"),
        "in_progress": ("in progress", "u izradi"),
        "superseded": ("superseded", "zamijenjen"),
    }[status]
    assert ref["name"] == name and ref["status"] == status
    assert (ref["status_label_en"], ref["status_label_me"]) == labels
    assert ref["registry_url"] == REGISTRY_URL and ref["source"] == "eRegistri"
    assert ref["type"] in {"DUP", "PUP"}
    assert isinstance(ref["id"], int)


def _assert_common_fields(body: dict[str, Any], panel_type: str) -> None:
    assert body["type"] == panel_type
    assert body["municipality_id"] == "podgorica"
    assert body["data_version"] == "sample-2026-09-22"
    assert body["data_version_date"] == "2026-09-22"
    assert body["formula_version"] == "poc-1"
    assert body["client_validated"] is False


def _assert_stated(field: dict[str, Any], value: Any, page: int, document: str) -> None:
    assert field["status"] == "stated", field["key"]
    assert field["value"] == value, (field["key"], field["value"])
    source = field["source"]
    assert source is not None, field["key"]
    assert source["page"] == page and source["document_name"] == document, field["key"]
    assert source["registry_url"] == REGISTRY_URL
    assert source["bbox_space"] == "pdf-points-bottom-left" and source["viewer_url"] is None
    assert field["reason_code"] is None and field["formula"] is None


def _assert_not_stated(field: dict[str, Any]) -> None:
    assert field["status"] == "not_stated", field["key"]
    assert field["value"] is None and field["source"] is None and field["scope"] is None
    assert field["fallback"] is False and field["reason_code"] is None


@asynccontextmanager
async def _fresh_client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """A brand-new app (own engine, own lifespan): what a redeploy would see."""
    app = make_app(settings)
    async with app.router.lifespan_context(app):
        async with make_client(app) as client:
            yield client


async def _execute(conn, sql: str, **params: Any) -> Any:
    """One committed statement on the raw connection (admin change / restore)."""
    result = await conn.execute(text(sql), params)
    await conn.commit()
    return result


# --- zone -----------------------------------------------------------------------------------------


async def test_zone_panel_centar(pg_client):
    body = await _get(pg_client, type="zone", id=1)
    _assert_common_fields(body, "zone")
    assert body["zone"]["id"] == 1 and body["zone"]["name"] == "Centar"
    assert body["zone"]["general_planning_summary"].startswith("Central mixed-use quarter.")
    assert body["header"] == {
        "title": "Centar",
        "subtitle_en": "Internal city division",
        "subtitle_me": "Interna podjela grada",
    }
    docs = body["planning_documents"]
    # adopted first, name asc within status, then in progress
    assert [(d["name"], d["status"]) for d in docs] == [
        (DUP_C2, "adopted"),
        (PUP, "adopted"),
        (AMENDMENT, "in_progress"),
    ]
    for d in docs:
        _assert_document_ref(d, d["name"], d["status"])
    assert [d["id"] for d in docs] == [2, 1, 3]
    assert docs[2]["amends_document_id"] == 2 and docs[0]["amends_document_id"] is None
    assert body["counts"] == {"documents": 3, "adopted": 2, "in_progress": 1, "superseded": 0}


async def test_zone_panel_stari_aerodrom_lists_the_superseded_plan_last(pg_client):
    body = await _get(pg_client, type="zone", id=2)
    assert body["zone"]["name"] == "Stari Aerodrom" and body["header"]["title"] == "Stari Aerodrom"
    assert [(d["name"], d["status"]) for d in body["planning_documents"]] == [
        (DUP_SA, "adopted"),
        (DUP_SA_2009, "superseded"),
    ]
    _assert_document_ref(body["planning_documents"][1], DUP_SA_2009, "superseded")
    assert body["counts"] == {"documents": 2, "adopted": 1, "in_progress": 0, "superseded": 1}


# --- document -------------------------------------------------------------------------------------


async def test_document_panel_dup_with_amendment_in_progress(pg_client):
    body = await _get(pg_client, type="document", id=2)
    _assert_common_fields(body, "document")
    _assert_document_ref(body["document"], DUP_C2, "adopted")
    assert body["document"]["id"] == 2 and body["document"]["type"] == "DUP"
    assert body["document"]["ingestion_dataset_version"] == "podgorica-sample-2026-09"
    amendments = body["amendments_in_progress"]
    assert [a["id"] for a in amendments] == [3]
    _assert_document_ref(amendments[0], AMENDMENT, "in_progress")
    assert amendments[0]["amends_document_id"] == 2
    assert body["zones"] == [{"id": 1, "name": "Centar"}]
    # #1042, #1043, #2001/1 and #1044 have their point on surface inside the DUP; UP 12, 13, 21,
    # 31 and 32 belong to it (UP 7 belongs to the Stari Aerodrom DUP)
    assert body["coverage_counts"] == {"cadastral_parcels": 4, "urban_parcels": 5}
    assert body["general_planning_summary"].startswith("Central mixed-use quarter.")


async def test_document_panel_general_plan(pg_client):
    body = await _get(pg_client, type="document", id=1)
    _assert_document_ref(body["document"], PUP, "adopted")
    assert body["document"]["type"] == "PUP"
    assert body["amendments_in_progress"] == []  # linked by amends_document_id only
    assert body["zones"] == [{"id": 1, "name": "Centar"}]
    # the four DUP parcels plus #2002, which only the general plan covers; no planned parcels
    assert body["coverage_counts"] == {"cadastral_parcels": 5, "urban_parcels": 0}
    assert body["general_planning_summary"].startswith("Central mixed-use quarter.")


async def test_document_panel_of_the_amendment_itself(pg_client):
    body = await _get(pg_client, type="document", id=3)
    _assert_document_ref(body["document"], AMENDMENT, "in_progress")
    assert body["document"]["amends_document_id"] == 2
    assert body["amendments_in_progress"] == []
    assert body["zones"] == [{"id": 1, "name": "Centar"}]
    assert body["coverage_counts"] == {"cadastral_parcels": 1, "urban_parcels": 0}  # #3005


# --- cadastral ------------------------------------------------------------------------------------


async def test_cadastral_1042_is_calculated_on_its_planned_parcel(pg_client):
    body = await _get(pg_client, type="cadastral", id=1001)
    _assert_common_fields(body, "cadastral")

    ident = body["identification"]
    assert ident["parcel_id"] == 1001
    assert (ident["parcel_number"], ident["sub_number"], ident["ko_name"]) == (
        "1042",
        None,
        "Podgorica I",
    )
    assert ident["street_address"] == "Bulevar Save Kovačevića 12"
    assert ident["urban_block"] == {"id": 1, "block_ref": "C2-01"}
    assert ident["cadastral_area_m2"] == pytest.approx(1370.9, abs=0.05)
    _assert_document_ref(ident["governing_document"], DUP_C2, "adopted")
    assert ident["zone"] == {"id": 1, "name": "Centar"}

    header = body["header"]
    assert header["ko_and_number"] == "KO Podgorica I, 1042"
    assert header["zone"] == {"id": 1, "name": "Centar"}
    assert [(d["name"], d["role"]) for d in header["documents"]] == [
        (DUP_C2, "governing"),
        (AMENDMENT, "amendment"),
    ]
    assert header["data_version"] == "sample-2026-09-22"
    assert header["data_version_date"] == "2026-09-22"

    assert body["flags"] == {
        "public_ownership": False,
        "restitution_or_legal_burden": False,
        "note_en": "false means not flagged in the cadastral extract",
        "note_me": "false znači da nije označeno u katastarskom izvodu",
    }

    assert body["urban_parcel_defined"] is True and body["split"] is False
    link = body["urban_parcel"]
    assert link["id"] == 1 and link["urban_parcel_number"] == "UP 12"
    assert link["area_m2"] == pytest.approx(959.6, abs=0.05)
    assert link["overlap_m2"] == pytest.approx(959.6, abs=0.5)
    assert link["share_of_cadastral_pct"] == pytest.approx(70, abs=1)
    assert link["share_of_linked_pct"] == pytest.approx(100, abs=0.1)
    assert link["delta_pct"] == pytest.approx(-30, abs=1)
    _assert_document_ref(link["document"], DUP_C2, "adopted")
    assert link["urban_block"] == {"id": 1, "block_ref": "C2-01"}
    assert body["urban_parcels"] == [link]

    areas = body["areas"]
    assert areas["calculation_basis"] == body["calculation_basis"] == "urban"
    assert areas["basis_area_m2"] == body["basis_area_m2"] == link["area_m2"]
    assert areas["cadastral_area_m2"] == ident["cadastral_area_m2"]
    assert areas["urban_parcel_area_m2"] == link["area_m2"]
    assert areas["delta_pct"] == pytest.approx(-30, abs=1)
    assert areas["delta_m2"] == pytest.approx(959.6 - 1370.9, abs=0.1)
    assert areas["share_of_cadastral_pct"] == pytest.approx(70, abs=1)
    assert areas["share_of_urban_pct"] == pytest.approx(100, abs=0.1)
    assert areas["planned_area_stated_m2"] == 959.6
    assert areas["stated_vs_geometry_delta_pct"] == 0.0
    assert areas["basis_reason_code"] == "urban_covers_cadastral"
    assert areas["basis_reason_params"] == {
        "urban_parcel_number": "UP 12",
        "cadastral_parcel_number": "1042",
        "share_pct": link["share_of_cadastral_pct"],
    }
    assert areas["basis_reason_en"] == "planned parcel UP 12 covers 70% of cadastral parcel 1042"
    assert (
        areas["basis_reason_me"]
        == "urbanistička parcela UP 12 pokriva 70% katastarske parcele 1042"
    )

    # planning = UP 12's block: 11 stated fields, the same block the urban panel shows
    assert body["covered"] is True
    assert body["coverage_note_en"] is None and body["coverage_note_me"] is None
    planning = body["planning"]
    assert planning["tier"] == "free" and planning["calculation_basis"] == "urban"
    assert planning["basis_area_m2"] == link["area_m2"]
    fields = _planning(body)
    assert [f["status"] for f in planning["fields"]][:11] == ["stated"] * 11
    assert all(fields[k]["scope"] == "parcel" and not fields[k]["fallback"] for k in STATED_KEYS)

    urban = await _get(pg_client, type="urban", id=1)
    assert body["planning"] == urban["planning"]
    assert body["feasibility"] == urban["feasibility"]
    assert body["assumptions"] == urban["assumptions"]
    assert body["market_inputs"] == urban["market_inputs"]

    assert body["centroid"]["lat"] == pytest.approx(42.44115, abs=1e-4)
    assert body["centroid"]["lng"] == pytest.approx(19.26125, abs=1e-4)
    assert body["geometry"]["type"] == "MultiPolygon"


async def test_cadastral_2001_1_falls_back_to_the_cadastral_area(pg_client):
    body = await _get(pg_client, type="cadastral", id=1003)
    ident = body["identification"]
    assert (ident["parcel_number"], ident["sub_number"]) == ("2001", "1")
    assert ident["street_address"] is None
    assert ident["urban_block"] == {"id": 1, "block_ref": "C2-01"}  # block containing the point
    _assert_document_ref(ident["governing_document"], DUP_C2, "adopted")
    assert body["header"]["ko_and_number"] == "KO Podgorica I, 2001/1"
    assert _names(body["header"]["documents"]) == [DUP_C2, AMENDMENT]

    assert body["covered"] is True
    assert body["urban_parcel_defined"] is False
    assert body["urban_parcel"] is None and body["urban_parcels"] == []
    assert body["split"] is False

    areas = body["areas"]
    assert body["calculation_basis"] == areas["calculation_basis"] == "cadastral"
    assert body["basis_area_m2"] == areas["basis_area_m2"] == ident["cadastral_area_m2"]
    assert areas["basis_area_m2"] == pytest.approx(1370.9, abs=0.05)
    for key in (
        "urban_parcel_area_m2",
        "delta_m2",
        "delta_pct",
        "overlap_m2",
        "share_of_cadastral_pct",
        "share_of_urban_pct",
        "planned_area_stated_m2",
        "stated_vs_geometry_delta_pct",
    ):
        assert areas[key] is None, key
    assert areas["basis_reason_code"] == "no_urban_parcel"
    assert areas["basis_reason_params"] == {"cadastral_parcel_number": "2001/1"}
    assert areas["basis_reason_en"] == (
        "no planned parcel is defined over cadastral parcel 2001/1; cadastral area used"
    )
    assert areas["basis_reason_me"] == (
        "nad katastarskom parcelom 2001/1 nije definisana urbanistička parcela; koristi se "
        "katastarska površina"
    )

    # document-level provisions of the DUP (page 5), scope document without the fallback flag
    planning = body["planning"]
    assert planning["calculation_basis"] == "cadastral"
    assert planning["basis_area_m2"] == areas["basis_area_m2"]
    fields = _planning(body)
    _assert_stated(fields["min_green_area_pct"], 15, 5, DUP_C2)
    _assert_stated(fields["parking_requirement"], "1 space per apartment", 5, DUP_C2)
    _assert_stated(fields["utilities"], "water, sewage, electricity", 5, DUP_C2)
    for key in ("min_green_area_pct", "parking_requirement", "utilities"):
        assert fields[key]["scope"] == "document" and fields[key]["fallback"] is False
        assert fields[key]["source"]["note"] == "general provisions §3"
    for key in STATED_KEYS:
        if key not in ("min_green_area_pct", "parking_requirement", "utilities"):
            _assert_not_stated(fields[key])

    gfa, coverage = fields["max_gfa_m2"], fields["max_coverage_area_m2"]
    assert gfa["status"] == "cannot_compute" and gfa["value"] is None
    assert gfa["reason_code"] == "far_not_stated"
    assert gfa["reason_en"] == "FAR not stated in plan"
    assert gfa["reason_me"] == "indeks izgrađenosti nije naveden u planu"
    assert coverage["status"] == "cannot_compute" and coverage["value"] is None
    assert coverage["reason_code"] == "coverage_not_stated"
    assert coverage["reason_en"] == "site coverage not stated in plan"

    feasibility = _feasibility(body)
    assert feasibility["max_gfa_m2"]["status"] == "cannot_calculate"
    assert feasibility["max_gfa_m2"]["reason_code"] == "far_not_stated"
    assert feasibility["max_coverage_area_m2"]["reason_code"] == "coverage_not_stated"
    for key in ("saleable_area_m2", *MONEY_KEYS):
        assert feasibility[key]["status"] == "cannot_calculate", key
        assert feasibility[key]["reason_code"] == "requires_gfa", key
        assert feasibility[key]["reason_en"] == "requires max GFA"
        assert feasibility[key]["reason_me"] == "zahtijeva maksimalnu BGP"
    cost_rows = _cost_rows(body)
    land = cost_rows["land_value_eur"]  # needs only the area and the market row
    assert land["status"] == "ok"
    assert land["expected"] == pytest.approx(1370.9 * 1350, abs=0.5)
    for key in ("design_documentation_eur", "construction_cost_eur", "total_cost_eur"):
        assert cost_rows[key]["reason_code"] == "requires_gfa", key
    _assert_ranges_ordered(body["feasibility"])
    assert body["market_inputs"]["available"] is True
    assert body["market_inputs"]["zone"] == {"id": 1, "name": "Centar"}


async def test_cadastral_2002_under_the_general_plan_is_fully_computable(pg_client):
    body = await _get(pg_client, type="cadastral", id=1007)
    ident = body["identification"]
    assert ident["parcel_number"] == "2002" and ident["urban_block"] is None
    _assert_document_ref(ident["governing_document"], PUP, "adopted")
    assert ident["zone"] == {"id": 1, "name": "Centar"}
    assert body["header"]["ko_and_number"] == "KO Podgorica I, 2002"
    assert [(d["name"], d["role"]) for d in body["header"]["documents"]] == [(PUP, "governing")]

    assert body["covered"] is True
    assert body["urban_parcel"] is None and body["urban_parcels"] == []
    assert body["calculation_basis"] == "cadastral"
    assert body["basis_area_m2"] == pytest.approx(1371.0, abs=0.05)
    assert body["areas"]["basis_reason_code"] == "no_urban_parcel"
    assert body["areas"]["basis_reason_params"] == {"cadastral_parcel_number": "2002"}

    fields = _planning(body)
    _assert_stated(fields["land_use"], "Mixed use", 3, PUP)
    _assert_stated(fields["max_far"], 1.5, 3, PUP)
    _assert_stated(fields["max_site_coverage_pct"], 40, 3, PUP)
    _assert_stated(fields["max_floors"], "P+3", 3, PUP)
    _assert_stated(fields["min_green_area_pct"], 20, 3, PUP)
    for key in ("land_use", "max_far", "max_site_coverage_pct", "max_floors", "min_green_area_pct"):
        assert fields[key]["scope"] == "document" and fields[key]["fallback"] is False
        assert fields[key]["source"]["note"] == "zone-wide provisions"
        assert fields[key]["source"]["document_id"] == 1
    for key in (
        "max_height_m",
        "building_line_m",
        "setback_neighbours_m",
        "parking_requirement",
        "planned_parcel_area_m2",
        "utilities",
    ):
        _assert_not_stated(fields[key])
    basis = body["basis_area_m2"]
    assert fields["max_gfa_m2"]["status"] == "computed"
    assert fields["max_gfa_m2"]["value"] == pytest.approx(1.5 * basis, abs=0.05)
    assert fields["max_coverage_area_m2"]["status"] == "computed"
    assert fields["max_coverage_area_m2"]["value"] == pytest.approx(0.4 * basis, abs=0.05)

    # feasibility on the cadastral area with the Centar rates: every figure computes
    feasibility = body["feasibility"]
    assert feasibility["calculation_basis"] == "cadastral"
    assert feasibility["basis_area_m2"] == basis
    assert all(f["status"] == "ok" for f in (*feasibility["fields"], *feasibility["cost_rows"]))
    _assert_ranges_ordered(feasibility)
    engine = compute_feasibility(basis, 1.5, 40, CENTAR, Assumptions())
    _assert_feasibility_equals(feasibility, engine.to_dict())
    rows = _feasibility(body)
    assert rows["max_gfa_m2"]["expected"] == fields["max_gfa_m2"]["value"]
    assert rows["max_coverage_area_m2"]["expected"] == fields["max_coverage_area_m2"]["value"]
    assert rows["saleable_area_m2"]["expected"] == pytest.approx(1.5 * basis * 0.7, abs=0.1)
    assert rows["construction_cost_eur"]["expected"] == pytest.approx(1.5 * basis * 860, abs=0.5)
    assert body["market_inputs"]["zone"] == {"id": 1, "name": "Centar"}
    assert body["market_inputs"]["land_rate_eur_m2"] == 1350
    assert body["assumptions"]["construction_cost_eur_m2"] == 860


async def test_cadastral_1044_is_split_between_two_planned_parcels(pg_client):
    body = await _get(pg_client, type="cadastral", id=1006)
    assert body["identification"]["parcel_number"] == "1044"
    assert body["split"] is True and body["urban_parcel_defined"] is True
    links = body["urban_parcels"]
    assert [(link["id"], link["urban_parcel_number"]) for link in links] == [
        (5, "UP 31"),
        (6, "UP 32"),
    ]
    assert body["urban_parcel"] == links[0]  # largest overlap first
    cadastral_area = body["identification"]["cadastral_area_m2"]
    up31, up32 = links
    assert up31["share_of_cadastral_pct"] == pytest.approx(56, abs=1.5)
    assert up32["share_of_cadastral_pct"] == pytest.approx(41, abs=1.5)
    assert up31["share_of_linked_pct"] + up32["share_of_linked_pct"] == pytest.approx(100, abs=0.1)
    assert up31["share_of_linked_pct"] > up32["share_of_linked_pct"]
    for link in links:
        assert link["overlap_m2"] == pytest.approx(link["area_m2"], abs=0.5)  # fully inside #1044
        assert link["share_of_cadastral_pct"] == pytest.approx(
            link["overlap_m2"] / cadastral_area * 100, abs=0.1
        )
        assert link["delta_pct"] == pytest.approx(
            (link["area_m2"] - cadastral_area) / cadastral_area * 100, abs=0.1
        )
        _assert_document_ref(link["document"], DUP_C2, "adopted")
        assert link["urban_block"] == {"id": 1, "block_ref": "C2-01"}

    # calculated on the primary planned parcel UP 31, whose block is embedded
    assert body["calculation_basis"] == "urban"
    assert body["basis_area_m2"] == up31["area_m2"] == pytest.approx(1233.8, abs=0.05)
    areas = body["areas"]
    assert areas["urban_parcel_area_m2"] == up31["area_m2"]
    assert areas["basis_reason_params"]["urban_parcel_number"] == "UP 31"
    assert areas["basis_reason_params"]["share_pct"] == up31["share_of_cadastral_pct"]
    assert areas["basis_reason_en"].startswith("planned parcel UP 31 covers 56.")
    fields = _planning(body)
    _assert_stated(fields["max_far"], 2.0, 21, DUP_C2)
    _assert_stated(fields["max_site_coverage_pct"], 50, 21, DUP_C2)
    _assert_stated(fields["max_floors"], "P+4", 21, DUP_C2)
    assert fields["max_far"]["source"]["note"] == "table 3 – UP 31"
    assert fields["max_gfa_m2"]["value"] == pytest.approx(2.0 * up31["area_m2"], abs=0.05)
    # the DUP's document-level provisions fill the gaps of a planned parcel: fallback = true
    for key in ("min_green_area_pct", "parking_requirement", "utilities"):
        assert fields[key]["status"] == "stated" and fields[key]["scope"] == "document"
        assert fields[key]["fallback"] is True and fields[key]["source"]["page"] == 5
    assert body["feasibility"] == (await _get(pg_client, type="urban", id=5))["feasibility"]


async def test_uncovered_cadastral_parcel_is_200_without_planning_blocks(pg_client):
    body = await _get(pg_client, type="cadastral", id=1004)
    _assert_common_fields(body, "cadastral")
    assert body["identification"]["parcel_number"] == "3005"
    assert body["identification"]["governing_document"] is None
    assert body["flags"]["public_ownership"] is True
    assert body["header"]["ko_and_number"] == "KO Podgorica I, 3005"
    assert body["header"]["documents"] == []
    assert body["covered"] is False
    assert body["planning"] is None and body["market_inputs"] is None
    assert body["assumptions"] is None and body["feasibility"] is None
    assert body["urban_parcel"] is None and body["urban_parcels"] == []
    assert body["urban_parcel_defined"] is False
    assert body["calculation_basis"] == "cadastral"
    assert body["areas"]["basis_reason_code"] == "no_urban_parcel"
    assert body["coverage_note_en"] and body["coverage_note_me"]  # neutral, never an error
    assert body["centroid"] is not None and body["geometry"]["type"] == "MultiPolygon"


# --- urban ----------------------------------------------------------------------------------------


async def test_urban_up12_panel(pg_client):
    body = await _get(pg_client, type="urban", id=1)
    _assert_common_fields(body, "urban")

    ident = body["identification"]
    assert ident["urban_parcel_id"] == 1 and ident["urban_parcel_number"] == "UP 12"
    assert ident["urban_block"] == {"id": 1, "block_ref": "C2-01"}
    _assert_document_ref(ident["governing_document"], DUP_C2, "adopted")
    assert ident["zone"] == {"id": 1, "name": "Centar"}
    cad = ident["cadastral_parcel"]
    assert cad["parcel_id"] == 1001 and cad["parcel_number"] == "1042"
    assert cad["sub_number"] is None and cad["ko_name"] == "Podgorica I"
    assert cad["street_address"] == "Bulevar Save Kovačevića 12"
    assert cad["area_m2"] == pytest.approx(1370.9, abs=0.05)
    assert cad["share_of_urban_pct"] == pytest.approx(100, abs=0.1)
    assert cad["share_of_cadastral_pct"] == pytest.approx(70, abs=1)
    assert ident["linked_cadastral_parcels"] == [cad]

    header = body["header"]
    assert header["ko_and_number"] == "KO Podgorica I, 1042"
    assert header["zone"] == {"id": 1, "name": "Centar"}
    assert [(d["name"], d["status"], d["role"]) for d in header["documents"]] == [
        (DUP_C2, "adopted", "governing"),
        (AMENDMENT, "in_progress", "amendment"),
    ]
    assert (header["data_version"], header["data_version_date"]) == (
        "sample-2026-09-22",
        "2026-09-22",
    )

    areas = body["areas"]
    assert body["calculation_basis"] == areas["calculation_basis"] == "urban"
    assert body["basis_area_m2"] == areas["basis_area_m2"] == pytest.approx(959.6, abs=0.05)
    assert areas["basis_reason_code"] == "urban_covers_cadastral"
    assert areas["basis_reason_params"]["share_pct"] == pytest.approx(70, abs=1)
    assert areas["basis_reason_params"]["urban_parcel_number"] == "UP 12"
    assert areas["basis_reason_params"]["cadastral_parcel_number"] == "1042"
    assert areas["delta_pct"] == pytest.approx(-30, abs=1)
    assert areas["planned_area_stated_m2"] == 959.6
    assert areas["stated_vs_geometry_delta_pct"] == 0.0
    assert body["covered"] is True and body["coverage_note_en"] is None

    # planning: the 13 dictionary fields in order, 11 stated with a page-level source each
    planning = body["planning"]
    assert planning["tier"] == "free" and planning["calculation_basis"] == "urban"
    assert planning["basis_area_m2"] == body["basis_area_m2"]
    assert [f["key"] for f in planning["fields"]] == PLANNING_KEYS
    assert planning["not_stated_label"] == {
        "en": "not stated in plan",
        "me": "nije navedeno u planu",
    }
    fields = _planning(body)
    for key, (value, page) in UP12_VALUES.items():
        _assert_stated(fields[key], value, page, DUP_C2)
        assert fields[key]["scope"] == "parcel" and fields[key]["fallback"] is False
        source = fields[key]["source"]
        assert source["document_id"] == 2 and source["note"] == "table 3 – UP 12"
        assert isinstance(source["bbox"], list) and len(source["bbox"]) == 4
    assert fields["max_far"]["abbreviation"] == "II" and fields["max_far"]["unit"] is None
    assert fields["max_site_coverage_pct"]["abbreviation"] == "IZ"
    assert fields["max_site_coverage_pct"]["unit"] == "%"
    assert fields["max_site_coverage_pct"]["value_type"] == "number"
    assert fields["max_floors"]["value_type"] == "text"
    assert fields["max_height_m"]["unit"] == "m"
    assert (fields["max_far"]["label_en"], fields["max_far"]["label_me"]) == (
        "Max floor area ratio",
        "Maksimalni indeks izgrađenosti",
    )
    gfa, coverage = fields["max_gfa_m2"], fields["max_coverage_area_m2"]
    assert gfa["status"] == "computed" and gfa["value"] == pytest.approx(3070.7, abs=0.05)
    assert gfa["value"] == pytest.approx(3.2 * 959.6, abs=0.05)
    assert gfa["abbreviation"] == "BGP" and gfa["unit"] == "m²"
    assert gfa["formula"] == "max_far × basis_area_m2"
    assert gfa["derived_from"] == ["max_far", "basis_area_m2"]
    assert gfa["source"] is None and gfa["scope"] is None and gfa["reason_code"] is None
    assert coverage["status"] == "computed"
    assert coverage["value"] == pytest.approx(527.8, abs=0.05)
    assert coverage["formula"] == "max_site_coverage_pct / 100 × basis_area_m2"
    assert coverage["derived_from"] == ["max_site_coverage_pct", "basis_area_m2"]

    market = body["market_inputs"]
    assert market["tier"] == "paid" and market["available"] is True
    assert market["reason_code"] is None and market["reason_en"] is None
    assert market["zone"] == {"id": 1, "name": "Centar"}
    assert (
        market["land_rate_eur_m2"],
        market["build_rate_eur_m2"],
        market["design_rate_eur_m2"],
        market["sale_rate_eur_m2"],
    ) == (1350, 860, 90, 2450)
    assert (market["range_low_factor"], market["range_high_factor"]) == (0.86, 1.15)
    assert market["source"] == MARKET_SOURCE and market["source_date"] == "2026-08-01"
    assert datetime.fromisoformat(market["effective_from"]).tzinfo is not None

    assumptions = body["assumptions"]
    assert assumptions["tier"] == "paid"
    assert assumptions["saleable_share"] == 0.7
    assert assumptions["construction_cost_eur_m2"] == 860
    assert assumptions["sale_price_eur_m2"] == 2450
    assert assumptions["design_documentation_eur_m2"] == 90
    assert assumptions["land_rate_eur_m2"] == 1350
    assert (assumptions["range_low_factor"], assumptions["range_high_factor"]) == (0.86, 1.15)
    assert assumptions["overrides"] == {
        "saleable_share": False,
        "construction_cost_eur_m2": False,
        "sale_price_eur_m2": False,
    }
    assert assumptions["sources"] == {
        "construction_cost_eur_m2": "market",
        "sale_price_eur_m2": "market",
    }
    assert assumptions["market_source"] == MARKET_SOURCE
    assert assumptions["market_source_date"] == "2026-08-01"
    assert assumptions["formula_version"] == "poc-1"
    assert assumptions["data_version"] == "sample-2026-09-22"
    assert assumptions["data_version_date"] == "2026-09-22"
    assert assumptions["client_validated"] is False

    feasibility = body["feasibility"]
    assert feasibility["tier"] == "paid" and feasibility["calculation_basis"] == "urban"
    assert feasibility["basis_area_m2"] == body["basis_area_m2"]
    assert feasibility["formula_version"] == "poc-1"
    assert [f["key"] for f in feasibility["fields"]] == list(FIELD_KEYS)
    assert [f["key"] for f in feasibility["cost_rows"]] == list(COST_ROW_KEYS)
    assert all(f["status"] == "ok" for f in (*feasibility["fields"], *feasibility["cost_rows"]))
    _assert_ranges_ordered(feasibility)
    _assert_feasibility_equals(feasibility, _fixture_case("full_up12_centar")["expected"])
    rows = _feasibility(body)
    assert rows["max_gfa_m2"]["expected"] == gfa["value"]
    assert rows["max_coverage_area_m2"]["expected"] == coverage["value"]
    assert rows["roi_pct"]["unit"] == "%" and rows["revenue_eur"]["unit"] == "€"
    assert (rows["revenue_eur"]["label_en"], rows["revenue_eur"]["label_me"]) == (
        "Market value (revenue)",
        "Tržišna vrijednost",
    )
    assert rows["max_gfa_m2"]["range_kind"] == "deterministic"
    assert rows["profit_eur"]["range_kind"] == "range"
    assert feasibility["disclaimer_en"] == (
        "Figures are indicative ranges derived from the adopted plan and public market data, "
        "not investment, planning or legal advice."
    )
    assert feasibility["disclaimer_me"].startswith("Iznosi su indikativni rasponi")
    assert feasibility["disclaimer_status"] == "placeholder"
    assert feasibility["disclaimer_version"] == "poc-1"

    assert body["centroid"]["lat"] == pytest.approx(42.44115, abs=1e-4)
    assert body["geometry"]["type"] == "MultiPolygon"


async def test_user_overrides_recalculate_the_ranges(pg_client):
    default = await _get(pg_client, type="urban", id=1)
    body = await _get(
        pg_client,
        type="urban",
        id=1,
        saleable_share=0.8,
        construction_cost_eur_m2=700,
        sale_price_eur_m2=2600,
    )
    assumptions = body["assumptions"]
    assert assumptions["saleable_share"] == 0.8
    assert assumptions["construction_cost_eur_m2"] == 700
    assert assumptions["sale_price_eur_m2"] == 2600
    assert assumptions["overrides"] == {
        "saleable_share": True,
        "construction_cost_eur_m2": True,
        "sale_price_eur_m2": True,
    }
    assert assumptions["sources"] == {
        "construction_cost_eur_m2": "user",
        "sale_price_eur_m2": "user",
    }
    # land and design rates still come from the market row, which is shown unchanged
    assert assumptions["land_rate_eur_m2"] == 1350
    assert assumptions["design_documentation_eur_m2"] == 90
    assert body["market_inputs"] == default["market_inputs"]
    assert body["planning"] == default["planning"]  # the free block never depends on assumptions
    _assert_feasibility_equals(body["feasibility"], _fixture_case("user_overrides")["expected"])
    _assert_ranges_ordered(body["feasibility"])
    rows, default_rows = _feasibility(body), _feasibility(default)
    assert rows["saleable_area_m2"]["expected"] == pytest.approx(3070.72 * 0.8, abs=0.05)
    assert (
        rows["construction_cost_eur"]["expected"]
        < default_rows["construction_cost_eur"]["expected"]
    )
    assert rows["revenue_eur"]["expected"] > default_rows["revenue_eur"]["expected"]

    # a single override leaves the other rate on its market value
    partial = await _get(pg_client, type="urban", id=1, sale_price_eur_m2=2600)
    assert partial["assumptions"]["overrides"] == {
        "saleable_share": False,
        "construction_cost_eur_m2": False,
        "sale_price_eur_m2": True,
    }
    assert partial["assumptions"]["sources"] == {
        "construction_cost_eur_m2": "market",
        "sale_price_eur_m2": "user",
    }
    assert partial["assumptions"]["construction_cost_eur_m2"] == 860
    assert partial["assumptions"]["saleable_share"] == 0.7
    partial_rows = _feasibility(partial)
    assert partial_rows["construction_cost_eur"] == default_rows["construction_cost_eur"]
    assert partial_rows["revenue_eur"]["expected"] == pytest.approx(2149.504 * 2600, abs=0.5)

    # overrides apply to cadastral panels too (both basis modes)
    cad = await _get(pg_client, type="cadastral", id=1001, saleable_share=0.8)
    assert cad["assumptions"]["saleable_share"] == 0.8
    assert cad["assumptions"]["overrides"]["saleable_share"] is True
    assert _feasibility(cad)["saleable_area_m2"]["expected"] == pytest.approx(
        3070.72 * 0.8, abs=0.05
    )
    fallback = await _get(pg_client, type="cadastral", id=1007, sale_price_eur_m2=3000)
    assert fallback["calculation_basis"] == "cadastral"
    assert fallback["assumptions"]["sale_price_eur_m2"] == 3000
    assert fallback["assumptions"]["sources"]["sale_price_eur_m2"] == "user"
    assert _feasibility(fallback)["revenue_eur"]["expected"] == pytest.approx(
        1371.0 * 1.5 * 0.7 * 3000, abs=0.5
    )


async def test_staging_extractions_never_surface(pg_client, pg_conn):
    # the review queue holds a pending FAR of 9.9 and a rejected height of 99 for UP 12
    staged = (
        await pg_conn.execute(
            text(
                "SELECT field_key, value_number, review_state::text "
                "FROM planning_parameter_extractions WHERE urban_parcel_id = 1 ORDER BY id"
            )
        )
    ).all()
    assert [tuple(row) for row in staged] == [
        ("max_far", 9.9, "pending_review"),
        ("max_height_m", 99, "rejected"),
    ]
    assert all("planning_parameter_extractions" not in sql for sql in PANEL_SQL.values())

    for panel_type, entity_id in (("urban", 1), ("cadastral", 1001)):
        body = await _get(pg_client, type=panel_type, id=entity_id)
        fields = _planning(body)
        assert fields["max_far"]["value"] == 3.2 and fields["max_height_m"]["value"] == 27.5
        assert {f["value"] for f in body["planning"]["fields"]}.isdisjoint({9.9, 99})
        assert "staging" not in json.dumps(body, ensure_ascii=False)
        assert _feasibility(body)["max_gfa_m2"]["expected"] == pytest.approx(3070.7, abs=0.05)


@pytest.mark.parametrize(("panel_type", "entity_id"), PLANNING_PANELS)
async def test_every_stated_field_has_a_source_with_a_page(pg_client, panel_type, entity_id):
    body = await _get(pg_client, type=panel_type, id=entity_id)
    assert body["covered"] is True
    fields = body["planning"]["fields"]
    assert [f["key"] for f in fields] == PLANNING_KEYS
    for field in fields:
        if field["status"] == "stated":
            source = field["source"]
            assert source is not None, field["key"]
            assert isinstance(source["page"], int) and source["page"] >= 1, field["key"]
            assert isinstance(source["document_id"], int) and source["document_name"]
            assert source["registry_url"] == REGISTRY_URL
            assert source["bbox_space"] == "pdf-points-bottom-left"
            assert field["value"] is not None and field["scope"] in ("parcel", "document")
            # a document-level value stands in for a planned parcel's own value only when the
            # calculation runs on a planned parcel (cadastral basis: the document IS the source)
            fallback = field["scope"] == "document" and body["calculation_basis"] == "urban"
            assert field["fallback"] is fallback, field["key"]
        else:
            assert field["source"] is None, field["key"]  # only a stated value has a source
            assert field["status"] in ("not_stated", "computed", "cannot_compute")
            assert (field["value"] is not None) == (field["status"] == "computed"), field["key"]
        if field["key"] in COMPUTED_KEYS:
            assert field["status"] in ("computed", "cannot_compute")
            assert field["formula"] and field["source"] is None
            assert (field["status"] == "computed") == (field["value"] is not None)
            assert (field["status"] == "cannot_compute") == (field["reason_code"] is not None)
    # the computed planning rows and the first two feasibility rows carry the same numbers
    planning, rows = _planning(body), _feasibility(body)
    for key in COMPUTED_KEYS:
        assert rows[key]["expected"] == planning[key]["value"], key
        assert rows[key]["reason_code"] == planning[key]["reason_code"], key
    _assert_ranges_ordered(body["feasibility"])


async def test_urban_up7_without_a_stated_height(pg_client):
    body = await _get(pg_client, type="urban", id=3)
    assert body["identification"]["urban_parcel_number"] == "UP 7"
    assert body["identification"]["urban_block"] == {"id": 2, "block_ref": "SA-01"}
    assert body["identification"]["zone"] == {"id": 2, "name": "Stari Aerodrom"}
    _assert_document_ref(body["identification"]["governing_document"], DUP_SA, "adopted")
    assert body["header"]["ko_and_number"] == "KO Podgorica II, 1042"
    assert [(d["name"], d["role"]) for d in body["header"]["documents"]] == [(DUP_SA, "governing")]
    assert body["identification"]["cadastral_parcel"]["parcel_id"] == 1002
    assert body["areas"]["delta_pct"] == pytest.approx(0, abs=0.5)  # identical outlines
    assert body["areas"]["planned_area_stated_m2"] == 1370.9
    assert body["areas"]["stated_vs_geometry_delta_pct"] == pytest.approx(0, abs=0.1)

    fields = _planning(body)
    _assert_not_stated(fields["max_height_m"])
    for key in STATED_KEYS:
        if key != "max_height_m":
            assert fields[key]["status"] == "stated", key
            assert fields[key]["scope"] == "parcel" and fields[key]["fallback"] is False
            assert fields[key]["source"]["document_name"] == DUP_SA
            assert fields[key]["source"]["note"] == "table 2 – UP 7"
    _assert_stated(fields["max_far"], 2.4, 8, DUP_SA)
    _assert_stated(fields["max_site_coverage_pct"], 40, 8, DUP_SA)
    _assert_stated(fields["max_floors"], "P+5+Pk", 9, DUP_SA)
    basis = body["basis_area_m2"]
    assert basis == pytest.approx(1371.1, abs=0.05)
    assert fields["max_gfa_m2"]["value"] == pytest.approx(2.4 * basis, abs=0.05)
    assert fields["max_coverage_area_m2"]["value"] == pytest.approx(0.4 * basis, abs=0.05)

    feasibility = body["feasibility"]
    assert all(f["status"] == "ok" for f in (*feasibility["fields"], *feasibility["cost_rows"]))
    _assert_ranges_ordered(feasibility)
    engine = compute_feasibility(basis, 2.4, 40, STARI_AERODROM, Assumptions())
    _assert_feasibility_equals(feasibility, engine.to_dict())
    market = body["market_inputs"]
    assert market["zone"] == {"id": 2, "name": "Stari Aerodrom"}
    assert (market["land_rate_eur_m2"], market["build_rate_eur_m2"]) == (900, 780)
    assert (market["design_rate_eur_m2"], market["sale_rate_eur_m2"]) == (90, 1650)
    assert body["assumptions"]["construction_cost_eur_m2"] == 780
    assert body["assumptions"]["sale_price_eur_m2"] == 1650


async def test_urban_up21_without_far_or_cadastral_parcel(pg_client):
    body = await _get(pg_client, type="urban", id=4)
    ident = body["identification"]
    assert ident["urban_parcel_number"] == "UP 21"
    assert ident["cadastral_parcel"] is None and ident["linked_cadastral_parcels"] == []
    assert ident["zone"] == {"id": 1, "name": "Centar"}
    assert body["header"]["ko_and_number"] is None
    assert body["header"]["zone"] == {"id": 1, "name": "Centar"}
    assert _names(body["header"]["documents"]) == [DUP_C2, AMENDMENT]
    areas = body["areas"]
    assert areas["cadastral_area_m2"] is None and areas["overlap_m2"] is None
    assert areas["delta_pct"] is None and areas["share_of_cadastral_pct"] is None
    assert areas["calculation_basis"] == "urban"
    assert areas["basis_area_m2"] == body["basis_area_m2"] == pytest.approx(1096.7, abs=0.05)
    assert areas["basis_reason_code"] == "no_cadastral_parcel"
    assert areas["basis_reason_params"] == {"urban_parcel_number": "UP 21"}
    assert areas["basis_reason_en"] == "planned parcel UP 21 has no cadastral parcel under it"
    assert areas["basis_reason_me"]
    assert body["covered"] is True

    fields = _planning(body)
    _assert_not_stated(fields["max_far"])
    _assert_stated(fields["max_site_coverage_pct"], 45, 20, DUP_C2)
    _assert_stated(fields["land_use"], "Residential", 20, DUP_C2)
    _assert_stated(fields["max_floors"], "P+4", 20, DUP_C2)
    for key in ("min_green_area_pct", "parking_requirement", "utilities"):
        assert fields[key]["status"] == "stated", key
        assert fields[key]["scope"] == "document" and fields[key]["fallback"] is True, key
        assert fields[key]["source"]["page"] == 5
        assert fields[key]["source"]["note"] == "general provisions §3"
    assert fields["min_green_area_pct"]["value"] == 15
    for key in (
        "max_height_m",
        "building_line_m",
        "setback_neighbours_m",
        "planned_parcel_area_m2",
    ):
        _assert_not_stated(fields[key])
    gfa = fields["max_gfa_m2"]
    assert gfa["status"] == "cannot_compute" and gfa["value"] is None
    assert gfa["reason_code"] == "far_not_stated"
    assert gfa["reason_en"] == "FAR not stated in plan"
    assert gfa["reason_me"] == "indeks izgrađenosti nije naveden u planu"
    coverage = fields["max_coverage_area_m2"]
    assert coverage["status"] == "computed"
    assert coverage["value"] == pytest.approx(0.45 * body["basis_area_m2"], abs=0.05)

    rows = _feasibility(body)
    assert rows["max_gfa_m2"]["status"] == "cannot_calculate"
    assert rows["max_gfa_m2"]["reason_code"] == "far_not_stated"
    assert rows["max_gfa_m2"]["reason_params"] == {}
    assert rows["max_coverage_area_m2"]["status"] == "ok"
    assert rows["max_coverage_area_m2"]["expected"] == coverage["value"]
    for key in ("saleable_area_m2", *MONEY_KEYS):
        assert rows[key]["status"] == "cannot_calculate", key
        assert rows[key]["reason_code"] == "requires_gfa", key
        assert rows[key]["reason_en"] == "requires max GFA"
    cost_rows = _cost_rows(body)
    assert cost_rows["land_value_eur"]["status"] == "ok"
    assert cost_rows["land_value_eur"]["expected"] == pytest.approx(1096.7 * 1350, abs=0.5)
    for key in ("design_documentation_eur", "construction_cost_eur", "total_cost_eur"):
        assert cost_rows[key]["reason_code"] == "requires_gfa", key
    _assert_ranges_ordered(body["feasibility"])
    assert body["market_inputs"]["available"] is True  # the market is there; the plan is not


@pytest.mark.parametrize("panel_type", ["cadastral", "urban"])
async def test_tier_markers(pg_client, panel_type):
    body = await _get(pg_client, type=panel_type, id={"cadastral": 1001, "urban": 1}[panel_type])
    assert body["planning"]["tier"] == "free"
    assert body["market_inputs"]["tier"] == "paid"
    assert body["assumptions"]["tier"] == "paid"
    assert body["feasibility"]["tier"] == "paid"


# --- admin changes are visible immediately (no cache) ---------------------------------------------


async def test_market_data_removed_makes_money_fields_cannot_calculate(pg_conn, pg_settings):
    await _execute(pg_conn, "UPDATE financial_assumptions SET is_current = false WHERE zone_id = 1")
    try:
        async with _fresh_client(pg_settings) as client:
            body = await _get(client, type="urban", id=1)
            rows = _feasibility(body)
            for key in MONEY_KEYS:
                assert rows[key]["status"] == "cannot_calculate", key
                assert rows[key]["reason_code"] == "no_market_data", key
                assert rows[key]["reason_params"] == {"zone_name": "Centar"}, key
                assert rows[key]["reason_en"] == "no market data for zone Centar", key
                assert rows[key]["reason_me"] == "nema tržišnih podataka za zonu Centar", key
                assert (rows[key]["low"], rows[key]["expected"], rows[key]["high"]) == (
                    None,
                    None,
                    None,
                )
            for key in ("max_gfa_m2", "max_coverage_area_m2", "saleable_area_m2"):
                assert rows[key]["status"] == "ok", key
            assert rows["max_gfa_m2"]["expected"] == pytest.approx(3070.7, abs=0.05)
            assert rows["max_coverage_area_m2"]["expected"] == pytest.approx(527.8, abs=0.05)
            assert rows["saleable_area_m2"]["expected"] == pytest.approx(2149.5, abs=0.05)
            for row in body["feasibility"]["cost_rows"]:
                assert (
                    row["status"] == "cannot_calculate" and row["reason_code"] == "no_market_data"
                )
            _assert_ranges_ordered(body["feasibility"])
            expected = _fixture_case("no_market_data_zone_centar")["expected"]
            _assert_feasibility_equals(body["feasibility"], expected)

            market = body["market_inputs"]
            assert market["available"] is False
            assert market["reason_code"] == "no_market_data"
            assert market["reason_en"] == "no market data for zone Centar"
            assert market["reason_me"] == "nema tržišnih podataka za zonu Centar"
            assert market["zone"] == {"id": 1, "name": "Centar"}
            for key in (
                "land_rate_eur_m2",
                "build_rate_eur_m2",
                "design_rate_eur_m2",
                "sale_rate_eur_m2",
                "range_low_factor",
                "range_high_factor",
                "source",
                "source_date",
                "effective_from",
            ):
                assert market[key] is None, key
            assumptions = body["assumptions"]
            assert assumptions["saleable_share"] == 0.7
            for key in (
                "construction_cost_eur_m2",
                "sale_price_eur_m2",
                "design_documentation_eur_m2",
                "land_rate_eur_m2",
                "range_low_factor",
                "range_high_factor",
                "market_source",
                "market_source_date",
            ):
                assert assumptions[key] is None, key
            assert assumptions["sources"] == {
                "construction_cost_eur_m2": None,
                "sale_price_eur_m2": None,
            }
            # the free block is untouched
            assert body["covered"] is True
            assert [f["status"] for f in body["planning"]["fields"]][:11] == ["stated"] * 11
            assert _planning(body)["max_gfa_m2"]["value"] == pytest.approx(3070.7, abs=0.05)

            # user overrides never rescue a missing market row (land and design rates unknown)
            forced = await _get(client, type="urban", id=1, construction_cost_eur_m2=700)
            assert forced["assumptions"]["construction_cost_eur_m2"] == 700
            assert forced["assumptions"]["sources"]["construction_cost_eur_m2"] == "user"
            for key in MONEY_KEYS:
                assert _feasibility(forced)[key]["reason_code"] == "no_market_data", key

            # the cadastral panel on the same zone agrees; the other zone keeps its market row
            cad = await _get(client, type="cadastral", id=1001)
            assert cad["market_inputs"]["available"] is False
            assert _feasibility(cad)["revenue_eur"]["reason_code"] == "no_market_data"
            other = await _get(client, type="urban", id=3)
            assert other["market_inputs"]["available"] is True
            assert _feasibility(other)["revenue_eur"]["status"] == "ok"
    finally:
        await _execute(
            pg_conn, "UPDATE financial_assumptions SET is_current = true WHERE zone_id = 1"
        )

    async with _fresh_client(pg_settings) as client:
        restored = await _get(client, type="urban", id=1)
        assert restored["market_inputs"]["available"] is True
        assert _feasibility(restored)["revenue_eur"]["status"] == "ok"


async def test_municipality_wide_default_market_row_is_the_fallback(pg_conn, pg_settings):
    inserted = (
        await _execute(
            pg_conn,
            "INSERT INTO financial_assumptions (municipality_id, zone_id, land_rate_eur_m2, "
            "build_rate_eur_m2, design_rate_eur_m2, sale_rate_eur_m2, source, is_current, "
            "created_by) VALUES ('podgorica', NULL, 1000, 800, 80, 2000, 'test default', true, "
            "'test') RETURNING id",
        )
    ).scalar_one()
    try:
        await _execute(
            pg_conn, "UPDATE financial_assumptions SET is_current = false WHERE zone_id = 2"
        )
        async with _fresh_client(pg_settings) as client:
            fallback = await _get(client, type="urban", id=3)
            market = fallback["market_inputs"]
            assert market["available"] is True
            assert market["zone"] == {"id": 2, "name": "Stari Aerodrom"}
            assert (market["land_rate_eur_m2"], market["build_rate_eur_m2"]) == (1000, 800)
            assert (market["design_rate_eur_m2"], market["sale_rate_eur_m2"]) == (80, 2000)
            assert market["source"] == "test default"
            assert fallback["assumptions"]["construction_cost_eur_m2"] == 800
            engine = compute_feasibility(
                fallback["basis_area_m2"], 2.4, 40, MarketInputs(1000, 800, 80, 2000), Assumptions()
            )
            _assert_feasibility_equals(fallback["feasibility"], engine.to_dict())
            # a zone with its own current row keeps it
            own = await _get(client, type="urban", id=1)
            assert own["market_inputs"]["land_rate_eur_m2"] == 1350
    finally:
        await _execute(pg_conn, "DELETE FROM financial_assumptions WHERE id = :id", id=inserted)
        await _execute(
            pg_conn, "UPDATE financial_assumptions SET is_current = true WHERE zone_id = 2"
        )


async def test_document_no_longer_adopted_is_uncovered(pg_conn, pg_client, pg_settings):
    before = await _get(pg_client, type="urban", id=3)
    assert before["covered"] is True and before["planning"] is not None
    await _execute(pg_conn, "UPDATE planning_documents SET status = 'superseded' WHERE id = 4")
    try:
        async with _fresh_client(pg_settings) as client:
            body = await _get(client, type="urban", id=3)
            assert body["covered"] is False
            assert body["planning"] is None and body["market_inputs"] is None
            assert body["assumptions"] is None and body["feasibility"] is None
            doc = body["identification"]["governing_document"]
            _assert_document_ref(doc, DUP_SA, "superseded")
            assert body["coverage_note_en"] and DUP_SA in body["coverage_note_en"]
            assert "superseded" in body["coverage_note_en"]
            assert body["coverage_note_me"] and "zamijenjen" in body["coverage_note_me"]
            # identity, header and areas are still facts
            assert body["identification"]["urban_parcel_number"] == "UP 7"
            assert body["header"]["ko_and_number"] == "KO Podgorica II, 1042"
            assert body["areas"]["basis_area_m2"] == before["areas"]["basis_area_m2"]

            # the cadastral parcel under it has no governing document any more
            cad = await _get(client, type="cadastral", id=1002)
            assert cad["covered"] is False
            assert cad["identification"]["governing_document"] is None
            assert cad["header"]["documents"] == []
            assert cad["urban_parcel"] is None and cad["urban_parcels"] == []
            assert cad["calculation_basis"] == "cadastral"
            assert cad["planning"] is None and cad["feasibility"] is None

            zone = await _get(client, type="zone", id=2)
            assert zone["counts"] == {
                "documents": 2,
                "adopted": 0,
                "in_progress": 0,
                "superseded": 2,
            }
        # nothing is cached: the client created before the change sees it too
        after = await _get(pg_client, type="urban", id=3)
        assert after["covered"] is False and after["planning"] is None
    finally:
        await _execute(pg_conn, "UPDATE planning_documents SET status = 'adopted' WHERE id = 4")

    restored = await _get(pg_client, type="urban", id=3)
    assert restored["covered"] is True and restored["planning"] == before["planning"]


async def test_without_a_current_publish_version_the_panel_is_unpublished(pg_conn, pg_settings):
    await _execute(
        pg_conn,
        "UPDATE publish_versions SET is_current = false WHERE municipality_id = 'podgorica'",
    )
    try:
        async with _fresh_client(pg_settings) as client:
            zone = await _get(client, type="zone", id=1)
            assert zone["data_version"] == "unpublished" and zone["data_version_date"] is None
            assert zone["counts"]["documents"] == 3
            urban = await _get(client, type="urban", id=1)
            assert urban["data_version"] == "unpublished" and urban["data_version_date"] is None
            assert urban["header"]["data_version"] == "unpublished"
            assert urban["header"]["data_version_date"] is None
            assert urban["assumptions"]["data_version"] == "unpublished"
            assert urban["assumptions"]["data_version_date"] is None
            # whatever serving rows exist are still rendered
            assert [f["status"] for f in urban["planning"]["fields"]][:11] == ["stated"] * 11
            assert _feasibility(urban)["roi_pct"]["status"] == "ok"
    finally:
        await _execute(
            pg_conn,
            "UPDATE publish_versions SET is_current = true WHERE label = 'sample-2026-09-22'",
        )

    async with _fresh_client(pg_settings) as client:
        assert (await _get(client, type="zone", id=1))["data_version"] == "sample-2026-09-22"


# --- errors ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("panel_type", ["zone", "document", "cadastral", "urban"])
async def test_unknown_id_is_404_not_found(pg_client, panel_type):
    r = await pg_client.get(PANEL, params={"type": panel_type, "id": 999999})
    assert r.status_code == 404, r.text
    error = r.json()["error"]
    assert error["code"] == "not_found"
    assert error["details"] == {"type": panel_type, "id": 999999}
    assert "999999" in error["message"] and "podgorica" in error["message"]
    assert error["request_id"]


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"type": "foo", "id": 1}, "type"),
        ({"type": "zone"}, "id"),
        ({"id": 1}, "type"),
        ({"type": "urban", "id": "abc"}, "id"),
        ({"type": "urban", "id": 1, "saleable_share": 2}, "saleable_share"),
        ({"type": "urban", "id": 1, "saleable_share": 0}, "saleable_share"),
        ({"type": "urban", "id": 1, "construction_cost_eur_m2": 0}, "construction_cost_eur_m2"),
        ({"type": "cadastral", "id": 1001, "sale_price_eur_m2": 100001}, "sale_price_eur_m2"),
    ],
    ids=[
        "unknown-type",
        "missing-id",
        "missing-type",
        "non-integer-id",
        "share-above-1",
        "share-below-0.3",
        "construction-cost-zero",
        "sale-price-above-max",
    ],
)
async def test_malformed_requests_are_422(pg_client, params, field):
    r = await pg_client.get(PANEL, params=params)
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert any(detail["loc"] == ["query", field] for detail in error["details"]), error


# --- round trips, plans, latency ------------------------------------------------------------------


async def test_panel_issues_one_statement_per_call(pg_app):
    async with pg_app.router.lifespan_context(pg_app):
        statements: list[str] = []

        @event.listens_for(pg_app.state.engine.sync_engine, "before_cursor_execute")
        def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            statements.append(statement)

        async with make_client(pg_app) as client:
            for params in (
                {"type": "zone", "id": 1},
                {"type": "document", "id": 2},
                {"type": "cadastral", "id": 1001},
                {"type": "cadastral", "id": 1006},
                {"type": "cadastral", "id": 1004},
                {"type": "urban", "id": 1},
                {"type": "urban", "id": 4},
                {"type": "urban", "id": 1, "saleable_share": 0.8, "sale_price_eur_m2": 2600},
            ):
                statements.clear()
                r = await client.get(PANEL, params=params)
                assert r.status_code == 200, r.text
                assert len(statements) == 1, (params, len(statements))
                assert "planning_parameter_extractions" not in statements[0]

            statements.clear()
            assert (
                await client.get(PANEL, params={"type": "urban", "id": 999999})
            ).status_code == 404
            assert len(statements) == 1
            statements.clear()
            assert (await client.get(PANEL, params={"type": "foo", "id": 1})).status_code == 422
            assert statements == []  # rejected before any database work


def _nodes(plan: dict[str, Any]):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


async def _explain(conn, sql: str, params: dict[str, Any]) -> dict[str, Any]:
    """EXPLAIN ANALYZE of the real statement on realistic table sizes (no planner knobs)."""
    await conn.execute(text(sql), params)  # warm caches: measure steady state, not the first hit
    raw = (await conn.execute(text("EXPLAIN (ANALYZE, FORMAT JSON) " + sql), params)).scalar_one()
    plan = json.loads(raw) if isinstance(raw, str | bytes) else raw
    return plan[0]


def _assert_indexed_and_fast(root: dict[str, Any], expected_indexes: set[str]) -> None:
    nodes = list(_nodes(root["Plan"]))
    index_names = {n["Index Name"] for n in nodes if n.get("Index Name")}
    missing = expected_indexes - index_names
    assert not missing, f"indexes not used: {missing}; used: {sorted(index_names)}"
    seq_scans = {n.get("Relation Name") for n in nodes if n["Node Type"] == "Seq Scan"}
    assert not (seq_scans & LARGE_TABLES), f"sequential scans on {seq_scans & LARGE_TABLES}"
    # far inside the 2 s product budget for selection -> populated panel
    assert root["Execution Time"] < 250, root["Execution Time"]
    assert root["Planning Time"] < 250, root["Planning Time"]


@pytest.mark.parametrize("parcel_id", [1001, 1006, 1007])
async def test_cadastral_plan_uses_spatial_indexes_and_is_fast(pg_conn, parcel_id):
    root = await _explain(pg_conn, CADASTRAL_SQL, {**COMMON, "id": parcel_id})
    _assert_indexed_and_fast(
        root,
        {
            "cadastral_parcels_pkey",
            "idx_planning_documents_coverage_geom",
            "idx_urban_parcels_geom",
        },
    )


@pytest.mark.parametrize("urban_parcel_id", [1, 4, 5])
async def test_urban_plan_uses_spatial_indexes_and_is_fast(pg_conn, urban_parcel_id):
    root = await _explain(pg_conn, URBAN_SQL, {**COMMON, "id": urban_parcel_id})
    _assert_indexed_and_fast(root, {"idx_cadastral_parcels_geom"})
    index_names = {n["Index Name"] for n in _nodes(root["Plan"]) if n.get("Index Name")}
    # the parcel itself by primary key or by the (id, document_id) unique index
    assert index_names & {"urban_parcels_pkey", "uq_urban_parcels_id_document"}, index_names


async def test_zone_and_document_plans_are_indexed_and_fast(pg_conn):
    params = {"municipality_id": "podgorica", "id": 1}
    _assert_indexed_and_fast(
        await _explain(pg_conn, ZONE_SQL, params), {"ix_planning_documents_zone_id"}
    )
    _assert_indexed_and_fast(
        await _explain(pg_conn, DOCUMENT_SQL, {**params, "id": 2}),
        {"planning_documents_pkey", "idx_cadastral_parcels_geom", "ix_urban_parcels_document_id"},
    )


async def test_end_to_end_latency_is_well_within_budget(pg_client):
    for params in (
        {"type": "cadastral", "id": 1001},
        {"type": "urban", "id": 1},
    ):
        timings = []
        for _ in range(10):
            started = perf_counter()
            r = await pg_client.get(PANEL, params=params)
            timings.append(perf_counter() - started)
            assert r.status_code == 200
        assert max(timings) < 1.0, f"{params}: {timings}"  # includes the first connection
        assert statistics.median(timings) < 0.25, f"{params}: {timings}"
