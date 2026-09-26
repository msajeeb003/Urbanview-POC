"""What the model returns: a transcription, one response model per task (``RESPONSE_MODELS``).

Every field is an :class:`OutValue`: the value exactly as printed (always a string: the model
never types, converts or computes a number), how the document expresses its unit, a verbatim
``raw_text`` with the page it is on, an optional table reference, a confidence, and for a field
the pages do not state, ``value: null`` with ``absent_reason`` ``not_found`` or ``deferred``. The
validator (``core.extraction.validate``) turns this into the canonical contract.

:func:`strict_json_schema` is the structured-output format sent with the request: every object
closed (``additionalProperties: false``) with all its properties required, and the keywords
structured outputs do not support (numeric and string bounds, defaults, titles) removed.
Confidence bounds and the leaf rules are therefore checked by the validator, not the schema.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.extraction.schema import StatedUnit, TaskKind, UtilityKind, UtilityStatus


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutTableRef(_Out):
    table: str | None = Field(default=None, description="Table id as given (p7t1) or its caption")
    row: str | None = Field(default=None, description="Row label as printed, e.g. the parcel no.")
    column: str | None = Field(default=None, description="Column header as printed")
    cell: str | None = Field(default=None, description="Cell id as given (r4c11)")


class OutValue(_Out):
    value: str | None = Field(
        default=None,
        description=(
            "The value exactly as printed (digits, decimal separator, abbreviations, spelling "
            "untouched); for a code field, the code. null when the pages do not state it."
        ),
    )
    unit: StatedUnit | None = Field(
        default=None,
        description=(
            "How the document expresses the value: m, m2, ha, percent (a % sign or a % column), "
            "ratio (an index printed as a plain decimal), none (a bare number); null for words."
        ),
    )
    raw_text: str | None = Field(
        default=None,
        description=(
            "Verbatim, contiguous copy of the page text the value is read from (the table cell, "
            "or a short phrase with its label); contains the value. For a deferred field, the "
            "text that defers it."
        ),
    )
    page: int | None = Field(default=None, description="Page number from the page marker")
    table_ref: OutTableRef | None = None
    confidence: float = Field(
        description="0 to 1: how sure you are the value is legible and belongs to this field"
    )
    absent_reason: Literal["not_found", "deferred"] | None = Field(
        default=None,
        description=(
            "null when value is given; not_found when the pages do not state it; deferred when "
            "the document leaves it to a later decision."
        ),
    )


class OutRules(_Out):
    land_use: OutValue
    max_site_coverage_pct: OutValue
    max_far: OutValue
    max_height_m: OutValue
    max_floors: OutValue
    building_line_m: OutValue
    setback_neighbours_m: OutValue
    parking_requirement: OutValue
    min_green_area_pct: OutValue
    utilities: OutValue


ColumnField = Literal[
    "urban_parcel_number",
    "block_ref",
    "planned_parcel_area_m2",
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
]


class OutParcel(_Out):
    urban_parcel_number: OutValue
    block_ref: OutValue
    planned_parcel_area_m2: OutValue
    rules: OutRules
    public_area_relation: OutValue
    other_conditions: list[OutValue] = Field(default_factory=list)


class OutBlock(_Out):
    block_ref: OutValue
    is_total_row: bool = False
    area_m2: OutValue
    rules: OutRules
    notes: list[OutValue] = Field(default_factory=list)


class OutColumn(_Out):
    table: str | None = None
    page: int
    header: str = Field(description="Column header as printed")
    field: ColumnField | None = Field(default=None, description="The field it maps to, or null")


class ParcelsResponse(_Out):
    urban_parcels: list[OutParcel]
    blocks: list[OutBlock] = Field(default_factory=list)
    columns: list[OutColumn] = Field(default_factory=list)


class BlocksResponse(_Out):
    blocks: list[OutBlock]


class OutAmendment(_Out):
    relation: Literal["amends", "amended_by"]
    document_name: OutValue


class OutDocument(_Out):
    name: OutValue
    document_type: OutValue
    status: OutValue
    gazette_reference: OutValue
    decision_number: OutValue
    decision_date: OutValue
    area_ha: OutValue
    amendments: list[OutAmendment] = Field(default_factory=list)
    rules: OutRules
    notes: list[OutValue] = Field(default_factory=list)


class DocumentResponse(_Out):
    document: OutDocument


class OutUtility(_Out):
    scope_level: Literal["document", "block", "urban_parcel"]
    scope_ref: str | None = Field(default=None, description="Block label or parcel number")
    kind: UtilityKind
    status: UtilityStatus
    description: OutValue


class InfrastructureResponse(_Out):
    utilities: list[OutUtility]


class OutLegendEntry(_Out):
    code: OutValue
    name: OutValue


class LegendResponse(_Out):
    entries: list[OutLegendEntry]


RESPONSE_MODELS: dict[TaskKind, type[BaseModel]] = {
    "document": DocumentResponse,
    "block": BlocksResponse,
    "urban_parcel": ParcelsResponse,
    "parameter_table": ParcelsResponse,
    "infrastructure": InfrastructureResponse,
    "land_use_legend": LegendResponse,
}

_UNSUPPORTED = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "default",
        "title",
    }
)


def _tighten(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _tighten(item)
        return
    if not isinstance(node, dict):
        return
    for key in _UNSUPPORTED & node.keys():
        del node[key]
    if node.get("type") == "object" and "properties" in node:
        node["additionalProperties"] = False
        node["required"] = list(node["properties"])
    for key, value in node.items():
        if key in ("properties", "$defs"):  # name -> schema maps: tighten the schemas only
            for schema in value.values():
                _tighten(schema)
        else:
            _tighten(value)


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The structured-output schema for ``model``."""
    schema = copy.deepcopy(model.model_json_schema())
    _tighten(schema)
    return schema
