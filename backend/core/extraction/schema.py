"""Canonical extraction contract: what AI document extraction produces (``SCHEMA_VERSION``).

AI has one job in UrbanView: read the planning documents so people do not transcribe them. It
structures what a document states against the document / block / urban parcel model and attaches
a source reference to every value. It never invents a planning value and never does arithmetic:
the model only transcribes (``core.extraction.response``); deterministic code types, normalises
and verifies the transcription (``core.extraction.validate``) into the models below. Nothing here
publishes: extraction items reach the review queue as ``pending_review``
(``core.extraction.staging``) and the map only through the publish job.

Every field is a leaf:

* :class:`StatedValue`: the document states it. ``value`` is a number in the field's canonical
  unit or the document's own wording; ``raw_text`` is the page text it was read from, verbatim
  (whitespace runs collapsed); ``source`` names the document, the 1-based page, the bbox in PDF
  points (origin bottom-left, like the review queue) and the table cell when it came from a
  table; ``stated`` keeps the value and unit as printed next to any ``normalisation``; ``flags``
  tell the reviewer what to look at (``low_confidence``, ``out_of_range`` ...).
* :class:`MissingValue`: ``value`` is null and ``reason`` says why: ``not_found`` (the pages do
  not state it), ``deferred`` (the document leaves it to a later decision, e.g. an architectural
  competition; ``raw_text`` and ``source`` show where) or ``unverified`` (the model returned a
  value whose text is not on the cited page, or whose text does not contain the value; the
  candidate is kept in :attr:`ExtractionResult.issues`, never dropped silently).

Versioning: ``SCHEMA_VERSION`` is ``major.minor``. A minor version only adds (a field, a flag, an
enum value); a major version changes shapes, and the previous major's reader stays in
``PAYLOAD_READERS`` so items stored under it stay readable (:func:`read_payload`).
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1.0"
BBOX_SPACE = "pdf-points-bottom-left"

ExtractionMethod = Literal["text", "table", "ocr"]
MissingReason = Literal["not_found", "deferred", "unverified"]
DocumentStatus = Literal["adopted", "in_progress", "superseded"]
# How the document expresses a value; the model reports it, code converts (never the model).
StatedUnit = Literal["m", "m2", "ha", "percent", "ratio", "none"]
TaskKind = Literal[
    "document", "block", "urban_parcel", "parameter_table", "infrastructure", "land_use_legend"
]
EntityType = Literal["urban_parcel", "block", "document"]
UtilityKind = Literal[
    "water_supply",
    "sewerage",
    "stormwater",
    "electricity",
    "heating",
    "gas",
    "telecom",
    "access_road",
    "other",
]
UtilityStatus = Literal["existing", "planned", "unknown"]


class Flag(StrEnum):
    """Why a stated value deserves the reviewer's attention. Flags never remove a value."""

    low_confidence = "low_confidence"  # below EXTRACTION_LOW_CONFIDENCE
    out_of_range = "out_of_range"  # outside the field's plausible range (kept as stated)
    unit_assumed = "unit_assumed"  # no unit printed; the field's usual unit was assumed
    unit_unexpected = "unit_unexpected"  # a unit the field does not take; kept as stated
    number_ambiguous = "number_ambiguous"  # e.g. "1.500": decimal or thousands separator
    text_for_numeric_field = "text_for_numeric_field"  # a number field stated in words ("h/2")
    page_corrected = "page_corrected"  # the model cited another page; the text is on this one
    bbox_ambiguous = "bbox_ambiguous"  # the text occurs more than once on the page: no highlight
    cell_mismatch = "cell_mismatch"  # the cited table cell does not hold the value
    aggregate_row = "aggregate_row"  # read from a table's totals row (sums), not a rule
    found_under_other_parcel = "found_under_other_parcel"  # its text sits in another parcel's part
    land_use_unmapped = "land_use_unmapped"  # no land-use class matches the wording
    unknown_code = "unknown_code"  # a code outside the allowed list (document type, status)
    floors_not_derivable = "floors_not_derivable"  # notation tokens the profile does not know
    date_not_parsed = "date_not_parsed"  # a date field whose wording is not a d.m.yyyy date


class LandUseClass(StrEnum):
    """UrbanView's land-use classes (product-wide). The document's own wording stays the value;
    the class is derived from it with the profile's term table or the document's legend."""

    residential = "residential"
    residential_mixed = "residential_mixed"  # housing with business activities
    mixed_use = "mixed_use"
    central_activities = "central_activities"  # commerce, offices, services
    tourism = "tourism"
    education_social = "education_social"
    health = "health"
    culture = "culture"
    religious = "religious"
    sport_recreation = "sport_recreation"
    green_space = "green_space"
    traffic_infrastructure = "traffic_infrastructure"
    utility_infrastructure = "utility_infrastructure"
    industry = "industry"
    special_purpose = "special_purpose"
    other = "other"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- leaves ---------------------------------------------------------------------------------------


