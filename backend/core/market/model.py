"""Types of the market-data import path (``core.market``).

A :class:`RawTable` is a file as read (sheets of cells, nothing interpreted); a
:class:`SheetMapping` says what its rows and columns mean (the rules or the LLM produce it, never
a number); :class:`MarketInput` is one normalised zone-level input (low / expected / high in EUR
per m² for one metric) with its source, reference date, flags and the raw cells it came from.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Metric = Literal["land_rate", "build_rate", "design_rate", "sale_rate"]
METRICS: tuple[Metric, ...] = ("land_rate", "build_rate", "design_rate", "sale_rate")
Bound = Literal["low", "expected", "high"]
BOUNDS: tuple[Bound, ...] = ("low", "expected", "high")
ImportKind = Literal["statistics", "client_ranges", "listings"]
IMPORT_KINDS: tuple[ImportKind, ...] = ("statistics", "client_ranges", "listings")
RangeBasis = Literal["stated", "derived", "listings", "unavailable"]

# What the metric means (the engine's rates, core.engine.feasibility): also the model's glossary.
METRIC_MEANING: dict[Metric, str] = {
    "land_rate": "land value per m² of PARCEL area",
    "build_rate": "construction cost per m² of gross floor area",
    "design_rate": "design & documentation cost per m² of gross floor area",
    "sale_rate": "selling price per m² of saleable floor area",
}

Cell = str | float | int | None


@dataclass(slots=True)
class Sheet:
    name: str
    rows: list[list[Cell]]
    merged: list[str] = field(default_factory=list)  # A1 ranges, filled from their first cell

    @property
    def width(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def cell(self, row: int, column: int) -> Cell:
        if 0 <= row < len(self.rows) and 0 <= column < len(self.rows[row]):
            return self.rows[row][column]
        return None


@dataclass(slots=True)
class RawTable:
    """A file as read: ``format`` csv | xlsx | listings, ``sheets`` of untouched cells."""

    format: str
    sheets: list[Sheet]
    encoding: str | None = None
    delimiter: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "sheets": [asdict(s) for s in self.sheets],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RawTable:
        return cls(
            format=data["format"],
            encoding=data.get("encoding"),
            delimiter=data.get("delimiter"),
            sheets=[
                Sheet(name=s["name"], rows=s["rows"], merged=s.get("merged", []))
                for s in data["sheets"]
            ],
        )

    @property
    def row_count(self) -> int:
        return sum(len(s.rows) for s in self.sheets)


# --- mapping: what the cells mean (rules or the LLM; never a figure) ------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ColumnRole(_Strict):
    column: int = Field(description="0-based column index")
    role: Literal["geography", "value", "metric_label", "period", "ignore"]
    metric: Metric | None = Field(
        default=None, description="value columns of a wide table: the metric the column holds"
    )
    bound: Bound | None = Field(
        default=None, description="value columns: low, expected or high (one figure = expected)"
    )
    unit_as_printed: str | None = None
    period_as_printed: str | None = None
    header: str | None = Field(default=None, description="the header text as printed")
    reason: str | None = Field(default=None, description="why the column is ignored")


class RowPlace(_Strict):
    row: int = Field(description="0-based row index")
    geography_as_printed: str | None = None
    applies_to: Literal["zone", "municipality", "other", "none"]
    zone_id: int | None = None
    metric: Metric | None = Field(
        default=None, description="long tables: the metric of this row (its label cell)"
    )
    period_as_printed: str | None = None
    confidence: float = 1.0
    method: Literal["exact", "alias", "municipality", "table", "llm"] = "exact"
    reason: str | None = None


class SheetMapping(_Strict):
    sheet: int
    header_rows: list[int] = Field(default_factory=list)
    columns: list[ColumnRole] = Field(default_factory=list)
    rows: list[RowPlace] = Field(default_factory=list)
    table_geography: str | None = None
    table_period_as_printed: str | None = None
    table_unit_as_printed: str | None = None
    method: Literal["rules", "llm", "rules+llm"] = "rules"
    notes: str | None = None

    def column(self, index: int) -> ColumnRole | None:
        return next((c for c in self.columns if c.column == index), None)


# --- normalised output ----------------------------------------------------------------------------


@dataclass(slots=True)
class ZoneRef:
    id: int
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RangeFactors:
    """The configured range of a zone's current assumptions row (or the municipality row)."""

    low: float
    high: float
    assumption_id: int
    zone_id: int | None


@dataclass(slots=True)
class MarketInput:
    zone_id: int
    metric: Metric
    expected: float
    low: float | None
    high: float | None
    range_basis: RangeBasis
    source: str
    source_date: date | None
    confidence: float | None
    notes: str | None
    flags: list[str]
    raw: dict[str, Any]
    mapping: dict[str, Any]
    normaliser: str
    currency: str = "EUR"
    unit: str = "EUR/m²"

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_date"] = self.source_date.isoformat() if self.source_date else None
        return data


@dataclass(slots=True)
class Skipped:
    """A row or column that gave no input, and why (the report lists every one)."""

    reason: str
    sheet: int | None = None
    row: int | None = None
    column: int | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(slots=True)
class NormaliseResult:
    inputs: list[MarketInput]
    skipped: list[Skipped]
    mappings: list[SheetMapping]
    normaliser: str
    issues: list[str] = field(default_factory=list)
    llm: dict[str, Any] | None = None

    def report(self) -> dict[str, Any]:
        by_reason: dict[str, int] = {}
        for s in self.skipped:
            by_reason[s.reason] = by_reason.get(s.reason, 0) + 1
        return {
            "normaliser": self.normaliser,
            "inputs": len(self.inputs),
            "zones": sorted({i.zone_id for i in self.inputs}),
            "metrics": sorted({i.metric for i in self.inputs}),
            "skipped": [s.to_json() for s in self.skipped],
            "skipped_by_reason": by_reason,
            "issues": self.issues,
            "mappings": [m.model_dump(mode="json") for m in self.mappings],
            "llm": self.llm,
        }
