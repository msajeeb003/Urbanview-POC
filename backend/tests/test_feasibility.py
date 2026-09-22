"""Feasibility engine (formula version ``poc-1``).

Three things are proven here (contract section 4 / 6):
- parity with ``packages/formula-engine/fixtures/feasibility.json`` (every case, tolerance 0.5
  for euros and 0.05 for areas / ROI), the file a future TypeScript engine is held to;
- the dependency / reason-code rules, asserted directly;
- every range satisfies low <= expected <= high.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.engine.feasibility import (
    COST_ROW_KEYS,
    FIELD_KEYS,
    FORMULA_VERSION,
    Assumptions,
    FeasibilityResult,
    MarketInputs,
    ReasonCode,
    compute_feasibility,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "formula-engine"
    / "fixtures"
    / "feasibility.json"
)
EURO_TOLERANCE = 0.5
AREA_TOLERANCE = 0.05  # also ROI (1 decimal)

CENTAR = MarketInputs(
    land_rate_eur_m2=1350, build_rate_eur_m2=860, design_rate_eur_m2=90, sale_rate_eur_m2=2450
)
UP12 = {"basis_area_m2": 959.6, "max_far": 3.2, "max_site_coverage_pct": 55}
MONEY_FIELDS = ("construction_cost_eur", "revenue_eur", "profit_eur", "roi_pct")
AREA_FIELDS = ("max_gfa_m2", "max_coverage_area_m2", "saleable_area_m2")


def load_fixture() -> dict[str, Any]:
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


FIXTURE = load_fixture()
CASES: list[dict[str, Any]] = FIXTURE["cases"]


def run_case(case: dict[str, Any]) -> FeasibilityResult:
    inputs = case["inputs"]
    market = None if inputs["market"] is None else MarketInputs(**inputs["market"])
    return compute_feasibility(
        inputs["basis_area_m2"],
        inputs["max_far"],
        inputs["max_site_coverage_pct"],
        market,
        Assumptions(**inputs["assumptions"]),
        market_reason_code=inputs.get("market_reason_code"),
        market_reason_params=inputs.get("market_reason_params"),
    )


def tolerance_for(key: str) -> float:
    return EURO_TOLERANCE if key.endswith("_eur") else AREA_TOLERANCE


def assert_field_matches(got: dict[str, Any], expected: dict[str, Any]) -> None:
    for attr in ("key", "status", "reason_code", "reason_params", "range_kind"):
        assert got[attr] == expected[attr], (got["key"], attr, got[attr], expected[attr])
    for bound in ("low", "expected", "high"):
        if expected[bound] is None:
            assert got[bound] is None, (got["key"], bound, got[bound])
        else:
            assert got[bound] is not None, (got["key"], bound)
            assert abs(got[bound] - expected[bound]) <= tolerance_for(got["key"]), (
                got["key"],
                bound,
                got[bound],
                expected[bound],
            )


def assert_ranges_ordered(result: FeasibilityResult) -> None:
    lo = result.assumptions_used.range_low_factor
    hi = result.assumptions_used.range_high_factor
    for item in (*result.fields, *result.cost_rows):
        if item.status != "ok":
            assert item.low is None and item.expected is None and item.high is None
            continue
        assert item.low is not None and item.expected is not None and item.high is not None
        assert item.low <= item.expected <= item.high, item
        if item.range_kind == "deterministic":
            assert item.low == item.expected == item.high, item
        elif item.expected != 0 and lo is not None and lo < 1 < hi:
            assert item.low < item.expected < item.high, item


# --- fixture parity -------------------------------------------------------------------------


def test_fixture_header():
    assert FIXTURE["formula_version"] == FORMULA_VERSION == "poc-1"
    assert FIXTURE["client_validated"] is False
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)), "case names must be unique"
    assert set(names) >= {
        "full_up12_centar",
        "missing_far",
        "missing_coverage",
        "no_market_data_zone_centar",
        "user_overrides",
        "area_zero_total_cost_zero",
    }
    for case in CASES:
        assert case["notes"], case["name"]
        assert case["expected"]["formula_version"] == FORMULA_VERSION


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fixture_case(case: dict[str, Any]):
    result = run_case(case)
    got, expected = result.to_dict(), case["expected"]
    assert got["formula_version"] == expected["formula_version"]
    assert got["assumptions_used"] == expected["assumptions_used"]
    for section in ("fields", "cost_rows"):
        assert [f["key"] for f in got[section]] == [f["key"] for f in expected[section]]
        for g, e in zip(got[section], expected[section], strict=True):
            assert_field_matches(g, e)
    assert_ranges_ordered(result)


# --- result shape -------------------------------------------------------------------------


def test_result_shape_and_order():
    result = compute_feasibility(**UP12, market=CENTAR, assumptions=Assumptions())
    assert result.formula_version == "poc-1"
    assert tuple(f.key for f in result.fields) == FIELD_KEYS
    assert tuple(r.key for r in result.cost_rows) == COST_ROW_KEYS
    assert len(result.fields) == 7 and len(result.cost_rows) == 4
    # The construction cost row and field are one and the same figure.
    assert result.get("construction_cost_eur") is result.cost_rows[2]
    assert result.fields[3] == result.cost_rows[2]
    with pytest.raises(KeyError):
        result.get("nope")

    as_dict = result.to_dict()
    assert set(as_dict) == {"formula_version", "fields", "cost_rows", "assumptions_used"}
    assert json.loads(json.dumps(as_dict)) == as_dict  # plain JSON-serialisable dicts
    assert set(as_dict["assumptions_used"]) == {
        "saleable_share",
        "construction_cost_eur_m2",
        "sale_price_eur_m2",
        "design_rate_eur_m2",
        "land_rate_eur_m2",
        "range_low_factor",
        "range_high_factor",
        "sources",
    }
    assert set(as_dict["assumptions_used"]["sources"]) == {
        "construction_cost_eur_m2",
        "sale_price_eur_m2",
    }
    for item in (*as_dict["fields"], *as_dict["cost_rows"]):
        assert set(item) == {
            "key",
            "status",
            "reason_code",
            "reason_params",
            "range_kind",
            "low",
            "expected",
            "high",
        }
        assert item["status"] == "ok" and item["reason_code"] is None
        assert item["reason_params"] is None
        if item["key"].endswith("_eur"):
            assert all(isinstance(item[b], int) for b in ("low", "expected", "high"))
    kinds = {f.key: f.range_kind for f in result.fields}
    assert all(kinds[k] == "deterministic" for k in AREA_FIELDS)
    assert all(kinds[k] == "range" for k in MONEY_FIELDS)
    assert all(r.range_kind == "range" for r in result.cost_rows)


def test_rounding_rules():
    # areas 1 decimal, euros whole, ROI 1 decimal; half away from zero on the decimal value
    # (1.25 -> 1.3, where binary/banker's rounding would give 1.2).
    result = compute_feasibility(1.25, 1, 50, CENTAR, Assumptions())
    assert result.get("max_gfa_m2").expected == 1.3
    assert result.get("max_coverage_area_m2").expected == 0.6  # 0.625 -> 0.6
    assert result.get("saleable_area_m2").expected == 0.9  # 0.875 -> 0.9
    land = result.get("land_value_eur")
    assert land.expected == 1688 and isinstance(land.expected, int)  # 1687.5 -> 1688
    roi = result.get("roi_pct")
    assert roi.expected == round(roi.expected, 1)


# --- dependency rules ---------------------------------------------------------------------


def reasons(result: FeasibilityResult) -> dict[str, str | None]:
    return {item.key: item.reason_code for item in (*result.fields, *result.cost_rows)}


def test_no_area_blocks_everything():
    result = compute_feasibility(None, 3.2, 55, CENTAR, Assumptions())
    for item in (*result.fields, *result.cost_rows):
        assert item.status == "cannot_calculate"
        assert item.reason_code == ReasonCode.area_unknown == "area_unknown"
        assert item.reason_params == {}
        assert (item.low, item.expected, item.high) == (None, None, None)
    # Area wins over every other missing input.
    result = compute_feasibility(None, None, None, None, Assumptions())
    assert set(reasons(result).values()) == {"area_unknown"}


def test_no_far_blocks_gfa_and_its_dependents_only():
    result = compute_feasibility(959.6, None, 55, CENTAR, Assumptions())
    got = reasons(result)
    assert got["max_gfa_m2"] == "far_not_stated"
    assert result.get("max_coverage_area_m2").status == "ok"
    assert result.get("land_value_eur").status == "ok"
    assert result.get("land_value_eur").expected == 1295460
    for key in ("saleable_area_m2", *MONEY_FIELDS, "design_documentation_eur", "total_cost_eur"):
        assert got[key] == "requires_gfa", key
        assert result.get(key).status == "cannot_calculate"


def test_no_coverage_blocks_only_the_coverage_area():
    result = compute_feasibility(959.6, 3.2, None, CENTAR, Assumptions())
    coverage = result.get("max_coverage_area_m2")
    assert coverage.status == "cannot_calculate"
    assert coverage.reason_code == "coverage_not_stated" and coverage.reason_params == {}
    others = [i for i in (*result.fields, *result.cost_rows) if i.key != "max_coverage_area_m2"]
    assert all(i.status == "ok" for i in others)
    full = compute_feasibility(959.6, 3.2, 55, CENTAR, Assumptions())
    assert [i for i in (*full.fields, *full.cost_rows) if i.key != "max_coverage_area_m2"] == others


def test_no_market_blocks_money_and_cost_rows_with_callers_reason():
    result = compute_feasibility(
        **UP12,
        market=None,
        assumptions=Assumptions(),
        market_reason_code="no_market_data",
        market_reason_params={"zone_name": "Centar"},
    )
    for key in AREA_FIELDS:
        assert result.get(key).status == "ok", key
    for key in (*MONEY_FIELDS, *COST_ROW_KEYS):
        item = result.get(key)
        assert item.status == "cannot_calculate", key
        assert item.reason_code == "no_market_data"
        assert item.reason_params == {"zone_name": "Centar"}
    used = result.assumptions_used
    assert used.saleable_share == 0.7
    assert (used.construction_cost_eur_m2, used.sale_price_eur_m2) == (None, None)
    assert (used.design_rate_eur_m2, used.land_rate_eur_m2) == (None, None)
    assert (used.range_low_factor, used.range_high_factor) == (None, None)
    assert used.sources.to_dict() == {"construction_cost_eur_m2": None, "sale_price_eur_m2": None}


def test_no_market_without_reason_defaults_to_zone_unknown():
    result = compute_feasibility(**UP12, market=None, assumptions=Assumptions())
    for key in (*MONEY_FIELDS, *COST_ROW_KEYS):
        item = result.get(key)
        assert item.reason_code == "no_market_data_zone_unknown" and item.reason_params == {}
    # The reason is ignored when a market row is present.
    ok = compute_feasibility(
        **UP12, market=CENTAR, assumptions=Assumptions(), market_reason_code="no_market_data"
    )
    assert all(i.status == "ok" for i in (*ok.fields, *ok.cost_rows))


@pytest.mark.parametrize("code", ["area_unknown", "requires_gfa", "bogus"])
def test_non_market_reason_codes_are_rejected(code: str):
    with pytest.raises(ValueError):
        compute_feasibility(**UP12, market=None, assumptions=Assumptions(), market_reason_code=code)


def test_overrides_do_not_rescue_a_missing_market_row():
    result = compute_feasibility(
        **UP12, market=None, assumptions=Assumptions(0.8, 700, 2600), market_reason_code=None
    )
    assert result.get("saleable_area_m2").expected == 2456.6
    for key in (*MONEY_FIELDS, *COST_ROW_KEYS):
        assert result.get(key).status == "cannot_calculate", key
    used = result.assumptions_used
    assert (used.saleable_share, used.construction_cost_eur_m2, used.sale_price_eur_m2) == (
        0.8,
        700,
        2600,
    )
    assert used.sources.to_dict() == {
        "construction_cost_eur_m2": "user",
        "sale_price_eur_m2": "user",
    }
    assert (used.land_rate_eur_m2, used.design_rate_eur_m2) == (None, None)


def test_effective_rates_and_sources():
    market_only = compute_feasibility(**UP12, market=CENTAR, assumptions=Assumptions())
    used = market_only.assumptions_used
    assert (used.construction_cost_eur_m2, used.sale_price_eur_m2) == (860, 2450)
    assert used.sources.to_dict() == {
        "construction_cost_eur_m2": "market",
        "sale_price_eur_m2": "market",
    }
    assert (used.design_rate_eur_m2, used.land_rate_eur_m2) == (90, 1350)
    assert (used.range_low_factor, used.range_high_factor) == (0.86, 1.15)

    mixed = compute_feasibility(
        **UP12, market=CENTAR, assumptions=Assumptions(sale_price_eur_m2=2600)
    )
    used = mixed.assumptions_used
    assert (used.construction_cost_eur_m2, used.sale_price_eur_m2) == (860, 2600)
    assert used.sources.to_dict() == {
        "construction_cost_eur_m2": "market",
        "sale_price_eur_m2": "user",
    }
    # Only revenue-side figures move; construction and the cost rows are untouched.
    assert mixed.get("construction_cost_eur") == market_only.get("construction_cost_eur")
    assert mixed.cost_rows == market_only.cost_rows
    assert mixed.get("revenue_eur").expected == 5588710  # 2149.504 x 2600 = 5588710.4
    assert mixed.get("revenue_eur").expected > market_only.get("revenue_eur").expected


def test_total_cost_zero_blocks_roi_only():
    result = compute_feasibility(0, 3.2, 55, CENTAR, Assumptions())
    roi = result.get("roi_pct")
    assert roi.status == "cannot_calculate"
    assert roi.reason_code == "total_cost_zero" and roi.reason_params == {}
    for item in (*result.fields[:-1], *result.cost_rows):
        assert item.status == "ok" and (item.low, item.expected, item.high) == (0, 0, 0), item


def test_missing_far_wins_over_missing_market_for_gfa_dependents():
    result = compute_feasibility(959.6, None, 55, None, Assumptions())
    got = reasons(result)
    assert got["max_gfa_m2"] == "far_not_stated"
    assert got["land_value_eur"] == "no_market_data_zone_unknown"
    for key in ("saleable_area_m2", *MONEY_FIELDS, "design_documentation_eur", "total_cost_eur"):
        assert got[key] == "requires_gfa", key


def test_stated_zero_far_is_a_real_zero():
    result = compute_feasibility(959.6, 0, 55, CENTAR, Assumptions())
    assert result.get("max_gfa_m2").status == "ok"
    assert result.get("max_gfa_m2").expected == 0.0
    assert result.get("saleable_area_m2").expected == 0.0
    assert result.get("construction_cost_eur").expected == 0
    # Land value alone makes the total cost positive, so ROI is computable (and negative).
    assert result.get("total_cost_eur").expected == 1295460
    assert result.get("roi_pct").status == "ok"
    assert result.get("roi_pct").expected == -100.0


# --- range ordering -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "inputs",
    [
        {**UP12, "market": CENTAR, "assumptions": Assumptions()},
        {**UP12, "market": CENTAR, "assumptions": Assumptions(0.8, 700, 2600)},
        # negative profit at every bound (sale price far below cost)
        {**UP12, "market": CENTAR, "assumptions": Assumptions(sale_price_eur_m2=100)},
        # profit negative in the expected case but positive at the high bound
        {**UP12, "market": CENTAR, "assumptions": Assumptions(sale_price_eur_m2=1900)},
        {**UP12, "market": MarketInputs(900, 780, 90, 1650), "assumptions": Assumptions(0.3)},
        {
            **UP12,
            "market": MarketInputs(900, 780, 90, 1650, 0.5, 2.0),
            "assumptions": Assumptions(1.0),
        },
        # symmetric factors of 1: every bound equals the expected value
        {
            **UP12,
            "market": MarketInputs(900, 780, 90, 1650, 1.0, 1.0),
            "assumptions": Assumptions(),
        },
        {
            "basis_area_m2": 12.3,
            "max_far": 0.5,
            "max_site_coverage_pct": 10,
            "market": CENTAR,
            "assumptions": Assumptions(),
        },
        {
            "basis_area_m2": 25000,
            "max_far": 6,
            "max_site_coverage_pct": 90,
            "market": CENTAR,
            "assumptions": Assumptions(0.95, 1200, 5000),
        },
    ],
    ids=[
        "up12-defaults",
        "up12-overrides",
        "negative-profit",
        "profit-sign-changes",
        "stari-aerodrom-min-share",
        "wide-factors-full-share",
        "unit-factors",
        "tiny-parcel",
        "large-parcel",
    ],
)
def test_ranges_are_ordered(inputs: dict[str, Any]):
    result = compute_feasibility(**inputs)
    assert all(i.status == "ok" for i in (*result.fields, *result.cost_rows))
    assert_ranges_ordered(result)
    # profit low / high are built from the opposite bounds of revenue and total cost.
    revenue, total, profit = (
        result.get("revenue_eur"),
        result.get("total_cost_eur"),
        result.get("profit_eur"),
    )
    assert abs(profit.low - (revenue.low - total.high)) <= 1
    assert abs(profit.high - (revenue.high - total.low)) <= 1
    assert abs(profit.expected - (revenue.expected - total.expected)) <= 1


def test_negative_profit_range_is_ordered_and_negative():
    result = compute_feasibility(
        **UP12, market=CENTAR, assumptions=Assumptions(sale_price_eur_m2=100)
    )
    profit, roi = result.get("profit_eur"), result.get("roi_pct")
    assert profit.high < 0 and roi.high < 0
    assert profit.low <= profit.expected <= profit.high
    assert roi.low <= roi.expected <= roi.high