class TableRef(_Strict):
    """Where in a table a value sits, as printed (row label, column header) plus the grid cell id
    when the pipeline gave the model a table grid."""

    table: str | None = None
    row: str | None = Field(default=None, description="Row label as printed, e.g. 'UP 12'")
    column: str | None = Field(default=None, description="Column header as printed")
    cell: str | None = Field(default=None, description="Grid cell id, e.g. 'r4c11'")


class SourceRef(_Strict):
    document_id: int = Field(ge=1)
    page: int = Field(ge=1, description="1-based page of the source PDF")
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, description="x0, y0, x1, y1 in PDF points, origin bottom-left"
    )
    bbox_space: Literal["pdf-points-bottom-left"] = BBOX_SPACE
    table_ref: TableRef | None = None


class Stated(_Strict):
    """The value and unit exactly as the document printed them, before normalisation."""

    value: str
    unit: StatedUnit | None = None


class FloorCount(_Strict):
    """Floors parsed from the local notation (``Po+P+6``) by code, never by the model."""

    notation: str
    below_ground: int = Field(ge=0)
    above_ground: int = Field(ge=0)
    attic: int = Field(ge=0, description="Attic / mansard / set-back floors among above_ground")
    rule: Literal["floor-notation-v1"] = "floor-notation-v1"


class StatedValue(_Strict):
    value: float | str
    unit: str | None = Field(default=None, description="Canonical unit: %, m, m², ha or null")
    raw_text: str = Field(min_length=1, description="The page text the value was read from")
    source: SourceRef
    confidence: float = Field(ge=0, le=1)
    extraction_method: ExtractionMethod
    stated: Stated
    normalisation: list[str] = Field(
        default_factory=list, description="Rules applied to the stated value, in order"
    )
    derived: FloorCount | None = None
    category: LandUseClass | None = None
    flags: list[Flag] = Field(default_factory=list)


class MissingValue(_Strict):
    value: None = None
    reason: MissingReason
    raw_text: str | None = Field(default=None, description="deferred: the text that defers it")
    source: SourceRef | None = None
    note: str | None = None


Leaf = StatedValue | MissingValue


def not_found() -> MissingValue:
    return MissingValue(reason="not_found")


# --- entities ------------------------------------------------------------------------------------


class RuleFields(_Strict):
    """The planning rules a document states for one scope (an urban parcel, a block or the whole
    document): the Group 1 fields of ``planning_fields`` except the planned parcel area."""

    land_use: Leaf
    max_site_coverage_pct: Leaf
    max_far: Leaf
    max_height_m: Leaf
    max_floors: Leaf
    building_line_m: Leaf
    setback_neighbours_m: Leaf
    parking_requirement: Leaf
    min_green_area_pct: Leaf
    utilities: Leaf


class UrbanParcel(_Strict):
    urban_parcel_number: Leaf = Field(description="As printed, e.g. 'UP 12', 'A116/1'")
    parcel_key: str | None = Field(
        default=None, description="Lookup key derived from the number ('12', 'a116/1')"
    )
    block_ref: Leaf
    planned_parcel_area_m2: Leaf
    rules: RuleFields
    public_area_relation: Leaf = Field(
        description="Relation to public areas (regulation, access), in the document's words"
    )
    other_conditions: list[StatedValue] = Field(default_factory=list)


class Block(_Strict):
    """A plan's own subdivision grouping urban parcels ('Blok A', 'Zona B'). Not an UrbanView
    zone: those are internal city divisions and are never extracted from documents."""

    block_ref: Leaf
    block_key: str | None = None
    total_row: bool = Field(
        default=False, description="Read from a table's totals row: sums, not a rule set"
    )
    area_m2: Leaf
    rules: RuleFields
    notes: list[StatedValue] = Field(default_factory=list)


class Amendment(_Strict):
    relation: Literal["amends", "amended_by"]
    document_name: StatedValue


class PlanningDocument(_Strict):
    name: Leaf
    document_type: Leaf = Field(description="A document type key of the municipality profile")
    status: Leaf = Field(description="adopted | in_progress | superseded, with the evidence")
    gazette_reference: Leaf
    decision_number: Leaf
    decision_date: Leaf = Field(description="ISO date; stated keeps the printed form")
    area_ha: Leaf
    amendments: list[Amendment] = Field(default_factory=list)
    rules: RuleFields = Field(description="Rules the document states for its whole area")
    notes: list[StatedValue] = Field(default_factory=list)


class ScopeRef(_Strict):
    level: Literal["document", "block", "urban_parcel"]
    ref: str | None = Field(default=None, description="As printed: 'UP 12', 'Blok A'")


class UtilityItem(_Strict):
    scope: ScopeRef
    kind: UtilityKind
    status: UtilityStatus
    description: StatedValue


