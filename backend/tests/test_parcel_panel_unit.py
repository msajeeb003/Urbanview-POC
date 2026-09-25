"""The display-shaped panels without a database: the builders on canned statement rows (sources on
every stated value, reasons for missing ones, scope precedence, the calculation basis and its
explanation, the split case, uncovered and unpublished parcels, market and assumptions), Group 2
equal to the shared engine on the exposed inputs, the zone panel, the response cache (miss, hit,
304, bypass, fail-open) and the routes without PostGIS."""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from pydantic import BaseModel

from api.services.panel_cache import PanelCache, Stamp, etag_matches
from api.services.parcel_panel import GROUP2_KEYS, build_parcel_panel, build_zone_panel
from core.engine import shared
from core.errors import NotFoundError
from core.municipality import load_profile
from tests.helpers import make_app, make_client, make_settings

PROFILE = load_profile("podgorica")

FIELDS: list[dict[str, Any]] = [
    {
        "key": "land_use",
        "label_en": "Land use designation",
        "label_me": "Namjena površina",
        "abbreviation": None,
        "unit": None,
        "value_type": "text",
        "computed": False,
        "formula": None,
    },
    {
        "key": "max_site_coverage_pct",
        "label_en": "Max site coverage",
        "label_me": "Maksimalni indeks zauzetosti",
        "abbreviation": "IZ",
        "unit": "%",
        "value_type": "number",
        "computed": False,
        "formula": None,
    },
    {
        "key": "max_far",
        "label_en": "Max floor area ratio",
        "label_me": "Maksimalni indeks izgrađenosti",
        "abbreviation": "II",
        "unit": None,
        "value_type": "number",
        "computed": False,
        "formula": None,
    },
    {
        "key": "max_height_m",
        "label_en": "Max building height",
        "label_me": "Maksimalna visina objekta",
        "abbreviation": None,
        "unit": "m",
        "value_type": "number",
        "computed": False,
        "formula": None,
    },
    {
        "key": "max_floors",
        "label_en": "Max number of floors",
        "label_me": "Maksimalna spratnost",
        "abbreviation": None,
        "unit": None,
        "value_type": "text",
        "computed": False,
        "formula": None,
    },
    {
        "key": "building_line_m",
        "label_en": "Building line (setback from public area)",
        "label_me": "Građevinska linija",
        "abbreviation": None,
        "unit": "m",
        "value_type": "number",
        "computed": False,
        "formula": None,
    },
    {
        "key": "setback_neighbours_m",
        "label_en": "Min distance from neighbouring parcels",
        "label_me": "Minimalna udaljenost od susjednih parcela",
        "abbreviation": None,
        "unit": "m",
        "value_type": "number",
        "computed": False,
        "formula": None,
    },
    {
        "key": "parking_requirement",
        "label_en": "Parking requirement",
        "label_me": "Parkiranje",
        "abbreviation": None,
        "unit": None,
        "value_type": "text",
        "computed": False,
        "formula": None,
    },
    {
        "key": "min_green_area_pct",
        "label_en": "Min green area",
        "label_me": "Minimalna zelena površina",
        "abbreviation": None,
        "unit": "%",
        "value_type": "number",
        "computed": False,
        "formula": None,
    },
    {
        "key": "planned_parcel_area_m2",
        "label_en": "Planned parcel area (per plan)",
        "label_me": "Površina urbanističke parcele",
        "abbreviation": None,
        "unit": "m²",
        "value_type": "number",
        "computed": False,
        "formula": None,
    },
    {
        "key": "utilities",
        "label_en": "Infrastructure utilities",
        "label_me": "Infrastrukturna opremljenost",
        "abbreviation": None,
        "unit": None,
        "value_type": "text",
        "computed": False,
        "formula": None,
    },
    {
        "key": "max_gfa_m2",
        "label_en": "Calculated max gross floor area",
        "label_me": "Maksimalna bruto građevinska površina",
        "abbreviation": "BGP",
        "unit": "m²",
        "value_type": "number",
        "computed": True,
        "formula": "max_far × basis_area_m2",
    },
    {
        "key": "max_coverage_area_m2",
        "label_en": "Max coverage area",
        "label_me": "Maksimalna površina pod objektom",
        "abbreviation": None,
        "unit": "m²",
        "value_type": "number",
        "computed": True,
        "formula": "max_site_coverage_pct / 100 × basis_area_m2",
    },
]
DOC2 = {
    "id": 2,
    "name": "DUP Centar – Zona C2",
    "type": "DUP",
    "status": "adopted",
    "source": "eRegistri",
    "registry_url": "https://lamp.gov.me/doc/2",
    "amends_document_id": None,
    "coverage_live": True,
}
DOC3 = {
    "id": 3,
    "name": "Izmjene i dopune DUP-a Centar 2023",
    "type": "DUP",
    "status": "in_progress",
    "source": "eRegistri",
    "registry_url": None,
    "amends_document_id": 2,
    "coverage_live": False,
}
CAD = {
    "id": 1001,
    "parcel_number": "1042",
    "sub_number": None,
    "ko_name": "Podgorica I",
    "street_address": "Njegoševa 12",
    "area_m2": 1370.9,
    "public_ownership": False,
    "restitution_or_legal_burden": True,
    "centroid": {"lat": 42.4411, "lng": 19.2636},
    "bbox": [19.2630, 42.4405, 19.2642, 42.4417],
}
UP12 = {
    "id": 1,
    "urban_parcel_number": "UP 12",
    "area_m2": 959.6,
    "overlap_m2": 959.6,
    "overlap_fraction": 0.7,
    "area_delta_m2": -411.3,
    "rank": 1,
    "document": DOC2,
    "urban_block": {"id": 1, "block_ref": "C2-01"},
}
MARKET = {
    "id": 1,
    "zone_id": 1,
    "version": 1,
    "land_rate_eur_m2": 1350,
    "build_rate_eur_m2": 860,
    "design_rate_eur_m2": 90,
    "sale_rate_eur_m2": 2450,
    "range_low_factor": 0.86,
    "range_high_factor": 1.15,
    "source": "Realitica, Estitor, Monstat (sample)",
    "source_date": "2026-08-01",
    "effective_from": "2026-09-22T10:00:00+00:00",
    "bounds": {
        "land": [None, None],
        "build": [None, None],
        "design": [None, None],
        "sale": [2200, 2700],
    },
}
STATED = {
    "land_use": ("text", "Residential – mixed use", 12),
    "max_site_coverage_pct": ("number", 55, 13),
    "max_far": ("number", 3.2, 13),
    "max_height_m": ("number", 27.5, 14),
    "max_floors": ("text", "P+8", 14),
    "building_line_m": ("number", 5, 15),
    "setback_neighbours_m": ("number", 4, 15),
    "parking_requirement": ("text", "1 space per apartment", 16),
    "min_green_area_pct": ("number", 20, 17),
    "planned_parcel_area_m2": ("number", 959.6, 18),
    "utilities": ("text", "water, sewage, electricity", 19),
}


