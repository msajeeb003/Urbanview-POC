"""Python copy of the shared feasibility engine (``packages/feasibility-engine``).

Same formulas, same operation order, same rounding and the same JSON shapes as the TypeScript
package, so both are held to ``packages/feasibility-engine/fixtures/feasibility-cases.json`` with
exact equality (``tests/test_feasibility_shared.py``). Inputs and results are plain JSON-compatible
dicts (the fixture format) so the panel service and the tests need no conversion layer.

Formulas (client-owned, ``FORMULA_VERSION``), evaluated in exactly this order::

    max_gfa                        = far * plot_area
    max_coverage_area              = plot_area * (site_coverage_pct / 100)
    saleable_area                  = max_gfa * saleable_share          (0.70 by default)
    construction_costs             = max_gfa * construction_cost_per_m2 | construction total
    land_value                     = plot_area * land_value_per_m2     | land total
    design_and_documentation_costs = max_gfa * design_per_m2           | design total
    total_cost                     = land_value + design_and_documentation_costs
                                     + construction_costs
    market_value (revenue)         = saleable_area * market_value_per_m2
    potential_profit               = market_value - total_cost
    roi_pct                        = potential_profit / total_cost * 100

Range derivation (``RANGE_DERIVATION``): each money input carries admin-supplied bounds (multiplier
or absolute). Cost rows take their own low / high bound (sums for the total), revenue the price
bounds, profit low = revenue low - total cost high (high symmetrically) and ROI low = profit low /
total cost high (ROI = revenue / cost - 1 is monotone, so those are its true extremes). The three
areas depend on no market input and are ``deterministic`` (low = expected = high).

Missing inputs (``None``) never produce a made-up number: the affected figures are
``cannot_calculate`` with a reason code, in this precedence: no plot area -> everything
``area_unknown``; FAR not stated -> max_gfa ``far_not_stated`` and everything derived from GFA
``requires_gfa``; coverage not stated -> only max_coverage_area ``coverage_not_stated``; no
market row -> the money figures carry the market reason (the land value needs no FAR, so it
reports the market reason even when FAR is missing); total cost 0 -> roi_pct
``total_cost_zero``. Invalid inputs raise ``EngineInputError``.

Rounding: outputs and displayed assumption bounds are rounded to 2 decimals, half away from zero
on the shortest decimal representation (``repr``), never intermediate values; -0 becomes 0.
"""

from __future__ import annotations

import json
import math
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

FORMULA_VERSION = "poc-1"
ENGINE_VERSION = "1.0.0"
RANGE_DERIVATION = "pessimistic-pairing-v1"
DEFAULT_SALEABLE_SHARE = 0.7

FIXTURES_PATH = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "feasibility-engine"
    / "fixtures"
    / "feasibility-cases.json"
)

FieldKey = Literal[
    "max_gfa",
    "max_coverage_area",
    "saleable_area",
    "construction_costs",
    "land_value",
    "design_and_documentation_costs",
    "total_cost",
    "market_value",
    "potential_profit",
    "roi_pct",
]
FIELD_ORDER: tuple[str, ...] = (
    "max_gfa",
    "max_coverage_area",
    "saleable_area",
    "construction_costs",
    "land_value",
    "design_and_documentation_costs",
    "total_cost",
    "market_value",
    "potential_profit",
    "roi_pct",
)
FIELD_UNITS: dict[str, str] = {
    "max_gfa": "m2",
    "max_coverage_area": "m2",
    "saleable_area": "m2",
    "construction_costs": "EUR",
    "land_value": "EUR",
    "design_and_documentation_costs": "EUR",
    "total_cost": "EUR",
    "market_value": "EUR",
    "potential_profit": "EUR",
    "roi_pct": "%",
}
DETERMINISTIC = frozenset({"max_gfa", "max_coverage_area", "saleable_area"})
MONEY_FIELDS: tuple[str, ...] = (
    "construction_costs",
    "design_and_documentation_costs",
    "total_cost",
    "market_value",
    "potential_profit",
    "roi_pct",
)
FIELD_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "saleable_share": ("saleable_area", "market_value", "potential_profit", "roi_pct"),
    "construction_cost_per_m2": ("construction_costs", "total_cost", "potential_profit", "roi_pct"),
    "market_value_per_m2": ("market_value", "potential_profit", "roi_pct"),
}
DECIMALS = {"m2": 2, "EUR": 2, "%": 2}
MARKET_REASONS = frozenset({"no_market_data", "no_market_data_zone_unknown"})

JsonDict = dict[str, Any]


class EngineInputError(ValueError):
    """Invalid (not missing) input: a caller bug, never a data gap."""


# ---------------------------------------------------------------------------------------------
# rounding


