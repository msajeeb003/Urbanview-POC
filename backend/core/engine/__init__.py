"""Deterministic calculation engines. Pure Python: no I/O, no database, no municipality knowledge.

``core.engine.shared`` is the Python copy of the shared TypeScript engine
(``packages/feasibility-engine``); both are held to the same fixtures with exact equality.
``core.engine.feasibility`` is the panel-facing adapter over it (panel field keys, cost rows,
the zone's market row as inputs). Both emit numbers and reason codes only; labels and reason texts
live in the API layer.
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
