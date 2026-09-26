"""Field specifications of the extraction contract: how each field is typed, which stated units it
takes, its canonical unit and its plausible range.

Product-wide and municipality-agnostic: the wording a document uses (terms, floor notation,
land-use names) is profile data (``[extraction]`` in ``municipalities/<id>.toml``). Range checks
never remove a value; a value outside the range is kept and flagged ``out_of_range`` for the
reviewer. The keys of the rule fields and of ``planned_parcel_area_m2`` are the keys of the
``planning_fields`` dictionary (migration 0003): those are the fields staged as review items.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.extraction.schema import StatedUnit

FieldKind = Literal["number", "text", "floors", "land_use", "date", "code"]

# Canonical unit symbol for each way a document states a unit (None = dimensionless).
UNIT_SYMBOLS: dict[str, str | None] = {
    "m": "m",
    "m2": "m²",
    "ha": "ha",
    "percent": "%",
    "ratio": None,
    "none": None,
}


@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str
    kind: FieldKind
    unit: str | None = None  # canonical unit symbol
    units: tuple[StatedUnit, ...] = ()  # stated units taken without a flag
    default_unit: StatedUnit | None = None  # assumed when nothing is printed (unit_assumed)
    percent: bool = False  # a ratio (0.4) is converted to a percentage (40 %)
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    planning_field: bool = False  # staged as a review item under ``key``

    def in_range(self, value: float) -> bool:
        if self.minimum is not None:
            if value < self.minimum or (self.exclusive_minimum and value == self.minimum):
                return False
        return self.maximum is None or value <= self.maximum


def _number(key: str, unit: str | None, **kw) -> FieldSpec:
    return FieldSpec(key, "number", unit=unit, **kw)


RULE_FIELDS: tuple[str, ...] = (
    "land_use",
    "max_site_coverage_pct",
    "max_far",
    "max_height_m",
    "max_floors",
    "building_line_m",
    "setback_neighbours_m",
    "parking_requirement",
    "min_green_area_pct",
    "utilities",
)

FIELD_SPECS: dict[str, FieldSpec] = {
    spec.key: spec
    for spec in (
        # --- planning fields (Group 1, staged) ---
        FieldSpec("land_use", "land_use", planning_field=True),
        _number(
            "max_site_coverage_pct",
            "%",
            units=("percent", "ratio"),
            percent=True,
            minimum=0,
            maximum=100,
            planning_field=True,
        ),
        _number(
            "max_far", None, units=("ratio", "none"), minimum=0, maximum=20, planning_field=True
        ),
        _number(
            "max_height_m",
            "m",
            units=("m",),
            default_unit="m",
            minimum=0,
            maximum=300,
            exclusive_minimum=True,
            planning_field=True,
        ),
        FieldSpec("max_floors", "floors", planning_field=True),
        _number(
            "building_line_m",
            "m",
            units=("m",),
            default_unit="m",
            minimum=0,
            maximum=200,
            planning_field=True,
        ),
        _number(
            "setback_neighbours_m",
            "m",
            units=("m",),
            default_unit="m",
            minimum=0,
            maximum=200,
            planning_field=True,
        ),
        FieldSpec("parking_requirement", "text", planning_field=True),
        _number(
            "min_green_area_pct",
            "%",
            units=("percent", "ratio"),
            percent=True,
            minimum=0,
            maximum=100,
            planning_field=True,
        ),
        _number(
            "planned_parcel_area_m2",
            "m²",
            units=("m2", "ha"),
            default_unit="m2",
            minimum=0,
            maximum=10_000_000,
            exclusive_minimum=True,
            planning_field=True,
        ),
        FieldSpec("utilities", "text", planning_field=True),
        # --- entity fields (context for the reviewer and the registration check, not staged) ---
        FieldSpec("urban_parcel_number", "text"),
        FieldSpec("block_ref", "text"),
        FieldSpec("public_area_relation", "text"),
        _number(
            "area_m2",
            "m²",
            units=("m2", "ha"),
            default_unit="m2",
            minimum=0,
            maximum=100_000_000,
            exclusive_minimum=True,
        ),
        FieldSpec("name", "text"),
        FieldSpec("document_type", "code"),
        FieldSpec("status", "code"),
        FieldSpec("gazette_reference", "text"),
        FieldSpec("decision_number", "text"),
        FieldSpec("decision_date", "date"),
        _number(
            "area_ha",
            "ha",
            units=("ha", "m2"),
            default_unit="ha",
            minimum=0,
            maximum=100_000,
            exclusive_minimum=True,
        ),
        FieldSpec("note", "text"),  # other conditions, notes, descriptions, legend entries
    )
}

PLANNING_FIELD_KEYS: tuple[str, ...] = tuple(k for k, s in FIELD_SPECS.items() if s.planning_field)
