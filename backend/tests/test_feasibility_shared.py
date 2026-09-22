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
    ],
    ids=[
        "negative area",
        "coverage > 100",
        "bad basis",
        "multiplier low > 1",
        "bad kind",
        "empty cost",
        "bad market reason",
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
