"""The panel-facing adapter (core.engine.feasibility) over the shared engine.

The arithmetic itself is pinned by ``tests/test_feasibility_shared.py`` against the shared fixtures;
these tests check the translation: panel keys, cost rows, reason parameters, the assumptions block
and that every number equals the shared engine's for the same inputs.
"""

from __future__ import annotations

import pytest

from core.engine import (
    COST_ROW_KEYS,
    DEFAULT_SALEABLE_SHARE,
    FIELD_KEYS,
    Assumptions,
    MarketInputs,
    compute_feasibility,
    shared,
)
from core.engine.feasibility import SHARED_KEY, EngineInputError, shared_inputs

CENTAR = MarketInputs(
    land_rate_eur_m2=1350,
    build_rate_eur_m2=860,
    design_rate_eur_m2=90,
    sale_rate_eur_m2=2450,
    range_low_factor=0.86,
    range_high_factor=1.15,
)
UP12 = (959.6, 3.2, 55)


def test_maps_every_panel_key_onto_the_shared_engine():
    result = compute_feasibility(*UP12, CENTAR, Assumptions())
    expected = shared.calculate(shared_inputs(*UP12, CENTAR))["fields"]
    assert [f.key for f in result.fields] == list(FIELD_KEYS)
    assert [r.key for r in result.cost_rows] == list(COST_ROW_KEYS)
    for item in (*result.fields, *result.cost_rows):
        source = expected[SHARED_KEY[item.key]]
        assert (item.status, item.reason_code, item.range_kind) == (
            source["status"],
            source["reason"],
            source["range_kind"],
        )
        assert (item.low, item.expected, item.high) == (
            source["low"],
            source["expected"],
            source["high"],
        )
    assert result.get("max_gfa_m2").expected == 3070.72
    assert result.get("revenue_eur").expected == 5266284.8
    assert result.get("roi_pct").low == pytest.approx(-6.51)
    assert result.formula_version == shared.FORMULA_VERSION


def test_assumptions_block_reports_rates_factors_and_sources():
    used = compute_feasibility(*UP12, CENTAR, Assumptions()).assumptions_used
    assert used.saleable_share == DEFAULT_SALEABLE_SHARE
    assert (used.construction_cost_eur_m2, used.sale_price_eur_m2) == (860, 2450)
    assert (used.design_rate_eur_m2, used.land_rate_eur_m2) == (90, 1350)
    assert (used.range_low_factor, used.range_high_factor) == (0.86, 1.15)
    assert used.sources.to_dict() == {
        "construction_cost_eur_m2": "market",
        "sale_price_eur_m2": "market",
    }

    overridden = compute_feasibility(
        *UP12,
        CENTAR,
        Assumptions(saleable_share=0.8, construction_cost_eur_m2=700, sale_price_eur_m2=2600),
    )
    assert overridden.assumptions_used.saleable_share == 0.8
    assert overridden.assumptions_used.construction_cost_eur_m2 == 700
    assert overridden.assumptions_used.sale_price_eur_m2 == 2600
    assert overridden.assumptions_used.sources.to_dict() == {
        "construction_cost_eur_m2": "user",
        "sale_price_eur_m2": "user",
    }
    assert overridden.get("saleable_area_m2").expected == 2456.58
    assert overridden.get("construction_cost_eur").expected == 2149504


def test_market_reason_carries_the_zone_name_only_on_market_figures():
    result = compute_feasibility(
        959.6,
        None,
        55,
        None,
        Assumptions(),
        market_reason_code="no_market_data",
        market_reason_params={"zone_name": "Centar"},
    )
    assert result.get("max_gfa_m2").reason_code == "far_not_stated"
    assert result.get("max_gfa_m2").reason_params == {}
    assert result.get("construction_cost_eur").reason_code == "requires_gfa"
    land = result.get("land_value_eur")
    assert (land.reason_code, land.reason_params) == ("no_market_data", {"zone_name": "Centar"})
    assert result.get("max_coverage_area_m2").status == "ok"


def test_missing_market_without_a_code_means_zone_unknown_and_overrides_do_not_rescue():
    result = compute_feasibility(
        *UP12, None, Assumptions(construction_cost_eur_m2=700, sale_price_eur_m2=2600)
    )
    assert result.get("revenue_eur").reason_code == "no_market_data_zone_unknown"
    assert result.get("revenue_eur").reason_params == {}
    used = result.assumptions_used
    assert (used.construction_cost_eur_m2, used.sale_price_eur_m2) == (700, 2600)
    assert used.sources.to_dict() == {
        "construction_cost_eur_m2": "user",
        "sale_price_eur_m2": "user",
    }
    assert (used.land_rate_eur_m2, used.range_low_factor) == (None, None)


def test_non_market_reason_code_is_rejected():
    with pytest.raises(ValueError):
        compute_feasibility(*UP12, None, Assumptions(), market_reason_code="far_not_stated")


def test_invalid_inputs_raise_engine_input_error():
    with pytest.raises(EngineInputError):
        compute_feasibility(-1, 3.2, 55, CENTAR, Assumptions())
    with pytest.raises(EngineInputError):
        compute_feasibility(*UP12, CENTAR, Assumptions(saleable_share=0))


def test_to_dict_is_json_friendly():
    payload = compute_feasibility(*UP12, CENTAR, Assumptions()).to_dict()
    assert set(payload) == {"formula_version", "fields", "cost_rows", "assumptions_used"}
    assert payload["fields"][0]["key"] == "max_gfa_m2"
    assert payload["cost_rows"][-1]["key"] == "total_cost_eur"
    assert payload["assumptions_used"]["sources"]["sale_price_eur_m2"] == "market"