def value_row(
    key: str,
    value: Any,
    page: int,
    *,
    scope: str = "parcel",
    value_id: int = 0,
    document_id: int = 2,
) -> dict[str, Any]:
    kind = "text" if isinstance(value, str) else "number"
    return {
        "value_id": value_id or (100 + list(STATED).index(key) if key in STATED else 999),
        "field_key": key,
        "value_text": value if kind == "text" else None,
        "value_number": value if kind == "number" else None,
        "unit": None,
        "source_page": page,
        "source_bbox": [72, 400, 520, 418],
        "source_note": f"table 3 – {key}",
        "document_id": document_id,
        "document_name": DOC2["name"],
        "registry_url": DOC2["registry_url"],
        "file_id": 7,
        "scope": scope,
    }


def all_values(*, skip: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return [value_row(k, v, p) for k, (_, v, p) in STATED.items() if k not in skip]


def row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "cadastral": CAD,
        "governing_document": DOC2,
        "basis_document": DOC2,
        "urban_block": {"id": 1, "block_ref": "C2-01"},
        "zone": {"id": 1, "name": "Centar"},
        "links": [UP12],
        "amendments": [DOC3],
        "fields": FIELDS,
        "values": all_values(),
        "gaps": [],
        "market": MARKET,
        "version_id": 1,
        "data_version": "sample-2026-09-22",
        "data_version_date": "2026-09-22",
    }
    base.update(overrides)
    return copy.deepcopy(base)


