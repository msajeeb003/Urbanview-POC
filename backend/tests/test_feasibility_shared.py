"""Parity of the Python engine copy with the shared fixtures (packages/feasibility-engine).

Every case must be reproduced exactly (deep equality, no tolerance) and its JSON text must be the
same bytes ``JSON.stringify`` produces in the browser, so the two engines cannot drift.
"""

from __future__ import annotations

import copy
import json

import pytest

from core.engine import shared
from core.engine.shared import (
    FIELD_DEPENDENCIES,
    FIELD_ORDER,
    FIXTURES_PATH,
    EngineInputError,
    calculate,
    expected_of,
    recalculate,
    round_half_away_from_zero,
    select_calculation_basis,
    to_json,
)

FIXTURES = shared.load_fixtures()
CASES = FIXTURES["cases"]


def run(case: dict) -> dict:
    inputs = copy.deepcopy(case["inputs"])
    if case["edits"] is None:
        return calculate(inputs)
    return recalculate(inputs, case["edits"])


def test_fixture_file_is_the_shared_one():
    assert FIXTURES_PATH.is_file()
    assert FIXTURES["formula_version"] == shared.FORMULA_VERSION
    assert FIXTURES["range_derivation"] == shared.RANGE_DERIVATION
    assert len(CASES) >= 10


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_case_matches_exactly(case: dict):
    result = expected_of(run(case))
    assert result == case["expected"]
    assert list(result["fields"]) == list(FIELD_ORDER)
    # byte-identical JSON text (the TypeScript test asserts the same string against the fixture)
    assert to_json(result) == to_json(case["expected"])


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_ranges_are_ordered(case: dict):
    for field in case["expected"]["fields"].values():
        if field["status"] == "ok":
            assert field["low"] <= field["expected"] <= field["high"]
            if field["range_kind"] == "deterministic":
                assert field["low"] == field["expected"] == field["high"]
        else:
            assert field["low"] is None and field["expected"] is None and field["high"] is None
            assert field["reason"]


def test_engine_does_not_mutate_inputs():
    case = next(c for c in CASES if c["name"] == "up12_centar_rates")
    inputs = copy.deepcopy(case["inputs"])
    recalculate(inputs, {"saleable_share": 0.8, "construction_cost_per_m2": 700})
    assert inputs == case["inputs"]


def test_changing_an_assumption_changes_only_dependent_fields():
    case = next(c for c in CASES if c["name"] == "up12_centar_rates")
    baseline = calculate(case["inputs"])["fields"]
    edits = {"saleable_share": 0.8, "construction_cost_per_m2": 700, "market_value_per_m2": 2600}
    for assumption, dependants in FIELD_DEPENDENCIES.items():
        edited = recalculate(case["inputs"], {assumption: edits[assumption]})["fields"]
        changed = [key for key in FIELD_ORDER if edited[key] != baseline[key]]
        assert changed == list(dependants)


def test_rounding_rule_matches_the_typescript_engine():
    assert round_half_away_from_zero(1.005, 2) == 1.01
    assert round_half_away_from_zero(2.675, 2) == 2.68
    assert round_half_away_from_zero(-1.005, 2) == -1.01
    assert round_half_away_from_zero(2817.4999999999995, 2) == 2817.5
    assert round_half_away_from_zero(1.5e-7, 2) == 0
    assert str(round_half_away_from_zero(-0.001, 2)) == "0.0"
    with pytest.raises(ValueError):
        round_half_away_from_zero(float("nan"), 2)