def round_half_away_from_zero(value: float, decimals: int) -> float:
    """Half away from zero on the shortest decimal representation; -0 becomes 0."""
    if not math.isfinite(value):
        raise ValueError(f"cannot round a non-finite number: {value!r}")
    quantum = Decimal(1).scaleb(-decimals)
    rounded = Decimal(repr(float(value))).quantize(quantum, rounding=ROUND_HALF_UP)
    return float(rounded) + 0.0


def _round(value: float, unit: str) -> float:
    return round_half_away_from_zero(value, DECIMALS[unit])


# ---------------------------------------------------------------------------------------------
# validation


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EngineInputError(message)


def _assert_finite(value: Any, label: str) -> None:
    _require(
        isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value),
        f"{label} must be a finite number",
    )


def _assert_non_negative(value: Any, label: str) -> None:
    _assert_finite(value, label)
    _require(value >= 0, f"{label} must not be negative")


def _validate_ranged(ranged: JsonDict, label: str) -> None:
    _assert_non_negative(ranged.get("expected"), f"{label}.expected")
    bounds = ranged.get("bounds") or {}
    _assert_finite(bounds.get("low"), f"{label}.bounds.low")
    _assert_finite(bounds.get("high"), f"{label}.bounds.high")
    kind = bounds.get("kind")
    if kind == "multiplier":
        _require(
            0 <= bounds["low"] <= 1, f"{label}.bounds.low must be within [0, 1] for a multiplier"
        )
        _require(bounds["high"] >= 1, f"{label}.bounds.high must be at least 1 for a multiplier")
    else:
        _require(kind == "absolute", f'{label}.bounds.kind must be "multiplier" or "absolute"')
        _require(
            bounds["low"] <= ranged["expected"] <= bounds["high"],
            f"{label} absolute bounds must satisfy low <= expected <= high",
        )


def _validate_cost(cost: JsonDict, label: str) -> None:
    if cost.get("per_m2") is not None:
        _validate_ranged(cost["per_m2"], f"{label}.per_m2")
    else:
        _require(cost.get("total") is not None, f"{label} needs per_m2 or total")
        _validate_ranged(cost["total"], f"{label}.total")


def _validate(inputs: JsonDict, edits: JsonDict) -> None:
    planning = inputs["planning"]
    market = inputs.get("market")
    _require(
        planning.get("calculation_basis") in ("urban", "cadastral"),
        'planning.calculation_basis must be "urban" or "cadastral"',
    )
    if planning.get("plot_area") is not None:
        _assert_non_negative(planning["plot_area"], "planning.plot_area")
    if planning.get("far") is not None:
        _assert_non_negative(planning["far"], "planning.far")
    if planning.get("site_coverage_pct") is not None:
        _assert_non_negative(planning["site_coverage_pct"], "planning.site_coverage_pct")
        _require(
            planning["site_coverage_pct"] <= 100, "planning.site_coverage_pct must be at most 100"
        )
    if market is not None:
        _validate_ranged(market["market_value_per_m2"], "market.market_value_per_m2")
        _validate_cost(market["construction_cost"], "market.construction_cost")
        _validate_cost(market["land_value"], "market.land_value")
        _validate_cost(
            market["design_and_documentation_costs"], "market.design_and_documentation_costs"
        )
    share = _resolve_saleable_share(inputs, edits)["value"]
    _assert_finite(share, "saleable_share")
    _require(0 < share <= 1, "saleable_share must be within (0, 1]")
    for key in ("construction_cost_per_m2", "market_value_per_m2"):
        if edits.get(key) is not None:
            _assert_non_negative(edits[key], f"edits.{key}")


# ---------------------------------------------------------------------------------------------
# range derivation helpers


def bounds_of(ranged: JsonDict) -> tuple[float, float, float]:
    """(low, expected, high) of one market input from its bounds, unrounded."""
    expected = ranged["expected"]
    bounds = ranged["bounds"]
    if bounds["kind"] == "multiplier":
        return expected * bounds["low"], expected, expected * bounds["high"]
    return bounds["low"], expected, bounds["high"]


def with_edited_expected(original: JsonDict | None, edited: float) -> JsonDict:
    """A user edit replaces the expected value: multiplier bounds simply apply to the new value;
    absolute bounds keep their proportion to the original expected value."""
    if original is None or (original["bounds"]["kind"] == "absolute" and original["expected"] == 0):
        return {"expected": edited, "bounds": {"kind": "absolute", "low": edited, "high": edited}}
    if original["bounds"]["kind"] == "multiplier":
        return {"expected": edited, "bounds": original["bounds"]}
    low, high = original["bounds"]["low"], original["bounds"]["high"]
    return {
        "expected": edited,
        "bounds": {
            "kind": "absolute",
            "low": low * edited / original["expected"],
            "high": high * edited / original["expected"],
        },
    }