def fields_of(panel) -> dict[str, Any]:
    return {f.key: f for f in panel.group1.fields}


# --- group 1 --------------------------------------------------------------------------------------


def test_every_stated_value_carries_its_source():
    panel = build_parcel_panel(row(), PROFILE)
    fields = panel.group1.fields
    assert [f.key for f in fields] == list(STATED)  # the 11, dictionary order
    for field in fields:
        assert field.status == "stated" and field.scope == "parcel" and field.reason is None
        source = field.source
        assert source.document_id == 2 and source.document == "DUP Centar – Zona C2"
        assert source.page == STATED[field.key][2] and source.bbox == [72, 400, 520, 418]
        assert source.file_id == 7 and source.viewer_url == f"/v1/source/value/{source.value_id}"
        assert field.label_en and field.label_me
    assert (
        fields_of(panel)["max_far"].value == 3.2 and fields_of(panel)["max_floors"].value == "P+8"
    )
    computed = {c.key: c for c in panel.group1.computed}
    assert computed["max_gfa_m2"].value == pytest.approx(3070.72)
    assert [(i.key, i.value) for i in computed["max_gfa_m2"].inputs] == [
        ("max_far", 3.2),
        ("basis_area_m2", 959.6),
    ]
    assert computed["max_coverage_area_m2"].value == pytest.approx(527.78)
    assert panel.group1.document.id == 2 and panel.group1.urban_parcel_number == "UP 12"
    assert panel.group1.title_en == "Planning parameters" and panel.group1.tier == "free"


def test_missing_height_is_not_in_document_and_group2_still_computes():
    panel = build_parcel_panel(row(values=all_values(skip=("max_height_m",))), PROFILE)
    height = fields_of(panel)["max_height_m"]
    assert height.status == "not_stated" and height.value is None and height.source is None
    assert height.reason == "not_in_document"
    assert height.reason_en == "not stated in the planning document"
    assert height.reason_me == "nije navedeno u planskom dokumentu"
    assert panel.group2.status == "ok"
    assert all(f.status == "ok" for f in panel.group2.fields)
    (flag,) = panel.group2.input_flags
    assert (flag.key, flag.reason, flag.used_by_formulas, flag.affects) == (
        "max_height_m",
        "not_in_document",
        False,
        [],
    )
    assert "height" in flag.note_en and flag.note_me


def test_rejected_gaps_and_scope_precedence():
    gaps = [
        {"field_key": "max_height_m", "reason": "rejected", "document_id": 2, "scope": "parcel"},
        {"field_key": "max_far", "reason": "rejected", "document_id": 2, "scope": "parcel"},
    ]
    far_block = value_row("max_far", 2.8, 20, scope="block", value_id=501)
    far_doc = value_row("max_far", 1.9, 21, scope="document", value_id=502)
    values = [*all_values(skip=("max_height_m", "max_far")), far_block, far_doc]
    values.sort(key=lambda r: ("parcel", "block", "zone", "document").index(r["scope"]))
    panel = build_parcel_panel(row(values=values, gaps=gaps), PROFILE)
    fields = fields_of(panel)
    assert fields["max_height_m"].reason == "rejected" and fields["max_height_m"].value is None
    assert "rejected" in fields["max_height_m"].reason_en
    # a value at any scope beats a gap; the block's beats the document's
    assert fields["max_far"].status == "stated" and fields["max_far"].value == 2.8
    assert fields["max_far"].scope == "block" and fields["max_far"].source.value_id == 501
    flags = {f.key: f for f in panel.group2.input_flags}
    assert flags["max_height_m"].reason == "rejected" and "max_far" not in flags


def test_unpublished_is_not_reported_as_missing_from_the_document():
    panel = build_parcel_panel(
        row(version_id=None, data_version=None, data_version_date=None, links=[], values=[]),
        PROFILE,
    )
    assert panel.data_version == "unpublished" and panel.version_id is None
    assert {f.reason for f in panel.group1.fields} == {"unpublished"}
    assert panel.header.calculation_basis.reason == "unpublished"


# --- header ---------------------------------------------------------------------------------------