def test_json_text_is_javascript_style():
    assert to_json({"a": 1295460.0, "b": 2640819.2, "c": None, "d": [0.0, True]}) == (
        '{"a":1295460,"b":2640819.2,"c":null,"d":[0,true]}'
    )
    assert json.loads(to_json(expected_of(run(CASES[0])))) == CASES[0]["expected"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda i: i["planning"].__setitem__("plot_area", -1),
        lambda i: i["planning"].__setitem__("site_coverage_pct", 101),
        lambda i: i["planning"].__setitem__("calculation_basis", "plot"),
        lambda i: i["market"]["market_value_per_m2"]["bounds"].__setitem__("low", 1.2),
        lambda i: i["market"]["market_value_per_m2"]["bounds"].__setitem__("kind", "range"),
        lambda i: i["market"].__setitem__("land_value", {}),
        lambda i: i.__setitem__("market_missing_reason", "bogus") or i.__setitem__("market", None),
        lambda i: i["planning"].__setitem__("planned_area", -5),
        lambda i: i["planning"].__setitem__("cadastral_area", float("inf")),
    ],
    ids=[
        "negative area",
        "coverage > 100",
        "bad basis",
        "multiplier low > 1",
        "bad kind",
        "empty cost",
        "bad market reason",
        "negative planned area",
        "infinite cadastral area",
    ],
)
def test_invalid_inputs_raise(mutate):
    inputs = copy.deepcopy(next(c for c in CASES if c["name"] == "up12_centar_rates")["inputs"])
    mutate(inputs)
    with pytest.raises(EngineInputError):
        calculate(inputs)


def test_invalid_edits_raise():
    inputs = next(c for c in CASES if c["name"] == "up12_centar_rates")["inputs"]
    with pytest.raises(EngineInputError):
        recalculate(inputs, {"saleable_share": 0})
    with pytest.raises(EngineInputError):
        recalculate(inputs, {"construction_cost_per_m2": -1})


# --- both parcel areas: the calculation basis (selectCalculationBasis in TypeScript) --------------


def test_the_planned_area_is_the_basis_and_the_cadastral_area_the_fallback():
    assert select_calculation_basis(959.6, 1370.9) == {
        "plot_area": 959.6,
        "calculation_basis": "urban",
    }
    assert select_calculation_basis(0, 1370.9) == {"plot_area": 0, "calculation_basis": "urban"}
    assert select_calculation_basis(None, 1370.9) == {
        "plot_area": 1370.9,
        "calculation_basis": "cadastral",
    }
    assert select_calculation_basis(None, None) == {
        "plot_area": None,
        "calculation_basis": "cadastral",
    }
    # the same JSON text as the TypeScript object literal (key order plot_area, calculation_basis)
    assert to_json(select_calculation_basis(959.6, None)) == (
        '{"plot_area":959.6,"calculation_basis":"urban"}'
    )
    for planned, cadastral in ((-1, 10), (10, float("nan"))):
        with pytest.raises(EngineInputError):
            select_calculation_basis(planned, cadastral)


def test_context_inputs_never_change_a_figure():
    case = next(c for c in CASES if c["name"] == "up12_centar_rates")
    inputs = copy.deepcopy(case["inputs"])
    inputs["planning"].update(
        planned_area=959.6,
        cadastral_area=1370.9,
        max_height_m=None,
        max_floors="P+8",
        land_use="Residential",
    )
    assert to_json(calculate(inputs)) == to_json(calculate(case["inputs"]))


# --- the shared dependency snapshot (fixtures/assumption-dependencies.json) -----------------------

DEPENDENCIES = json.loads(
    (FIXTURES_PATH.parent / "assumption-dependencies.json").read_text(encoding="utf-8")
)


def test_dependency_snapshot_covers_every_calculate_case():
    assert DEPENDENCIES["formula_version"] == shared.FORMULA_VERSION
    assert list(DEPENDENCIES["cases"]) == [c["name"] for c in CASES if c["edits"] is None]
    assert sorted(DEPENDENCIES["edits"]) == sorted(FIELD_DEPENDENCIES)


@pytest.mark.parametrize("name", list(DEPENDENCIES["cases"]))
def test_one_edit_changes_only_the_snapshot_figures(name: str):
    case = next(c for c in CASES if c["name"] == name)
    baseline = calculate(copy.deepcopy(case["inputs"]))["fields"]
    for assumption, value in DEPENDENCIES["edits"].items():
        edited = recalculate(copy.deepcopy(case["inputs"]), {assumption: value})["fields"]
        changed = [k for k in FIELD_ORDER if to_json(edited[k]) != to_json(baseline[k])]
        assert changed == DEPENDENCIES["cases"][name][assumption], (name, assumption)
        assert set(changed) <= set(FIELD_DEPENDENCIES[assumption])
