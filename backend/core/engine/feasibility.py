"""Panel-facing adapter over the shared feasibility engine (``core.engine.shared``).

The arithmetic lives in ONE place: the shared engine, a line-for-line copy of the TypeScript
package ``packages/feasibility-engine`` that both are held to
``packages/feasibility-engine/fixtures/feasibility-cases.json`` with exact equality. This module
only translates between the panel's vocabulary (a zone's admin market row with per-m² rates and
range factors, the panel field keys ``max_gfa_m2`` … ``roi_pct`` and the four cost rows) and the
shared engine's input / output shapes. It adds no formula of its own.

``None`` inputs mean "unknown / not stated in the plan" (a stated 0 is a real 0). Reason codes and
their precedence are the shared engine's; the market reason (``no_market_data`` with
``{zone_name}`` or ``no_market_data_zone_unknown``) is supplied by the caller because the engine
does not know the zone. Rounding follows the shared rule: 2 decimals for areas, euros and ROI,
half away from zero on the shortest decimal representation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from core.engine import shared
from core.engine.shared import DEFAULT_SALEABLE_SHARE, FORMULA_VERSION, EngineInputError

__all__ = [
    "COST_ROW_KEYS",
    "DEFAULT_SALEABLE_SHARE",
    "FIELD_KEYS",
    "FORMULA_VERSION",
    "Assumptions",
    "AssumptionsUsed",
    "EngineInputError",
    "FeasibilityResult",
    "FieldRange",
    "MarketInputs",
    "RateSources",
    "ReasonCode",
    "compute_feasibility",
    "shared_inputs",
]

FIELD_KEYS: tuple[str, ...] = (
    "max_gfa_m2",
    "max_coverage_area_m2",
    "saleable_area_m2",
    "construction_cost_eur",
    "revenue_eur",
    "profit_eur",
    "roi_pct",
)
COST_ROW_KEYS: tuple[str, ...] = (
    "land_value_eur",
    "design_documentation_eur",
    "construction_cost_eur",
    "total_cost_eur",
)
# panel key -> shared engine key
SHARED_KEY: dict[str, str] = {
    "max_gfa_m2": "max_gfa",
    "max_coverage_area_m2": "max_coverage_area",
    "saleable_area_m2": "saleable_area",
    "construction_cost_eur": "construction_costs",
    "revenue_eur": "market_value",
    "profit_eur": "potential_profit",
    "roi_pct": "roi_pct",
    "land_value_eur": "land_value",
    "design_documentation_eur": "design_and_documentation_costs",
    "total_cost_eur": "total_cost",
}

Status = Literal["ok", "cannot_calculate"]
RangeKind = Literal["deterministic", "range"]
RateSource = Literal["market", "user"]


class ReasonCode(StrEnum):
    """Why a figure cannot be calculated. The API layer owns the texts, one per code."""

    area_unknown = "area_unknown"
    far_not_stated = "far_not_stated"
    coverage_not_stated = "coverage_not_stated"
    requires_gfa = "requires_gfa"
    no_market_data = "no_market_data"  # params: {zone_name}
    no_market_data_zone_unknown = "no_market_data_zone_unknown"
    total_cost_zero = "total_cost_zero"


MARKET_REASON_CODES = frozenset({ReasonCode.no_market_data, ReasonCode.no_market_data_zone_unknown})


@dataclass(frozen=True, slots=True)
class MarketInputs:
    """Current admin market inputs for the parcel's zone (``financial_assumptions`` row)."""

    land_rate_eur_m2: float
    build_rate_eur_m2: float
    design_rate_eur_m2: float
    sale_rate_eur_m2: float
    range_low_factor: float = 0.86
    range_high_factor: float = 1.15


@dataclass(frozen=True, slots=True)
class Assumptions:
    """User-editable assumptions (query overrides). ``None`` means "use the market rate"."""

    saleable_share: float = DEFAULT_SALEABLE_SHARE
    construction_cost_eur_m2: float | None = None
    sale_price_eur_m2: float | None = None


