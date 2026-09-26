"""Compact answers (prompt set 1.1 onwards): what the model returns, flat and union-free.

The 1.0 response models asked for every field of every entity as a nested object with nullable
members; the API refuses that schema as too complex (HTTP 400 "Schema is too complex", seen on the
first live request) and it made the model write hundreds of "not found" objects per page. A
compact answer lists, per entity, **only the fields the pages state**, each as one flat entry
(value as printed, how the unit is printed, the verbatim text, page, table / cell / column,
confidence, stated or deferred). No member is optional or nullable: "" means "not given".

:meth:`to_legacy` turns a compact answer into the 1.0 response model of its task, so validation
(``core.extraction.validate.assemble``) and the canonical contract are unchanged: a field an
entity does not list becomes ``not_found``; a deferred entry becomes ``deferred`` with its text;
a field listed twice keeps the first entry and turns the others into other conditions / notes
(never dropped).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.extraction.fields import RULE_FIELDS
from core.extraction.response import (
    BlocksResponse,
    DocumentResponse,
    InfrastructureResponse,
    LegendResponse,
    OutAmendment,
    OutBlock,
    OutColumn,
    OutDocument,
    OutLegendEntry,
    OutParcel,
    OutRules,
    OutTableRef,
    OutUtility,
    OutValue,
    ParcelsResponse,
)
from core.extraction.schema import UtilityKind, UtilityStatus

PrintedUnit = Literal["m", "m2", "ha", "percent", "ratio", "none", "words"]
Status = Literal["stated", "deferred"]
RuleField = Literal[
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
ParcelField = Literal[
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
    "public_area_relation",
    "other_condition",
]
BlockField = Literal[
    "area_m2",
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
    "note",
]
DocumentField = Literal[
    "name",
    "document_type",
    "status",
    "gazette_reference",
    "decision_number",
    "decision_date",
    "area_ha",
    "amends",
    "amended_by",
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
    "note",
]
ColumnTarget = Literal[
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
    "none",
]


class _C(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Entry(_C):
    """One value as printed. Every member is present; "" means "not given"."""

    value: str = Field(description='The value exactly as printed; "" when status is deferred')
    unit: PrintedUnit = Field(
        description=(
            "How the value is printed: m, m2, ha, percent, ratio (an index as a plain decimal), "
            "none (a bare number), words (a value in words)"
        )
    )
    text: str = Field(
        description="Verbatim, contiguous page text the value is read from (the cell text)"
    )
    page: int = Field(description="Page number from the page marker")
    table: str = Field(description='Table id as given (p7t1), "" when not from a table')
    cell: str = Field(description='Cell id as given (r4c11), "" when not from a table grid')
    column: str = Field(description='Column header as printed, "" when not from a table')
    confidence: float = Field(description="0 to 1: legible and belongs to this field and entity")
    status: Status = Field(description="stated, or deferred (set later): then text says so")


class ParcelEntry(_Entry):
    field: ParcelField


class BlockEntry(_Entry):
    field: BlockField


class DocumentEntry(_Entry):
    field: DocumentField


class CParcel(_C):
    number: str = Field(description="The urban parcel number exactly as printed")
    page: int
    table: str = Field(description='Table id, "" when not from a table')
    number_cell: str = Field(description='Cell id of the number, "" when none')
    block: str = Field(description='The block / plan zone as printed in its row, "" when none')
    block_cell: str = Field(description='Cell id of the block, "" when none')
    confidence: float
    values: list[ParcelEntry] = Field(description="Only the fields the pages state for it")


class CBlock(_C):
    label: str = Field(description="The block / plan zone label as printed")
    page: int
    table: str
    label_cell: str
    is_total_row: bool = Field(description="A totals row of the block (sums), not rules")
    confidence: float
    values: list[BlockEntry]


class CColumn(_C):
    table: str
    page: int
    header: str = Field(description="Column header as printed")
    field: ColumnTarget


class CUtility(_C):
    scope_level: Literal["document", "block", "urban_parcel"]
    scope_ref: str = Field(description='Block label or parcel number, "" for the document')
    kind: UtilityKind
    status: UtilityStatus
    description: str = Field(description="The statement as printed")
    page: int
    confidence: float


class CLegendEntry(_C):
    code: str = Field(description='Legend code as printed ("SS"), "" when the legend has none')
    name: str = Field(description="The land-use name as printed")
    page: int
    table: str
    code_cell: str
    name_cell: str
    confidence: float


# --- conversion to the 1.0 response models ------------------------------------------------------


def _ref(table: str, cell: str, row: str | None, column: str) -> OutTableRef | None:
    if not (table or cell or column):
        return None
    return OutTableRef(
        table=table or None, row=row or None, column=column or None, cell=cell or None
    )


def missing() -> OutValue:
    return OutValue(value=None, absent_reason="not_found", confidence=1.0)


def value_of(entry: _Entry, row: str | None = None) -> OutValue:
    ref = _ref(entry.table, entry.cell, row, entry.column)
    text = entry.text.strip() or entry.value.strip()
    if entry.status == "deferred":
        return OutValue(
            value=None,
            raw_text=text or None,
            page=entry.page,
            table_ref=ref,
            confidence=entry.confidence,
            absent_reason="deferred",
        )
    return OutValue(
        value=entry.value,
        unit=None if entry.unit == "words" else entry.unit,
        raw_text=text or None,
        page=entry.page,
        table_ref=ref,
        confidence=entry.confidence,
    )


def _label(text: str, page: int, table: str, cell: str, confidence: float) -> OutValue:
    if not text.strip():
        return missing()
    return OutValue(
        value=text,
        unit=None,
        raw_text=text,
        page=page,
        table_ref=_ref(table, cell, text, ""),
        confidence=confidence,
    )


def _split(entries: list, keys: tuple[str, ...], row: str | None):
    """(first entry per field among ``keys`` as OutValue, the rest in order)."""
    first: dict[str, OutValue] = {}
    rest: list[_Entry] = []
    for entry in entries:
        if entry.field in keys and entry.field not in first:
            first[entry.field] = value_of(entry, row)
        else:
            rest.append(entry)
    return first, rest


def _rules(first: dict[str, OutValue]) -> OutRules:
    return OutRules(**{key: first.get(key) or missing() for key in RULE_FIELDS})


class CompactParcelsResponse(_C):
    urban_parcels: list[CParcel]
    blocks: list[CBlock]
    columns: list[CColumn]

    def to_legacy(self) -> ParcelsResponse:
        parcels = []
        for p in self.urban_parcels:
            first, rest = _split(
                p.values, ("planned_parcel_area_m2", "public_area_relation", *RULE_FIELDS), p.number
            )
            parcels.append(
                OutParcel(
                    urban_parcel_number=_label(
                        p.number, p.page, p.table, p.number_cell, p.confidence
                    ),
                    block_ref=_label(p.block, p.page, p.table, p.block_cell, p.confidence),
                    planned_parcel_area_m2=first.get("planned_parcel_area_m2") or missing(),
                    rules=_rules(first),
                    public_area_relation=first.get("public_area_relation") or missing(),
                    other_conditions=[value_of(e, p.number) for e in rest],
                )
            )
        return ParcelsResponse(
            urban_parcels=parcels,
            blocks=[block_to_legacy(b) for b in self.blocks],
            columns=[
                OutColumn(
                    table=c.table or None,
                    page=c.page,
                    header=c.header,
                    field=None if c.field == "none" else c.field,
                )
                for c in self.columns
            ],
        )


def block_to_legacy(b: CBlock) -> OutBlock:
    first, rest = _split(b.values, ("area_m2", *RULE_FIELDS), b.label)
    return OutBlock(
        block_ref=_label(b.label, b.page, b.table, b.label_cell, b.confidence),
        is_total_row=b.is_total_row,
        area_m2=first.get("area_m2") or missing(),
        rules=_rules(first),
        notes=[value_of(e, b.label) for e in rest],
    )


class CompactBlocksResponse(_C):
    blocks: list[CBlock]

    def to_legacy(self) -> BlocksResponse:
        return BlocksResponse(blocks=[block_to_legacy(b) for b in self.blocks])


IDENTITY = (
    "name",
    "document_type",
    "status",
    "gazette_reference",
    "decision_number",
    "decision_date",
    "area_ha",
)


class CompactDocumentResponse(_C):
    values: list[DocumentEntry]

    def to_legacy(self) -> DocumentResponse:
        first, rest = _split(self.values, (*IDENTITY, *RULE_FIELDS), None)
        amendments = [
            OutAmendment(relation=e.field, document_name=value_of(e))  # type: ignore[arg-type]
            for e in rest
            if e.field in ("amends", "amended_by")
        ]
        notes = [value_of(e) for e in rest if e.field not in ("amends", "amended_by")]
        return DocumentResponse(
            document=OutDocument(
                **{key: first.get(key) or missing() for key in IDENTITY},
                amendments=amendments,
                rules=_rules(first),
                notes=notes,
            )
        )


class CompactInfrastructureResponse(_C):
    utilities: list[CUtility]

    def to_legacy(self) -> InfrastructureResponse:
        return InfrastructureResponse(
            utilities=[
                OutUtility(
                    scope_level=u.scope_level,
                    scope_ref=u.scope_ref or None,
                    kind=u.kind,
                    status=u.status,
                    description=OutValue(
                        value=u.description,
                        raw_text=u.description,
                        page=u.page,
                        confidence=u.confidence,
                    ),
                )
                for u in self.utilities
            ]
        )


class CompactLegendResponse(_C):
    entries: list[CLegendEntry]

    def to_legacy(self) -> LegendResponse:
        return LegendResponse(
            entries=[
                OutLegendEntry(
                    code=_label(e.code, e.page, e.table, e.code_cell, e.confidence),
                    name=_label(e.name, e.page, e.table, e.name_cell, e.confidence),
                )
                for e in self.entries
            ]
        )


COMPACT_MODELS: dict[str, type[BaseModel]] = {
    model.__name__: model
    for model in (
        CompactParcelsResponse,
        CompactBlocksResponse,
        CompactDocumentResponse,
        CompactInfrastructureResponse,
        CompactLegendResponse,
    )
}