class LegendEntry(_Strict):
    code: Leaf = Field(description="Legend code as printed ('SS'); missing when names only")
    name: StatedValue
    category: LandUseClass | None = None


class ColumnMapping(_Strict):
    table: str | None = None
    page: int = Field(ge=1)
    header: str
    field: str | None = Field(default=None, description="Field key the column maps to, or null")


IssueCode = Literal[
    "malformed_leaf",
    "page_not_in_input",
    "raw_text_not_on_page",
    "value_not_in_raw_text",
    "parcel_without_number",
    "duplicate_parcel",
]


class Issue(_Strict):
    code: IssueCode
    severity: Literal["error", "warning"]
    path: str = Field(description="Where in the result, e.g. 'urban_parcels[3].rules.max_far'")
    message: str
    candidate: dict[str, Any] | None = Field(
        default=None, description="What the model returned, kept for the reviewer"
    )


class ResultStats(_Strict):
    stated: int = 0
    missing: dict[str, int] = Field(default_factory=dict)
    flags: dict[str, int] = Field(default_factory=dict)
    issues: int = 0


class ExtractionResult(_Strict):
    """One extraction run over some pages of one document version."""

    schema_version: str = SCHEMA_VERSION
    prompt_version: str
    task: TaskKind
    municipality_id: str
    document_id: int = Field(ge=1)
    pages: list[int]
    model: str | None = None
    document: PlanningDocument | None = None
    blocks: list[Block] = Field(default_factory=list)
    urban_parcels: list[UrbanParcel] = Field(default_factory=list)
    utilities: list[UtilityItem] = Field(default_factory=list)
    land_use_legend: list[LegendEntry] = Field(default_factory=list)
    table_columns: list[ColumnMapping] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    stats: ResultStats = Field(default_factory=ResultStats)

    @field_validator("schema_version")
    @classmethod
    def _readable_major(cls, value: str) -> str:
        if _major(value) != _major(SCHEMA_VERSION):
            raise ValueError(f"schema {value} is not read by the {SCHEMA_VERSION} models")
        return value


# --- walking the leaves ---------------------------------------------------------------------------


def iter_leaves(result: ExtractionResult) -> list[tuple[str, Leaf]]:
    """Every leaf of a result with its path, in document order."""
    out: list[tuple[str, Leaf]] = []

    def walk(prefix: str, obj: Any) -> None:
        if isinstance(obj, StatedValue | MissingValue):
            out.append((prefix, obj))
        elif isinstance(obj, BaseModel):
            for name in type(obj).model_fields:
                walk(f"{prefix}.{name}" if prefix else name, getattr(obj, name))
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                walk(f"{prefix}[{i}]", item)

    for name in ("document", "blocks", "urban_parcels", "utilities", "land_use_legend"):
        walk(name, getattr(result, name))
    return out


def summarise(result: ExtractionResult) -> ResultStats:
    stated = 0
    missing: Counter[str] = Counter()
    flags: Counter[str] = Counter()
    for _, leaf in iter_leaves(result):
        if isinstance(leaf, StatedValue):
            stated += 1
            flags.update(str(f) for f in leaf.flags)
        else:
            missing[leaf.reason] += 1
    return ResultStats(
        stated=stated, missing=dict(missing), flags=dict(flags), issues=len(result.issues)
    )


# --- stored items ---------------------------------------------------------------------------------


class StagedPayload(_Strict):
    """What a review-queue item stores in ``planning_parameter_extractions.payload``: the leaf as
    extracted plus the context needed to read it without the run that produced it."""

    schema_version: str
    prompt_version: str | None = None
    task: TaskKind
    entity_type: EntityType
    path: str
    field_key: str
    urban_parcel_number: str | None = None
    block_ref: str | None = None
    leaf: StatedValue


class UnsupportedSchemaVersion(ValueError):
    pass


def _major(version: str) -> int:
    head = version.split(".", 1)[0]
    if not head.isdigit():
        raise UnsupportedSchemaVersion(f"not a schema version: {version!r}")
    return int(head)


# One reader per major version, kept when a new major arrives.
PAYLOAD_READERS = {1: StagedPayload.model_validate}


def read_payload(schema_version: str, payload: dict[str, Any]) -> StagedPayload:
    """Read a stored extraction item with the reader of the major version it was written in."""
    reader = PAYLOAD_READERS.get(_major(schema_version))
    if reader is None:
        raise UnsupportedSchemaVersion(f"no reader for extraction schema {schema_version}")
    return reader(payload)


def result_json_schema() -> dict[str, Any]:
    """JSON Schema of :class:`ExtractionResult` (exported to ``schemas/``)."""
    schema = ExtractionResult.model_json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urbanview:extraction-result:{SCHEMA_VERSION}",
        "x-schema-version": SCHEMA_VERSION,
        **schema,
    }