def test_header_explains_the_planned_parcel_basis():
    panel = build_parcel_panel(row(), PROFILE)
    header = panel.header
    assert (header.ko, header.parcel_number, header.title) == (
        "Podgorica I",
        "1042",
        "KO Podgorica I, 1042",
    )
    assert header.zone.name == "Centar" and header.urban_block.block_ref == "C2-01"
    assert [(d.id, d.role, d.status, d.status_label_me) for d in header.documents] == [
        (2, "governing", "adopted", "usvojen"),
        (3, "amendment", "in_progress", "u izradi"),
    ]
    assert {f.key: f.value for f in header.flags} == {
        "public_ownership": False,
        "restitution_or_legal_burden": True,
    }
    basis = header.calculation_basis
    assert (basis.basis, basis.area_m2, basis.reason, basis.split) == (
        "urban",
        959.6,
        "planned_parcel",
        False,
    )
    assert "UP 12" in basis.explanation_en and "70%" in basis.explanation_en
    assert "UP 12" in basis.explanation_me and basis.links_source == "parcel_links"
    (link,) = basis.links
    assert (link.urban_parcel_number, link.overlap_pct, link.rank, link.primary) == (
        "UP 12",
        70.0,
        1,
        True,
    )
    areas = header.areas
    assert (areas.cadastral_m2, areas.planned_m2, areas.delta_m2, areas.delta_pct) == (
        1370.9,
        959.6,
        -411.3,
        -30.0,
    )
    assert areas.mismatch and "-411.3" in areas.note_en and areas.planned_stated_m2 == 959.6
    assert panel.covered and panel.coverage_note_en is None
    assert panel.bbox == CAD["bbox"] and panel.centroid.lat == 42.4411


def test_split_parcel_explains_the_primary_link():
    up31 = {
        **UP12,
        "id": 5,
        "urban_parcel_number": "UP 31",
        "area_m2": 1233.8,
        "overlap_m2": 1233.8,
        "overlap_fraction": 0.5625,
        "area_delta_m2": -959.6,
    }
    up32 = {
        **UP12,
        "id": 6,
        "urban_parcel_number": "UP 32",
        "area_m2": 904.8,
        "overlap_m2": 904.8,
        "overlap_fraction": 0.4125,
        "area_delta_m2": -1288.6,
        "rank": 2,
    }
    cad = {**CAD, "area_m2": 2193.4}
    panel = build_parcel_panel(row(cadastral=cad, links=[up31, up32]), PROFILE)
    basis = panel.header.calculation_basis
    assert basis.reason == "split" and basis.split and basis.area_m2 == 1233.8
    assert [(li.urban_parcel_number, li.primary) for li in basis.links] == [
        ("UP 31", True),
        ("UP 32", False),
    ]
    assert "UP 31, UP 32" in basis.explanation_en and "56.2%" in basis.explanation_en
    assert "UP 31" in basis.explanation_me
    assert panel.header.areas.linked_planned_total_m2 == 2138.6
    assert panel.group1.urban_parcel_number == "UP 31"


def test_no_planned_parcel_uses_the_cadastral_area():
    panel = build_parcel_panel(row(links=[]), PROFILE)
    basis = panel.header.calculation_basis
    assert (basis.basis, basis.area_m2, basis.reason) == ("cadastral", 1370.9, "no_planned_parcel")
    assert "DUP Centar – Zona C2" in basis.explanation_en
    assert panel.header.areas.planned_m2 is None and not panel.header.areas.mismatch
    assert panel.group2.basis_area_m2 == 1370.9
    assert panel.engine.inputs["planning"]["calculation_basis"] == "cadastral"
    assert panel.group1.urban_parcel_number is None


def test_uncovered_parcel_is_a_neutral_200_payload():
    panel = build_parcel_panel(
        row(governing_document=None, basis_document=None, links=[], amendments=[]), PROFILE
    )
    assert panel.covered is False and panel.coverage_note_en and panel.coverage_note_me
    assert (panel.group1, panel.market, panel.assumptions, panel.group2, panel.engine) == (
        None,
        None,
        None,
        None,
        None,
    )
    assert panel.header.calculation_basis.reason == "not_covered"
    assert panel.header.documents == []