@dataclass(frozen=True, slots=True)
class FieldRange:
    """One feasibility figure as a low / expected / high range.

    ``low``/``expected``/``high`` are numbers when ``status == "ok"`` and all ``None`` otherwise;
    ``reason_code``/``reason_params`` are set only when ``status == "cannot_calculate"``.
    """

    key: str
    status: Status
    reason_code: str | None
    reason_params: dict[str, Any] | None
    range_kind: RangeKind
    low: float | None
    expected: float | None
    high: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "reason_code": self.reason_code,
            "reason_params": None if self.reason_params is None else dict(self.reason_params),
            "range_kind": self.range_kind,
            "low": self.low,
            "expected": self.expected,
            "high": self.high,
        }


@dataclass(frozen=True, slots=True)
class RateSources:
    """Where the effective construction and sale rates came from (``None`` = no rate at all)."""

    construction_cost_eur_m2: RateSource | None
    sale_price_eur_m2: RateSource | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "construction_cost_eur_m2": self.construction_cost_eur_m2,
            "sale_price_eur_m2": self.sale_price_eur_m2,
        }


@dataclass(frozen=True, slots=True)
class AssumptionsUsed:
    """The numbers the calculation actually used. Rates are ``None`` without a market row."""

    saleable_share: float
    construction_cost_eur_m2: float | None
    sale_price_eur_m2: float | None
    design_rate_eur_m2: float | None
    land_rate_eur_m2: float | None
    range_low_factor: float | None
    range_high_factor: float | None
    sources: RateSources

    def to_dict(self) -> dict[str, Any]:
        return {
            "saleable_share": self.saleable_share,
            "construction_cost_eur_m2": self.construction_cost_eur_m2,
            "sale_price_eur_m2": self.sale_price_eur_m2,
            "design_rate_eur_m2": self.design_rate_eur_m2,
            "land_rate_eur_m2": self.land_rate_eur_m2,
            "range_low_factor": self.range_low_factor,
            "range_high_factor": self.range_high_factor,
            "sources": self.sources.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class FeasibilityResult:
    """``fields`` in ``FIELD_KEYS`` order (7), ``cost_rows`` in ``COST_ROW_KEYS`` order (4)."""

    fields: tuple[FieldRange, ...]
    cost_rows: tuple[FieldRange, ...]
    assumptions_used: AssumptionsUsed
    formula_version: str = FORMULA_VERSION

    def get(self, key: str) -> FieldRange:
        """Look a figure up by key, in ``fields`` first, then ``cost_rows``."""
        for item in (*self.fields, *self.cost_rows):
            if item.key == key:
                return item
        raise KeyError(key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "formula_version": self.formula_version,
            "fields": [f.to_dict() for f in self.fields],
            "cost_rows": [row.to_dict() for row in self.cost_rows],
            "assumptions_used": self.assumptions_used.to_dict(),
        }


def shared_inputs(
    basis_area_m2: float | None,
    max_far: float | None,
    max_site_coverage_pct: float | None,
    market: MarketInputs | None,
    *,
    calculation_basis: str = "urban",
    saleable_share: float = DEFAULT_SALEABLE_SHARE,
    market_reason_code: str | None = None,
) -> dict[str, Any]:
    """The shared engine's ``EngineInputs`` for a panel calculation.

    The admin row's range factors become multiplier bounds on every market input; the land rate
    is per m² of parcel area, the build and design rates per m² of GFA.
    """
    inputs: dict[str, Any] = {
        "planning": {
            "plot_area": basis_area_m2,
            "calculation_basis": calculation_basis,
            "far": max_far,
            "site_coverage_pct": max_site_coverage_pct,
        },
        "market": None,
        "assumptions": {"saleable_share": saleable_share},
    }
    if market is None:
        if market_reason_code is not None:
            inputs["market_missing_reason"] = market_reason_code
        return inputs
    bounds = {
        "kind": "multiplier",
        "low": market.range_low_factor,
        "high": market.range_high_factor,
    }
    inputs["market"] = {
        "market_value_per_m2": {"expected": market.sale_rate_eur_m2, "bounds": bounds},
        "construction_cost": {"per_m2": {"expected": market.build_rate_eur_m2, "bounds": bounds}},
        "land_value": {"per_m2": {"expected": market.land_rate_eur_m2, "bounds": bounds}},
        "design_and_documentation_costs": {
            "per_m2": {"expected": market.design_rate_eur_m2, "bounds": bounds}
        },
    }
    return inputs


def _market_reason(
    market: MarketInputs | None,
    market_reason_code: str | None,
    market_reason_params: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any]] | None:
    if market is not None:
        return None
    if market_reason_code is None:
        return ReasonCode.no_market_data_zone_unknown.value, {}
    code = ReasonCode(market_reason_code)
    if code not in MARKET_REASON_CODES:
        raise ValueError(f"{market_reason_code!r} is not a market-data reason code")
    return code.value, dict(market_reason_params or {})


