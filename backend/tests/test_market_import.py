"""Market-data imports (core.market): reading, parsing, rule mapping, the LLM step with a scripted
model, range completion and pasted listings. No database: zones and configured range factors are
given directly (the PostGIS path is ``tests/integration/test_market_postgis.py``).

Fixtures: ``tests/fixtures/market/monstat_new_dwellings_q4_2025_synthetic.csv`` (the layout of a
Monstat release table: cp1250, ``;``, decimal commas, a title naming the quarter; SYNTHETIC
figures) and a client range sheet built here with openpyxl (merged metric headers over Min /
Očekivano / Max, a single-figure design column, a zone the database does not have).
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import pytest

from core.extraction.llm import ModelRefused, ScriptedModel
from core.market.listings import PastedListings, percentile, portal_source
from core.market.llm_map import PLACES_SCHEMA, STRUCTURE_SCHEMA
from core.market.model import RangeFactors, RawTable, Sheet, ZoneRef
from core.market.normalise import NormaliseContext, normalise_listings, normalise_table
from core.market.parse import parse_amount, parse_number_token, parse_period, parse_unit
from core.market.readers import ReadError, read_csv, read_table
from core.market.rules import HeaderRules, ZoneMatcher
from core.municipality import load_market_profile

FIXTURES = Path(__file__).parent / "fixtures" / "market"
MONSTAT = FIXTURES / "monstat_new_dwellings_q4_2025_synthetic.csv"
PROFILE = load_market_profile("podgorica")
ZONES = [ZoneRef(1, "Centar", ("centar grada",)), ZoneRef(2, "Stari Aerodrom")]
FACTORS = {1: RangeFactors(0.86, 1.15, 11, 1), 2: RangeFactors(0.9, 1.1, 12, 2)}


def context(kind: str = "statistics", **overrides) -> NormaliseContext:
    values = dict(
        municipality="Podgorica",
        kind=kind,
        source="Monstat" if kind == "statistics" else "Monmaks range sheet",
        retrieved_on=date(2026, 9, 20),
        zones=list(ZONES),
        profile=PROFILE,
        factors=dict(FACTORS),
        mode="never",
    )
    values.update(overrides)
    return NormaliseContext(**values)


def by_key(result) -> dict[tuple[int, str], object]:
    return {(i.zone_id, i.metric): i for i in result.inputs}


def reasons(result) -> dict[str, list]:
    out: dict[str, list] = {}
    for s in result.skipped:
        out.setdefault(s.reason, []).append(s)
    return out


def client_sheet(title: str = "Tržišni rasponi po zonama, Podgorica — stanje avgust 2026"):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Rasponi"
    ws.append([title])
    ws.merge_cells("A1:K1")
    ws.append(
        [
            "Zona",
            "Zemljište €/m²",
            None,
            None,
            "Gradnja €/m²",
            None,
            None,
            "Projektovanje €/m²",
            "Prodaja €/m²",
            None,
            None,
        ]
    )
    for rng in ("B2:D2", "E2:G2", "I2:K2"):
        ws.merge_cells(rng)
    ws.append(
        [
            None,
            "Min",
            "Očekivano",
            "Max",
            "Min",
            "Očekivano",
            "Max",
            None,
            "Min",
            "Očekivano",
            "Max",
        ]
    )
    ws.append(["Centar", 1100, 1350, 1600, 750, 860, 980, 90, 2100, 2450, 2800])
    ws.append(["Stari Aerodrom", 750, 900, 1050, 700, 780, 900, 85, "1.450", "1.650", "1.900"])
    ws.append(["Zagorič", 300, 400, 500, 650, 720, 800, 80, 1100, 1250, 1400])
    ws.append(["Napomena: primjer za testove, nisu stvarni podaci"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# --- reading --------------------------------------------------------------------------------------


def test_csv_encoding_and_delimiter_are_detected():
    table = read_table(MONSTAT.name, MONSTAT.read_bytes())
    assert (table.format, table.encoding, table.delimiter) == ("csv", "cp1250", ";")
    header = table.sheets[0].rows[2]
    assert header[0] == "Opština" and header[3] == "Troškovi gradnje (€/m2)"  # diacritics kept
    assert table.sheets[0].rows[4][:2] == ["Podgorica", "1.905,50"]  # cells untouched
    assert RawTable.from_json(table.to_json()).sheets[0].rows == table.sheets[0].rows


def test_xlsx_merged_headers_are_filled_and_other_files_refused():
    table = read_table("rasponi.xlsx", client_sheet())
    sheet = table.sheets[0]
    assert sheet.rows[1][1:4] == ["Zemljište €/m²"] * 3  # merged over Min / Očekivano / Max
    assert "B2:D2" in sheet.merged
    with pytest.raises(ReadError, match="xls"):
        read_table("old.xls", b"\xd0\xcf\x11\xe0")
    with pytest.raises(ReadError, match="unsupported"):
        read_table("plan.pdf", b"%PDF-1.7")
    with pytest.raises(ReadError, match="rows"):
        read_csv(b"a;b\n" * 20, max_rows=10)


# --- parsing --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "value"),
    [
        ("1.234,56", 1234.56),
        ("1,234.56", 1234.56),
        ("1 234", 1234),
        ("1.620", 1620),
        ("1,5", 1.5),
        ("0,86", 0.86),
        ("1.62", 1.62),
        ("1.234.567", 1234567),
    ],
)
def test_numbers_in_either_convention(token, value):
    assert parse_number_token(token) == pytest.approx(value)


def test_amounts_ranges_and_units():
    assert parse_amount("1.200 – 1.500").values == (1200, 1500)
    assert parse_amount("od 1.200 do 1.500").values == (1200, 1500)
    assert parse_amount("2.450 €/m²").values == (2450,)  # the 2 of m² is not a figure
    assert parse_amount("1.850 (2025)") is None  # a figure next to a year: ambiguous
    assert parse_amount("—") is None and parse_amount(None) is None
    assert parse_unit("EUR/ha").to_eur_m2 == pytest.approx(1e-4)
    assert parse_unit("€ po aru").to_eur_m2 == pytest.approx(0.01)
    assert parse_unit("u hiljadama €", thousands=PROFILE.headers.thousands).scale == 1000
    assert parse_unit("din/m2").currency == "other"


@pytest.mark.parametrize(
    ("label", "day"),
    [
        ("IV kvartal 2025", date(2025, 12, 31)),
        ("III kv. 2025", date(2025, 9, 30)),
        ("Q3 2026", date(2026, 9, 30)),
        ("I polugodište 2026", date(2026, 6, 30)),
        ("septembra 2026", date(2026, 9, 30)),
        ("09/2026", date(2026, 9, 30)),
        ("2025", date(2025, 12, 31)),
        ("30.09.2026", date(2026, 9, 30)),
    ],
)
def test_periods_become_their_last_day(label, day):
    assert parse_period(label, PROFILE.periods).reference_date == day


def test_zone_names_aliases_and_the_municipality():
    matcher = ZoneMatcher(ZONES, PROFILE)
    assert matcher.match("CENTAR").zone_id == 1
    assert matcher.match("Centar grada").method == "alias"
    assert matcher.match("Glavni grad Podgorica").applies_to == "municipality"
    assert matcher.match("MZ Stari Aerodrom").zone_id == 2  # contains exactly one zone name
    assert matcher.match("Nikšić").applies_to == "none"
    rules = HeaderRules(PROFILE)
    assert rules.metric("Cijena građevinskog zemljišta") == "land_rate"
    assert rules.metric("Troškovi gradnje") == "build_rate"
    assert rules.metric("Prosječna cijena m2") == "sale_rate"
    assert rules.bound("Očekivano") == "expected" and rules.bound("Max") == "high"


# --- a statistics table (Monstat layout) ----------------------------------------------------------


def test_monstat_table_gives_municipality_level_sale_and_build_rates():
    result = normalise_table(read_table(MONSTAT.name, MONSTAT.read_bytes()), context())
    items = by_key(result)
    assert set(items) == {(z, m) for z in (1, 2) for m in ("sale_rate", "build_rate")}
    sale = items[(1, "sale_rate")]
    assert (sale.expected, sale.low, sale.high) == (1905.5, 1638.73, 2191.32)  # × 0.86 / 1.15
    assert sale.range_basis == "derived" and sale.source_date == date(2025, 12, 31)
    assert {"municipality_level", "range_derived"} <= set(sale.flags)
    assert sale.mapping["range"]["assumption_id"] == 11
    assert items[(2, "sale_rate")].low == pytest.approx(1714.95)  # zone 2's own factors
    assert items[(1, "build_rate")].expected == 1180
    assert sale.raw["cells"][0] == "Podgorica" and sale.raw["columns"]["expected"]["cell"] == (
        "1.905,50"
    )
    assert sale.source == "Monstat" and sale.normaliser == "rules"
    why = reasons(result)
    # land per m² of DWELLING is not a land rate; other costs are not a metric
    assert why["metric_not_for_kind"][0].detail.startswith("Cijena građevinskog zemljišta")
    assert why["header_ignored"][0].detail.startswith("Ostali troškovi")
    assert {s.detail.split(":")[0] for s in why["no_zone_match"]} == {
        "'Crna Gora'",
        "'Nikšić'",
        "'Budva'",
    }
    assert why["not_data"][0].detail.startswith("Izvor")


def test_a_single_figure_without_configured_factors_has_no_range():
    result = normalise_table(read_table(MONSTAT.name, MONSTAT.read_bytes()), context(factors={}))
    sale = by_key(result)[(1, "sale_rate")]
    assert (sale.low, sale.high, sale.range_basis) == (None, None, "unavailable")
    assert "range_unavailable" in sale.flags


def test_the_municipality_row_factors_widen_a_zone_without_its_own():
    factors = {None: RangeFactors(0.8, 1.2, 99, None)}
    result = normalise_table(
        read_table(MONSTAT.name, MONSTAT.read_bytes()), context(factors=factors)
    )
    sale = by_key(result)[(2, "sale_rate")]
    assert (sale.low, sale.high) == (1524.4, 2286.6)
    assert sale.mapping["range"] == {
        "from": "financial_assumptions",
        "assumption_id": 99,
        "zone_id": None,
        "low_factor": 0.8,
        "high_factor": 1.2,
    }


# --- the client's range sheet ---------------------------------------------------------------------


def test_client_range_sheet_gives_stated_ranges_per_zone():
    result = normalise_table(read_table("rasponi.xlsx", client_sheet()), context("client_ranges"))
    items = by_key(result)
    assert set(items) == {
        (z, m) for z in (1, 2) for m in ("land_rate", "build_rate", "design_rate", "sale_rate")
    }
    land = items[(1, "land_rate")]
    assert (land.low, land.expected, land.high, land.range_basis) == (1100, 1350, 1600, "stated")
    assert land.source_date == date(2026, 8, 31)  # "stanje avgust 2026" in the title
    assert (items[(2, "sale_rate")].low, items[(2, "sale_rate")].high) == (1450, 1900)
    design = items[(1, "design_rate")]  # one figure: the zone's configured factors
    assert (design.expected, design.low, design.high) == (90, 77.4, 103.5)
    assert design.range_basis == "derived"
    assert all("municipality_level" not in i.flags for i in result.inputs)
    assert reasons(result)["no_zone_match"][0].detail.startswith("'Zagorič'")


def test_only_bounds_give_the_midpoint_and_bad_bounds_are_flagged():
    sheet = Sheet(
        "s",
        [
            ["Zona", "Prodaja min €/m²", "Prodaja max €/m²", "Gradnja €/m²"],
            ["Centar", 2000, 2600, "700 - 900"],
            ["Stari Aerodrom", 1800, 1500, 1_000_000],
        ],
    )
    result = normalise_table(RawTable("xlsx", [sheet]), context("client_ranges"))
    items = by_key(result)
    sale = items[(1, "sale_rate")]
    assert (sale.low, sale.expected, sale.high) == (2000, 2300, 2600)
    assert "expected_midpoint" in sale.flags
    build = items[(1, "build_rate")]  # a range in one cell
    assert (build.low, build.expected, build.high) == (700, 800, 900)
    assert "range_in_one_cell" in build.flags
    swapped = items[(2, "sale_rate")]
    assert (swapped.low, swapped.high) == (1800, 1500) and "bounds_inconsistent" in swapped.flags
    assert "implausible" in items[(2, "build_rate")].flags  # flagged, never dropped


def test_units_are_converted_and_other_currencies_refused():
    sheet = Sheet(
        "s",
        [
            ["Zona", "Zemljište €/ha", "Prodaja (din/m2)"],
            ["Centar", 13_500_000, 250_000],
        ],
    )
    result = normalise_table(RawTable("xlsx", [sheet]), context("client_ranges"))
    land = by_key(result)[(1, "land_rate")]
    assert land.expected == 1350 and "unit_converted" in land.flags
    assert (1, "sale_rate") not in by_key(result)
    assert reasons(result)["currency_not_eur"]


# --- the LLM step ---------------------------------------------------------------------------------


MESSY = Sheet(
    "prices",
    [
        ["Loc.", "psm lo", "psm mid", "psm hi", "n"],
        ["Centar", 2000, 2300, 2600, 41],
        ["Blok 5", 1500, 1700, 1900, 17],
        ["Zabjelo", 1400, 1600, 1800, 12],
    ],
)


def llm_script(system, user, schema):
    if schema is PLACES_SCHEMA or "places" in schema.get("properties", {}):
        return {
            "places": [
                {
                    "name": "Blok 5",
                    "applies_to": "zone",
                    "zone_id": 2,
                    "confidence": 0.8,
                    "reason": "Blok 5 lies next to Stari Aerodrom",
                },
                {
                    "name": "Zabjelo",
                    "applies_to": "zone",
                    "zone_id": 2,
                    "confidence": 0.4,
                    "reason": "not sure",
                },
            ]
        }
    return {
        "header_rows": [0],
        "columns": [
            {
                "column": 0,
                "role": "geography",
                "metric": None,
                "bound": None,
                "unit_as_printed": None,
                "period_as_printed": None,
                "reason": None,
            },
            {
                "column": 1,
                "role": "value",
                "metric": "sale_rate",
                "bound": "low",
                "unit_as_printed": "psm",
                "period_as_printed": None,
                "reason": None,
            },
            {
                "column": 2,
                "role": "value",
                "metric": "sale_rate",
                "bound": "expected",
                "unit_as_printed": "psm",
                "period_as_printed": None,
                "reason": None,
            },
            {
                "column": 3,
                "role": "value",
                "metric": "sale_rate",
                "bound": "high",
                "unit_as_printed": "psm",
                "period_as_printed": None,
                "reason": None,
            },
            {
                "column": 4,
                "role": "ignore",
                "metric": None,
                "bound": None,
                "unit_as_printed": None,
                "period_as_printed": None,
                "reason": "listing count",
            },
            {
                "column": 9,
                "role": "value",
                "metric": "land_rate",
                "bound": "expected",
                "unit_as_printed": None,
                "period_as_printed": None,
                "reason": None,
            },
        ],
        "row_metrics": [],
        "non_data_rows": [],
        "table_geography": None,
        "table_period_as_printed": "Q2 2026",
        "table_unit_as_printed": "EUR per m2",
        "notes": None,
    }


def test_the_llm_maps_what_the_rules_cannot_and_code_reads_every_figure():
    model = ScriptedModel(llm_script, name="claude-test")
    result = normalise_table(
        RawTable("xlsx", [MESSY]), context("client_ranges", mode="auto"), model
    )
    items = by_key(result)
    centar = items[(1, "sale_rate")]
    assert (centar.low, centar.expected, centar.high) == (2000, 2300, 2600)
    assert centar.source_date == date(2026, 6, 30)  # the label the model copied, parsed by code
    assert "columns_mapped_by_ai" in centar.flags and "zone_mapped_by_ai" not in centar.flags
    placed = items[(2, "sale_rate")]  # "Blok 5", placed by the model
    assert placed.expected == 1700 and "zone_mapped_by_ai" in placed.flags
    assert placed.confidence == 0.8
    assert result.normaliser == "rules+llm:claude-test@market-1.0"
    assert result.llm["calls"] == 2
    why = reasons(result)
    assert why["no_zone_match"][0].detail.startswith("'Zabjelo'")  # 0.4: stays unplaced
    assert any("column 9" in note for note in result.llm["notes"])  # out of range: dropped
    # the model is asked for meanings only: no field of either answer holds a figure
    column = STRUCTURE_SCHEMA["$defs"]["OutColumn"]["properties"]
    assert set(column) == {
        "column",
        "role",
        "metric",
        "bound",
        "unit_as_printed",
        "period_as_printed",
        "reason",
    }
    place = PLACES_SCHEMA["$defs"]["OutPlace"]["properties"]
    assert set(place) == {"name", "applies_to", "zone_id", "confidence", "reason"}


def test_without_the_llm_an_unreadable_sheet_is_reported_not_guessed():
    result = normalise_table(RawTable("xlsx", [MESSY]), context("client_ranges", mode="never"))
    assert result.inputs == []
    assert reasons(result)["structure_not_recognised"]
    auto = ScriptedModel(lambda *a: pytest.fail("not needed"))
    clean = normalise_table(
        read_table("rasponi.xlsx", client_sheet()),
        context("client_ranges", mode="auto", zones=ZONES + [ZoneRef(3, "Zagorič")]),
        auto,
    )
    assert auto.calls == [] and clean.normaliser == "rules"  # the rules read it all


def test_a_refusing_model_leaves_the_rules_result_and_says_so():
    def refuse(system, user, schema):
        raise ModelRefused("declined")

    result = normalise_table(
        read_table(MONSTAT.name, MONSTAT.read_bytes()),
        context(mode="auto"),
        ScriptedModel(refuse),
    )
    assert len(result.inputs) == 4
    assert any("could not place" in issue for issue in result.issues)


# --- pasted listings ------------------------------------------------------------------------------


LISTINGS = """Lokacija; Cijena €/m²; Datum
# Realitica, septembar 2026
Centar, Njegoševa; 2.400; 02.09.2026
Centar grada; 2.100 €/m²; 2026-09-05
centar; 2.650; 06.09.2026
Centar - Hercegovačka; 2.300; 07.09.2026
Centar; 2.500; 10.09.2026
Centar; 12; 10.09.2026
Stari Aerodrom; 1.700; 11.09.2026
Stari Aerodrom; 1.650; 12.09.2026
Podgorica; 1.900; 12.09.2026
Tološi; 1.500; 12.09.2026
Masline; 1.550;
nije oglas
"""


def test_pasted_listings_give_the_median_and_quartiles_per_zone():
    batch = PastedListings(LISTINGS).fetch()
    assert len(batch.listings) == 10
    why = {s.reason for s in batch.skipped}
    assert {"header", "comment", "listing_date_missing", "listing_unreadable"} <= why
    result = normalise_listings(batch, context("listings", source="Realitica"), min_listings=5)
    (centar,) = result.inputs
    assert (centar.zone_id, centar.metric, centar.range_basis) == (1, "sale_rate", "listings")
    assert (centar.low, centar.expected, centar.high) == (2300, 2400, 2500)
    assert centar.source_date == date(2026, 9, 10) and "asking_prices" in centar.flags
    assert len(centar.raw["listings"]) == 5
    skipped = reasons(result)
    assert skipped["implausible_price"][0].detail.startswith("12")  # 12 EUR/m²: excluded, said
    assert skipped["too_few_listings"][0].detail.startswith("Stari Aerodrom: 2 listings")
    assert skipped["not_zone_specific"][0].detail == "Podgorica"
    assert skipped["no_zone_match"][0].detail == "Tološi"


def test_percentiles_interpolate_and_portals_are_deferred():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([10.0], 25) == 10.0
    with pytest.raises(NotImplementedError, match="pilot"):
        portal_source("realitica")