# --- market, assumptions, group 2 -----------------------------------------------------------------


def test_market_and_assumptions():
    panel = build_parcel_panel(row(), PROFILE)
    market = panel.market
    assert market.scope == "zone" and market.zone.name == "Centar"
    sale = market.sale_price_eur_m2
    assert (sale.low, sale.expected, sale.high, sale.kind) == (2200, 2450, 2700, "absolute")
    assert market.source == "Realitica, Estitor, Monstat (sample)"
    assert market.source_date == "2026-08-01" and market.version.id == 1
    items = {i.key: i for i in panel.assumptions.items}
    assert list(items) == [
        "construction_cost_eur_m2",
        "saleable_share",
        "sale_price_eur_m2",
        "land_value_eur_m2",
        "design_documentation_eur_m2",
    ]
    build = items["construction_cost_eur_m2"]
    assert (build.value, build.low, build.high) == (860, 739.6, 989.0)
    assert (build.source, build.editable, build.engine_edit_key) == (
        "market",
        True,
        "construction_cost_per_m2",
    )
    share = items["saleable_share"]
    assert (share.value, share.unit, share.source, share.editable) == (
        0.7,
        "share",
        "product_default",
        True,
    )
    assert items["sale_price_eur_m2"].engine_edit_key == "market_value_per_m2"
    assert items["land_value_eur_m2"].editable is False and items["land_value_eur_m2"].value == 1350
    assert items["design_documentation_eur_m2"].value == 90
    assert panel.assumptions.formula_version == "poc-1" and panel.assumptions.market_version.id == 1


def test_group2_is_the_shared_engine_on_the_exposed_inputs():
    for variant in (
        row(),
        row(links=[]),
        row(values=all_values(skip=("max_far",))),
        row(market=None),
    ):
        panel = build_parcel_panel(variant, PROFILE)
        expected = shared.calculate(panel.engine.inputs)["fields"]
        assert [f.key for f in panel.group2.fields] == list(GROUP2_KEYS)
        for field in panel.group2.fields:
            engine = expected[field.engine_key]
            assert (field.status, field.low, field.expected, field.high) == (
                engine["status"],
                engine["low"],
                engine["expected"],
                engine["high"],
            ), field.key
            assert field.reason == engine["reason"] and field.range_kind == engine["range_kind"]
        assert panel.engine.formula_version == "poc-1" and panel.engine.deterministic
        assert panel.engine.edit_keys["saleable_share"] == "saleable_share"


def test_missing_far_and_missing_market_row():
    no_far = build_parcel_panel(row(values=all_values(skip=("max_far",))), PROFILE)
    figures = {f.key: f for f in no_far.group2.fields}
    assert figures["land_value_eur"].status == "ok"
    assert figures["saleable_area_m2"].reason == "requires_gfa"
    assert no_far.group2.status == "partial"
    flag = {f.key: f for f in no_far.group2.input_flags}["max_far"]
    assert flag.used_by_formulas and "roi_pct" in flag.affects and "revenue_eur" in flag.affects

    no_market = build_parcel_panel(row(market=None), PROFILE)
    assert no_market.market is None
    figures = {f.key: f for f in no_market.group2.fields}
    assert figures["saleable_area_m2"].status == "ok"
    assert figures["profit_eur"].reason == "no_market_data"
    assert figures["profit_eur"].reason_params == {"zone_name": "Centar"}
    assert "Centar" in figures["profit_eur"].reason_en
    assert no_market.group2.status == "partial"
    items = {i.key: i for i in no_market.assumptions.items}
    assert items["construction_cost_eur_m2"].value is None and items["saleable_share"].value == 0.7


def test_every_label_is_bilingual():
    panel = build_parcel_panel(row(values=all_values(skip=("max_height_m", "max_far"))), PROFILE)
    labelled = [
        *panel.group1.fields,
        *panel.group1.computed,
        *panel.group2.fields,
        *panel.assumptions.items,
        *panel.group2.input_flags,
        *panel.header.flags,
    ]
    for item in labelled:
        assert item.label_en and item.label_me, item
    for block in (panel.group1, panel.group2, panel.assumptions, panel.market):
        assert block.title_en and block.title_me
    assert panel.group2.disclaimer_en and panel.group2.disclaimer_status == "placeholder"