def _field(shared_field: Mapping[str, Any], key: str, market_reason: tuple | None) -> FieldRange:
    reason = shared_field["reason"]
    params: dict[str, Any] | None = None
    if reason is not None:
        params = dict(market_reason[1]) if market_reason and reason == market_reason[0] else {}
    return FieldRange(
        key=key,
        status=shared_field["status"],
        reason_code=reason,
        reason_params=params,
        range_kind=shared_field["range_kind"],
        low=shared_field["low"],
        expected=shared_field["expected"],
        high=shared_field["high"],
    )


def compute_feasibility(
    basis_area_m2: float | None,
    max_far: float | None,
    max_site_coverage_pct: float | None,
    market: MarketInputs | None,
    assumptions: Assumptions,
    *,
    market_reason_code: str | None = None,
    market_reason_params: Mapping[str, Any] | None = None,
    calculation_basis: str = "urban",
) -> FeasibilityResult:
    """Run the shared ``poc-1`` formulas for one parcel and return the panel's result shape.

    A user override of the construction cost or the sale price replaces the market rate (the
    range factors still apply to it); overrides never rescue a missing market row. Any market
    reason code other than the two market codes raises ``ValueError``.
    """
    market_reason = _market_reason(market, market_reason_code, market_reason_params)
    inputs = shared_inputs(
        basis_area_m2,
        max_far,
        max_site_coverage_pct,
        market,
        calculation_basis=calculation_basis,
        saleable_share=assumptions.saleable_share,
        market_reason_code=market_reason[0] if market_reason else None,
    )
    edits: dict[str, Any] = {}
    if assumptions.construction_cost_eur_m2 is not None:
        edits["construction_cost_per_m2"] = assumptions.construction_cost_eur_m2
    if assumptions.sale_price_eur_m2 is not None:
        edits["market_value_per_m2"] = assumptions.sale_price_eur_m2
    result = shared.recalculate(inputs, edits)
    fields = result["fields"]
    used = result["assumptions_used"]

    construction = used["construction_cost"]
    price = used["market_value_per_m2"]
    assumptions_used = AssumptionsUsed(
        saleable_share=assumptions.saleable_share,
        construction_cost_eur_m2=construction["expected"] if construction else None,
        sale_price_eur_m2=price["expected"] if price else None,
        design_rate_eur_m2=market.design_rate_eur_m2 if market else None,
        land_rate_eur_m2=market.land_rate_eur_m2 if market else None,
        range_low_factor=market.range_low_factor if market else None,
        range_high_factor=market.range_high_factor if market else None,
        sources=RateSources(
            construction_cost_eur_m2=construction["source"] if construction else None,
            sale_price_eur_m2=price["source"] if price else None,
        ),
    )
    return FeasibilityResult(
        fields=tuple(_field(fields[SHARED_KEY[key]], key, market_reason) for key in FIELD_KEYS),
        cost_rows=tuple(
            _field(fields[SHARED_KEY[key]], key, market_reason) for key in COST_ROW_KEYS
        ),
        assumptions_used=assumptions_used,
        formula_version=result["formula_version"],
    )