def _resolve_saleable_share(inputs: JsonDict, edits: JsonDict) -> JsonDict:
    if "saleable_share" in edits:
        return {"value": edits["saleable_share"], "source": "user"}
    defaults = inputs.get("assumptions") or {}
    return {"value": defaults.get("saleable_share", DEFAULT_SALEABLE_SHARE), "source": "default"}


def _resolve_market(inputs: JsonDict, edits: JsonDict) -> JsonDict:
    market = inputs.get("market")
    defaults = inputs.get("assumptions") or {}
    resolved: JsonDict = {
        "price": market["market_value_per_m2"] if market else None,
        "price_source": "market" if market else None,
        "construction": market["construction_cost"] if market else None,
        "construction_source": "market" if market else None,
        "land": market["land_value"] if market else None,
        "design": market["design_and_documentation_costs"] if market else None,
    }
    edited_price = edits.get("market_value_per_m2")
    if edited_price is None:
        edited_price = defaults.get("market_value_per_m2")
    if edited_price is not None:
        resolved["price"] = with_edited_expected(resolved["price"], edited_price)
        resolved["price_source"] = "user"
    edited_construction = edits.get("construction_cost_per_m2")
    if edited_construction is None:
        edited_construction = defaults.get("construction_cost_per_m2")
    if edited_construction is not None:
        base = resolved["construction"].get("per_m2") if resolved["construction"] else None
        resolved["construction"] = {"per_m2": with_edited_expected(base, edited_construction)}
        resolved["construction_source"] = "user"
    return resolved


# ---------------------------------------------------------------------------------------------
# output helpers


def _field(key: str, status: str, reason: str | None, low, expected, high) -> JsonDict:
    return {
        "key": key,
        "unit": FIELD_UNITS[key],
        "range_kind": "deterministic" if key in DETERMINISTIC else "range",
        "status": status,
        "reason": reason,
        "low": low,
        "expected": expected,
        "high": high,
    }


def _ok(key: str, triplet: tuple[float, float, float]) -> JsonDict:
    unit = FIELD_UNITS[key]
    low, expected, high = triplet
    return _field(key, "ok", None, _round(low, unit), _round(expected, unit), _round(high, unit))


def _cannot(key: str, reason: str) -> JsonDict:
    return _field(key, "cannot_calculate", reason, None, None, None)


def _flat(value: float) -> tuple[float, float, float]:
    return value, value, value


def _cost_triplet(cost: JsonDict, base_area: float) -> tuple[float, float, float]:
    if cost.get("per_m2") is not None:
        low, expected, high = bounds_of(cost["per_m2"])
        return base_area * low, base_area * expected, base_area * high
    return bounds_of(cost["total"])


def _used_value(triplet: tuple[float, float, float], source: str) -> JsonDict:
    low, expected, high = triplet
    return {
        "low": _round(low, "EUR"),
        "expected": _round(expected, "EUR"),
        "high": _round(high, "EUR"),
        "source": source,
    }


def _used_cost(cost: JsonDict | None, source: str | None, per_m2_basis: str) -> JsonDict | None:
    if cost is None or source is None:
        return None
    if cost.get("per_m2") is not None:
        return {**_used_value(bounds_of(cost["per_m2"]), source), "basis": per_m2_basis}
    return {**_used_value(bounds_of(cost["total"]), source), "basis": "total"}


# ---------------------------------------------------------------------------------------------
# the engine