# --- zone panel -----------------------------------------------------------------------------------


def test_zone_panel_builder():
    zone_row = {
        "zone": {"id": 1, "name": "Centar", "general_planning_summary": "Mixed-use centre."},
        "documents": [
            {**DOC2, "file_available": True},
            {**DOC3, "file_available": False},
        ],
        "counts": {"documents": 2, "adopted": 1, "in_progress": 1, "superseded": 0},
        "typical_parameters": None,
        "version_id": 1,
        "data_version": "sample-2026-09-22",
        "data_version_date": "2026-09-22",
    }
    panel = build_zone_panel(zone_row, PROFILE)
    assert (panel.title, panel.summary, panel.zone_id) == ("Centar", "Mixed-use centre.", 1)
    assert panel.summary_label_en == "General planning summary"
    docs = {d.id: d for d in panel.documents}
    assert docs[2].covered and docs[2].file_available and docs[2].status_label_en == "adopted"
    assert docs[2].type_name.startswith("Detaljni urbanistički plan")
    assert docs[3].covered is False and docs[3].status == "in_progress"
    assert panel.counts.adopted == 1 and panel.typical_parameters is None


# --- cache ----------------------------------------------------------------------------------------


class Payload(BaseModel):
    value: int


def cache_with(redis: Any, *, ttl: int = 60, stamp: Stamp | None = None) -> PanelCache:
    cache = PanelCache(
        None,  # type: ignore[arg-type] - stamp() is replaced below
        lambda: redis,
        municipality_id="podgorica",
        ttl_seconds=ttl,
        namespace="test",
    )
    cache.stamp = AsyncMock(return_value=stamp or Stamp(version_id=1, token="abc"))
    return cache


async def test_cache_miss_hit_revalidate_and_new_state():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = cache_with(redis)
    build = AsyncMock(return_value=Payload(value=7))
    first = await cache.serve("parcel", 1001, build)
    second = await cache.serve("parcel", 1001, build)
    assert (first.cache, second.cache) == ("miss", "hit")
    assert first.body == second.body == b'{"value":7}' and first.etag == second.etag
    assert build.await_count == 1
    assert await redis.ttl("panel:test:podgorica:parcel:1001:1:abc") > 0
    revalidated = await cache.serve("parcel", 1001, build, if_none_match=f"W/{first.etag}")
    assert revalidated.body is None and revalidated.cache == "revalidated"
    cache.stamp = AsyncMock(return_value=Stamp(version_id=2, token="abc"))  # a new publish
    after = await cache.serve("parcel", 1001, build)
    assert after.cache == "miss" and after.etag != first.etag and build.await_count == 2
    assert etag_matches("*", first.etag) and not etag_matches(None, first.etag)
    assert etag_matches(f'"x", {first.etag}', first.etag)


async def test_cache_disabled_broken_and_missing_entities():
    build = AsyncMock(return_value=Payload(value=1))
    disabled = cache_with(fakeredis.aioredis.FakeRedis(decode_responses=True), ttl=0)
    assert (await disabled.serve("zone", 1, build)).cache == "bypass"

    class Broken:
        async def get(self, key):
            raise ConnectionError("redis down")

        async def set(self, key, value, ex=None):
            raise ConnectionError("redis down")

    broken = cache_with(Broken())
    view = await broken.serve("zone", 1, build)
    assert view.cache == "bypass" and view.body == b'{"value":1}'
    assert broken._skip_until > 0  # skipped for a while, no second timeout per request

    missing = cache_with(fakeredis.aioredis.FakeRedis(decode_responses=True))
    with pytest.raises(NotFoundError):
        await missing.serve("parcel", 5, AsyncMock(side_effect=NotFoundError("gone")))


# --- routes ---------------------------------------------------------------------------------------


async def test_routes_without_postgis_answer_503():
    app = make_app(make_settings())
    async with app.router.lifespan_context(app), make_client(app) as client:
        parcel = await client.get("/v1/parcels/1001/panel")
        zone = await client.get("/v1/zones/1/panel")
    assert parcel.status_code == 503 and zone.status_code == 503
    assert parcel.json()["error"]["code"] == "service_unavailable"
