"""Feasibility engine: the client's deterministic formulas, formula version ``poc-1``.

Pure functions and frozen dataclasses. No I/O, no database, no municipality knowledge and no
label text: the engine takes numbers in and gives numbers out, and when a figure cannot be
computed it emits a *reason code* plus parameters; the API layer (``api/services/panel_text.py``)
turns codes into bilingual text. The shared TypeScript engine in ``packages/formula-engine`` must
pass the same fixtures (``packages/formula-engine/fixtures/feasibility.json``, parity test
``tests/test_feasibility.py``). AI never touches this arithmetic.

Formulas (CLAUDE.md, client-owned; ``docs/specs/panel-payload.md`` section 4):

- max GFA = FAR x basis area
- max coverage area = site coverage % / 100 x basis area
- saleable area = GFA x saleable share (0.70 by default, a visible user-editable assumption)
- land value = basis area x land rate; design & documentation = GFA x design rate;
  construction cost = GFA x build rate; total cost = land + design + construction
- revenue (market value) = saleable area x sale rate
- profit = revenue - total cost; ROI % = profit / total cost x 100

Ranges: every rate-based figure is a ``range``: low = expected x ``range_low_factor``,
high = expected x ``range_high_factor`` (factors from the market row). Profit and ROI combine the
bounds pessimistically: profit low = revenue low - total cost high, profit high = revenue high -
total cost low, ROI low = profit low / total cost high, ROI high = profit high / total cost low.
The three area figures are ``deterministic`` (low = expected = high). Every range satisfies
low <= expected <= high: this follows from low factor <= 1 <= high factor (enforced by the
``financial_assumptions`` CHECK constraint) with non-negative areas and rates, and rounding is
monotonic so it never breaks the order.

Effective rates: a user override replaces the market rate for construction cost and sale price
(``assumptions_used.sources`` says which one was used). Overrides never rescue a missing market
row: without one the land and design rates are unknown, so the money figures stay
``cannot_calculate``.

Rounding is applied to outputs only (full precision inside): areas 1 decimal, euros whole
(returned as ``int``), ROI 1 decimal; half away from zero on the shortest decimal representation
of the float, so hand arithmetic on the decimal inputs reproduces the figures.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, Literal

FORMULA_VERSION = "poc-1"
DEFAULT_SALEABLE_SHARE = 0.70

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

AREA_DECIMALS = 1
EURO_DECIMALS = 0
PCT_DECIMALS = 1

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
            "reason_code": None if self.reason_code is None else str(self.reason_code),
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


_Reason = tuple[ReasonCode, dict[str, Any]]


def _round(value: float, ndigits: int) -> float | int:
    """Half away from zero on the shortest decimal representation; whole euros become ``int``."""
    quantum = Decimal(1).scaleb(-ndigits)
    rounded = Decimal(repr(float(value))).quantize(quantum, rounding=ROUND_HALF_UP)
    if ndigits == 0:
        return int(rounded)
    return float(rounded) + 0.0  # normalises -0.0 to 0.0


def _ok(
    key: str, kind: RangeKind, low: float, expected: float, high: float, ndigits: int
) -> FieldRange:
    return FieldRange(
        key=key,
        status="ok",
        reason_code=None,
        reason_params=None,
        range_kind=kind,
        low=_round(low, ndigits),
        expected=_round(expected, ndigits),
        high=_round(high, ndigits),
    )


def _cannot(key: str, kind: RangeKind, reason: _Reason) -> FieldRange:
    code, params = reason
    return FieldRange(
        key=key,
        status="cannot_calculate",
        reason_code=code.value,
        reason_params=dict(params),
        range_kind=kind,
        low=None,
        expected=None,
        high=None,
    )


def _area_field(key: str, value: float, reason: _Reason | None) -> FieldRange:
    if reason is not None:
        return _cannot(key, "deterministic", reason)
    return _ok(key, "deterministic", value, value, value, AREA_DECIMALS)


def _euro_range(key: str, expected: float, lo: float, hi: float) -> FieldRange:
    return _ok(key, "range", expected * lo, expected, expected * hi, EURO_DECIMALS)


def _effective_rate(
    override: float | None, market_rate: float | None
) -> tuple[float | None, RateSource | None]:
    if override is not None:
        return override, "user"
    if market_rate is not None:
        return market_rate, "market"
    return None, None


def _market_reason(
    market: MarketInputs | None,
    market_reason_code: str | None,
    market_reason_params: Mapping[str, Any] | None,
) -> _Reason | None:
    if market is not None:
        return None
    if market_reason_code is None:
        return ReasonCode.no_market_data_zone_unknown, {}
    code = ReasonCode(market_reason_code)
    if code not in MARKET_REASON_CODES:
        raise ValueError(f"{market_reason_code!r} is not a market-data reason code")
    return code, dict(market_reason_params or {})


def compute_feasibility(
    basis_area_m2: float | None,
    max_far: float | None,
    max_site_coverage_pct: float | None,
    market: MarketInputs | None,
    assumptions: Assumptions,
    *,
    market_reason_code: str | None = None,
    market_reason_params: Mapping[str, Any] | None = None,
) -> FeasibilityResult:
    """Run the ``poc-1`` formulas on one parcel.

    ``None`` inputs mean "unknown / not stated in the plan" (a stated 0 is a real 0). Dependency
    rules, in precedence order: no area -> every figure ``area_unknown``; no FAR -> GFA
    ``far_not_stated`` and everything derived from it ``requires_gfa``; no coverage -> only the
    coverage area ``coverage_not_stated``; no market row -> the money figures and all cost rows
    carry the market reason (``no_market_data`` with ``{zone_name}``, or
    ``no_market_data_zone_unknown``, the default when no code is given); total cost 0 -> ROI
    ``total_cost_zero``. The engine cannot know the zone, so the caller supplies the market
    reason; any code other than the two market codes raises ``ValueError``.
    """
    market_reason = _market_reason(market, market_reason_code, market_reason_params)
    build_rate, build_source = _effective_rate(
        assumptions.construction_cost_eur_m2, market.build_rate_eur_m2 if market else None
    )
    sale_rate, sale_source = _effective_rate(
        assumptions.sale_price_eur_m2, market.sale_rate_eur_m2 if market else None
    )
    assumptions_used = AssumptionsUsed(
        saleable_share=assumptions.saleable_share,
        construction_cost_eur_m2=build_rate,
        sale_price_eur_m2=sale_rate,
        design_rate_eur_m2=market.design_rate_eur_m2 if market else None,
        land_rate_eur_m2=market.land_rate_eur_m2 if market else None,
        range_low_factor=market.range_low_factor if market else None,
        range_high_factor=market.range_high_factor if market else None,
        sources=RateSources(construction_cost_eur_m2=build_source, sale_price_eur_m2=sale_source),
    )

    # Blocking reasons, most fundamental first; the first that applies to a figure wins.
    area_reason: _Reason | None = (ReasonCode.area_unknown, {}) if basis_area_m2 is None else None
    far_missing = max_far is None
    gfa_reason = area_reason or ((ReasonCode.far_not_stated, {}) if far_missing else None)
    needs_gfa = area_reason or ((ReasonCode.requires_gfa, {}) if far_missing else None)
    coverage_reason = area_reason or (
        (ReasonCode.coverage_not_stated, {}) if max_site_coverage_pct is None else None
    )
    land_reason = area_reason or market_reason
    money_reason = needs_gfa or market_reason

    area = basis_area_m2 or 0.0
    gfa = 0.0 if gfa_reason else (max_far or 0.0) * area
    coverage_area = 0.0 if coverage_reason else (max_site_coverage_pct or 0.0) / 100 * area
    saleable = 0.0 if needs_gfa else gfa * assumptions.saleable_share

    fields = [
        _area_field("max_gfa_m2", gfa, gfa_reason),
        _area_field("max_coverage_area_m2", coverage_area, coverage_reason),
        _area_field("saleable_area_m2", saleable, needs_gfa),
    ]

    # A missing market row sets market_reason, so past these checks ``market`` is present and
    # the effective build/sale rates are resolved.
    if land_reason is not None:  # land value needs the area and the market row, not the FAR
        land_row = _cannot("land_value_eur", "range", land_reason)
    else:
        land_row = _euro_range(
            "land_value_eur",
            area * market.land_rate_eur_m2,
            market.range_low_factor,
            market.range_high_factor,
        )

    if money_reason is not None:
        construction_row = _cannot("construction_cost_eur", "range", money_reason)
        fields += [
            construction_row,
            _cannot("revenue_eur", "range", money_reason),
            _cannot("profit_eur", "range", money_reason),
            _cannot("roi_pct", "range", money_reason),
        ]
        cost_rows = (
            land_row,
            _cannot("design_documentation_eur", "range", money_reason),
            construction_row,
            _cannot("total_cost_eur", "range", money_reason),
        )
        return FeasibilityResult(
            fields=tuple(fields), cost_rows=cost_rows, assumptions_used=assumptions_used
        )

    lo, hi = market.range_low_factor, market.range_high_factor
    land = area * market.land_rate_eur_m2
    design = gfa * market.design_rate_eur_m2
    construction = gfa * build_rate
    total = land + design + construction
    total_low, total_high = total * lo, total * hi
    revenue = saleable * sale_rate
    profit = revenue - total
    profit_low = revenue * lo - total_high
    profit_high = revenue * hi - total_low

    construction_row = _euro_range("construction_cost_eur", construction, lo, hi)
    fields += [
        construction_row,
        _euro_range("revenue_eur", revenue, lo, hi),
        _ok("profit_eur", "range", profit_low, profit, profit_high, EURO_DECIMALS),
    ]
    if total == 0 or total_low == 0 or total_high == 0:
        fields.append(_cannot("roi_pct", "range", (ReasonCode.total_cost_zero, {})))
    else:
        fields.append(
            _ok(
                "roi_pct",
                "range",
                profit_low / total_high * 100,
                profit / total * 100,
                profit_high / total_low * 100,
                PCT_DECIMALS,
            )
        )

    cost_rows = (
        land_row,
        _euro_range("design_documentation_eur", design, lo, hi),
        construction_row,
        _euro_range("total_cost_eur", total, lo, hi),
    )
    return FeasibilityResult(
        fields=tuple(fields), cost_rows=cost_rows, assumptions_used=assumptions_used
    )