def _compute(inputs: JsonDict, edits: JsonDict) -> JsonDict:
    _validate(inputs, edits)
    planning = inputs["planning"]
    saleable_share = _resolve_saleable_share(inputs, edits)
    market = _resolve_market(inputs, edits)
    market_reason = None
    if inputs.get("market") is None:
        market_reason = inputs.get("market_missing_reason") or "no_market_data_zone_unknown"
        if market_reason not in MARKET_REASONS:
            raise EngineInputError(f"{market_reason!r} is not a market-data reason code")

    plot_area = planning.get("plot_area")
    area_reason = "area_unknown" if plot_area is None else None
    far_missing = planning.get("far") is None
    gfa_reason = area_reason or ("far_not_stated" if far_missing else None)
    needs_gfa = area_reason or ("requires_gfa" if far_missing else None)
    coverage_reason = area_reason or (
        "coverage_not_stated" if planning.get("site_coverage_pct") is None else None
    )
    land_reason = area_reason or market_reason
    money_reason = needs_gfa or market_reason

    area = plot_area if plot_area is not None else 0.0
    gfa = 0.0 if gfa_reason else planning["far"] * area
    coverage_area = 0.0 if coverage_reason else area * (planning["site_coverage_pct"] / 100)
    saleable = 0.0 if needs_gfa else gfa * saleable_share["value"]

    fields: JsonDict = {}
    fields["max_gfa"] = _cannot("max_gfa", gfa_reason) if gfa_reason else _ok("max_gfa", _flat(gfa))
    fields["max_coverage_area"] = (
        _cannot("max_coverage_area", coverage_reason)
        if coverage_reason
        else _ok("max_coverage_area", _flat(coverage_area))
    )
    fields["saleable_area"] = (
        _cannot("saleable_area", needs_gfa) if needs_gfa else _ok("saleable_area", _flat(saleable))
    )
    fields["land_value"] = (
        _cannot("land_value", land_reason)
        if land_reason
        else _ok("land_value", _cost_triplet(market["land"], area))
    )
    if money_reason:
        for key in MONEY_FIELDS:
            fields[key] = _cannot(key, money_reason)
    else:
        construction = _cost_triplet(market["construction"], gfa)
        design = _cost_triplet(market["design"], gfa)
        land = _cost_triplet(market["land"], area)
        total = (
            land[0] + design[0] + construction[0],
            land[1] + design[1] + construction[1],
            land[2] + design[2] + construction[2],
        )
        price = bounds_of(market["price"])
        revenue = (saleable * price[0], saleable * price[1], saleable * price[2])
        profit = (revenue[0] - total[2], revenue[1] - total[1], revenue[2] - total[0])
        fields["construction_costs"] = _ok("construction_costs", construction)
        fields["design_and_documentation_costs"] = _ok("design_and_documentation_costs", design)
        fields["total_cost"] = _ok("total_cost", total)
        fields["market_value"] = _ok("market_value", revenue)
        fields["potential_profit"] = _ok("potential_profit", profit)
        if total[1] == 0 or total[0] == 0 or total[2] == 0:
            fields["roi_pct"] = _cannot("roi_pct", "total_cost_zero")
        else:
            fields["roi_pct"] = _ok(
                "roi_pct",
                (
                    profit[0] / total[2] * 100,
                    profit[1] / total[1] * 100,
                    profit[2] / total[0] * 100,
                ),
            )

    assumptions_used: JsonDict = {
        "calculation_basis": planning["calculation_basis"],
        "plot_area": plot_area,
        "saleable_share": saleable_share,
        "market_value_per_m2": (
            _used_value(bounds_of(market["price"]), market["price_source"])
            if market["price"]
            else None
        ),
        "construction_cost": _used_cost(
            market["construction"], market["construction_source"], "per_m2_gfa"
        ),
        "land_value": _used_cost(
            market["land"], "market" if market["land"] else None, "per_m2_plot"
        ),
        "design_and_documentation_costs": _used_cost(
            market["design"], "market" if market["design"] else None, "per_m2_gfa"
        ),
        "market_missing_reason": market_reason,
    }
    return {
        "engine_version": ENGINE_VERSION,
        "formula_version": FORMULA_VERSION,
        "range_derivation": RANGE_DERIVATION,
        "calculation_basis": planning["calculation_basis"],
        "fields": {key: fields[key] for key in FIELD_ORDER},
        "assumptions_used": assumptions_used,
    }


def calculate(inputs: JsonDict) -> JsonDict:
    """Run the formulas with the defaults (saleable share 0.70, market rates)."""
    return _compute(inputs, {})


def recalculate(inputs: JsonDict, edited_assumptions: JsonDict | None) -> JsonDict:
    """Merge the visitor's edits (construction cost per m², selling price per m², saleable share)
    over the defaults and recompute. Missing keys are ignored; ``None`` means "back to market"."""
    edits: JsonDict = {}
    for key in ("saleable_share", "construction_cost_per_m2", "market_value_per_m2"):
        if edited_assumptions is not None and key in edited_assumptions:
            edits[key] = edited_assumptions[key]
    return _compute(inputs, edits)


def expected_of(result: JsonDict) -> JsonDict:
    """The fixture view of a result: everything except the engine (package) version."""
    return {key: value for key, value in result.items() if key != "engine_version"}


def _json_default(value: Any) -> Any:
    raise TypeError(f"not JSON serialisable: {value!r}")


def _normalise_numbers(value: Any) -> Any:
    """Integral floats become ints so the text matches ``JSON.stringify`` byte for byte."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _normalise_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise_numbers(item) for item in value]
    return value


def to_json(value: JsonDict) -> str:
    """Compact JSON identical to ``JSON.stringify`` in JavaScript for engine results."""
    return json.dumps(
        _normalise_numbers(value), separators=(",", ":"), ensure_ascii=False, default=_json_default
    )


def load_fixtures(path: Path = FIXTURES_PATH) -> JsonDict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)
