"""Deterministic calculation engines. Pure Python: no I/O, no database, no municipality knowledge.

``core.engine.feasibility`` implements the client's feasibility formulas (formula version
``poc-1``). It emits numbers and reason codes only; labels and reason texts live in the API layer.
"""

from core.engine.feasibility import (
    COST_ROW_KEYS,
    DEFAULT_SALEABLE_SHARE,
    FIELD_KEYS,
    FORMULA_VERSION,
    Assumptions,
    AssumptionsUsed,
    FeasibilityResult,
    FieldRange,
    MarketInputs,
    RateSources,
    ReasonCode,
    compute_feasibility,
)

__all__ = [
    "COST_ROW_KEYS",
    "DEFAULT_SALEABLE_SHARE",
    "FIELD_KEYS",
    "FORMULA_VERSION",
    "Assumptions",
    "AssumptionsUsed",
    "FeasibilityResult",
    "FieldRange",
    "MarketInputs",
    "RateSources",
    "ReasonCode",
    "compute_feasibility",
]
