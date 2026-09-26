"""The LLM step of market-data normalisation: interpretation only, never a figure.

Two structured calls (prompt set ``MARKET_PROMPT_VERSION``, strict JSON Schema, the same model
seam as document extraction, :class:`core.extraction.llm.StructuredModel`):

- ``map_structure``: for a sheet the rules cannot read, which rows are the header, what each
  column holds (area names, a metric with its bound, metric labels, periods) and the unit and
  period labels *as printed*;
- ``place_names``: which zone an area name is (a zone, the whole municipality, somewhere else, or
  unknown), with a confidence. Every placement it makes is flagged ``zone_mapped_by_ai`` for the
  reviewer.

The model never returns a number from the table, a converted unit, an average or a date: code
reads the cells it points at (:mod:`core.market.normalise`) and parses the labels it copied
(:mod:`core.market.parse`). Its answer is checked against the sheet and the zone list; anything
out of range is dropped and reported.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from core.extraction.llm import ModelOutputInvalid, ModelUsage, StructuredModel
from core.extraction.prompts import SystemBlock
from core.extraction.response import strict_json_schema
from core.market.model import (
    METRIC_MEANING,
    Bound,
    ColumnRole,
    Metric,
    Sheet,
    SheetMapping,
    ZoneRef,
)
from core.market.parse import cell_text

MARKET_PROMPT_VERSION = "market-1.0"
CELL_CHARS = 80


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutColumn(_Out):
    column: int
    role: Literal["geography", "value", "metric_label", "period", "ignore"]
    metric: Metric | None
    bound: Bound | None
    unit_as_printed: str | None
    period_as_printed: str | None
    reason: str | None


class OutRowMetric(_Out):
    row: int
    metric: Metric | None


class StructureResponse(_Out):
    header_rows: list[int]
    columns: list[OutColumn]
    row_metrics: list[OutRowMetric]
    non_data_rows: list[int]
    table_geography: str | None
    table_period_as_printed: str | None
    table_unit_as_printed: str | None
    notes: str | None


class OutPlace(_Out):
    name: str
    applies_to: Literal["zone", "municipality", "other", "none"]
    zone_id: int | None
    confidence: float
    reason: str | None


class PlacesResponse(_Out):
    places: list[OutPlace]


STRUCTURE_SCHEMA = strict_json_schema(StructureResponse)
PLACES_SCHEMA = strict_json_schema(PlacesResponse)

STRUCTURE_SYSTEM = """\
You read market-data tables for UrbanView, a planning and feasibility map. A table arrives as a
grid of cells addressed by 0-based row (r) and column (c) indexes. You say what the rows and
columns MEAN; code reads every figure from the cells you point at. Therefore:
- never write a figure from the table, never convert a unit, never compute an average, a range
  or a date; copy labels exactly as printed (unit_as_printed, period_as_printed, table_*);
- when a column is unclear, give it role "ignore" with a short reason rather than guess.

The metrics, all money per square metre:
{metrics}

Roles: "geography" = the column naming the area of each row (zone, district, settlement,
municipality); "value" = a column of figures for one metric and one bound: "low", "expected" (a
single figure, an average, a median or an expected value) or "high" - a cell holding a range
("1.200 - 1.500") is still one column, mark it "expected"; "metric_label" = in a table with one row
per metric, the column naming the metric of the row (its figure columns then have metric null,
and row_metrics gives each data row's metric); "period" = a column of periods or dates; "ignore"
= anything else (counts, indexes, percentage changes, other costs, a metric this import may not
give). header_rows are the rows of column headings. non_data_rows are rows below the header that
are not figures for an area (notes, sources, sub-headings). table_geography is the area the whole
table is about when no column names areas (copied from the title), else null.
"""

PLACES_SYSTEM = """\
You place area names from market-data tables and property listings in the zones of {municipality}.
Zones are UrbanView's divisions of the city (roughly city quarters). For each name answer:
- "zone" with its zone_id when the name is the zone, one of its other spellings, or a
  neighbourhood, street or settlement that you know lies inside that zone;
- "municipality" when the name means the whole municipality ({municipality_names});
- "other" when the name is somewhere else (another municipality, the country total, a region);
- "none" when you cannot tell.
Never guess: when you are less than 0.6 sure, answer "none". confidence is 0-1, how sure you are of
the placement; reason is one short sentence. Answer every name exactly once, copied as given.
"""


@dataclass(slots=True)
class LlmLog:
    """What the LLM step cost and said, for the import report and the job's cost record."""

    model: str | None = None
    calls: int = 0
    usage: ModelUsage = field(default_factory=ModelUsage)
    notes: list[str] = field(default_factory=list)

    def add(self, model: str, usage: ModelUsage) -> None:
        self.model = model
        self.calls += 1
        self.usage = self.usage + usage

    def to_json(self) -> dict:
        return {
            "model": self.model,
            "prompt_version": MARKET_PROMPT_VERSION,
            "calls": self.calls,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_tokens": self.usage.cache_read_tokens,
            "cache_write_tokens": self.usage.cache_write_tokens,
            "notes": self.notes,
        }


def _zone_lines(zones: Sequence[ZoneRef]) -> str:
    lines = []
    for zone in zones:
        also = f" (also: {', '.join(zone.aliases)})" if zone.aliases else ""
        lines.append(f"{zone.id}: {zone.name}{also}")
    return "\n".join(lines) or "(none)"


def render_sheet(sheet: Sheet, max_rows: int) -> str:
    lines = []
    for r, row in enumerate(sheet.rows[:max_rows]):
        cells = [
            f"c{c}={json.dumps(cell_text(v)[:CELL_CHARS], ensure_ascii=False)}"
            for c, v in enumerate(row)
            if cell_text(v)
        ]
        if cells:
            lines.append(f"r{r}: " + " | ".join(cells))
    shown = min(len(sheet.rows), max_rows)
    more = (
        f"\n(rows {shown}-{len(sheet.rows) - 1} are not shown; they follow the same layout)"
        if len(sheet.rows) > max_rows
        else ""
    )
    return "\n".join(lines) + more


def structure_prompt(
    sheet: Sheet,
    *,
    kind: str,
    source: str,
    allowed: Sequence[str],
    zones: Sequence[ZoneRef],
    municipality: str,
    max_rows: int,
) -> tuple[list[SystemBlock], str]:
    metrics = "\n".join(f"- {m}: {meaning}" for m, meaning in METRIC_MEANING.items())
    system = [SystemBlock(STRUCTURE_SYSTEM.format(metrics=metrics), cache=True)]
    user = (
        f"Import: {kind} figures from {source} for {municipality}.\n"
        f"Metrics this import may give: {', '.join(allowed)} (map any other metric to "
        f'"ignore" with reason "metric_not_for_kind").\n'
        f"Zones of {municipality} (to recognise area names):\n{_zone_lines(zones)}\n\n"
        f"Sheet {sheet.name!r}: {len(sheet.rows)} rows x {sheet.width} columns.\n"
        f"{render_sheet(sheet, max_rows)}"
    )
    return system, user


def places_prompt(
    names: Sequence[str],
    *,
    zones: Sequence[ZoneRef],
    municipality: str,
    municipality_names: Sequence[str],
) -> tuple[list[SystemBlock], str]:
    system = [
        SystemBlock(
            PLACES_SYSTEM.format(
                municipality=municipality,
                municipality_names=", ".join(municipality_names) or municipality,
            ),
            cache=True,
        )
    ]
    user = f"Zones of {municipality}:\n{_zone_lines(zones)}\n\nNames:\n" + "\n".join(
        f"- {json.dumps(n, ensure_ascii=False)}" for n in names
    )
    return system, user


def _parse(model_cls: type[BaseModel], data: dict) -> BaseModel:
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise ModelOutputInvalid(f"the mapping does not fit the schema: {exc}") from exc


def map_structure(
    model: StructuredModel,
    sheet: Sheet,
    index: int,
    *,
    kind: str,
    source: str,
    allowed: Sequence[str],
    zones: Sequence[ZoneRef],
    municipality: str,
    max_rows: int,
    log: LlmLog,
) -> tuple[SheetMapping, dict[int, Metric | None], set[int]]:
    """The model's reading of a sheet as a mapping (rows are placed afterwards), the metric of
    each row of a long table, and the rows it says are not data."""
    system, user = structure_prompt(
        sheet,
        kind=kind,
        source=source,
        allowed=allowed,
        zones=zones,
        municipality=municipality,
        max_rows=max_rows,
    )
    reply = model.complete(system=system, user=user, schema=STRUCTURE_SCHEMA)
    log.add(reply.model, reply.usage)
    out = _parse(StructureResponse, reply.data)
    assert isinstance(out, StructureResponse)
    width, height = sheet.width, len(sheet.rows)
    header_rows = sorted({r for r in out.header_rows if 0 <= r < height})
    columns: dict[int, ColumnRole] = {}
    for col in out.columns:
        if not 0 <= col.column < width or col.column in columns:
            log.notes.append(f"sheet {index}: column {col.column} dropped (out of range or twice)")
            continue
        header = " ".join(
            cell_text(sheet.cell(r, col.column))
            for r in header_rows
            if cell_text(sheet.cell(r, col.column))
        )
        role = col.role
        reason = col.reason
        metric = col.metric
        if role == "value" and metric is not None and metric not in allowed:
            role, reason = "ignore", "metric_not_for_kind"
        columns[col.column] = ColumnRole(
            column=col.column,
            role=role,
            metric=metric if role == "value" else None,
            bound=(col.bound or "expected") if role == "value" else None,
            unit_as_printed=col.unit_as_printed,
            period_as_printed=col.period_as_printed,
            header=header or None,
            reason=reason,
        )
    mapping = SheetMapping(
        sheet=index,
        header_rows=header_rows,
        columns=[columns[c] for c in sorted(columns)],
        table_geography=out.table_geography,
        table_period_as_printed=out.table_period_as_printed,
        table_unit_as_printed=out.table_unit_as_printed,
        method="llm",
        notes=out.notes,
    )
    row_metrics = {
        rm.row: (rm.metric if rm.metric in allowed else None)
        for rm in out.row_metrics
        if 0 <= rm.row < height
    }
    non_data = {r for r in out.non_data_rows if 0 <= r < height}
    return mapping, row_metrics, non_data


@dataclass(frozen=True, slots=True)
class AiPlace:
    applies_to: str
    zone_id: int | None
    confidence: float
    reason: str | None


def place_names(
    model: StructuredModel,
    names: Sequence[str],
    *,
    zones: Sequence[ZoneRef],
    municipality: str,
    municipality_names: Sequence[str],
    log: LlmLog,
) -> dict[str, AiPlace]:
    """The model's zone for each name; names it did not answer, or answered with an unknown zone,
    are absent (they stay unplaced)."""
    if not names:
        return {}
    system, user = places_prompt(
        names, zones=zones, municipality=municipality, municipality_names=municipality_names
    )
    reply = model.complete(system=system, user=user, schema=PLACES_SCHEMA)
    log.add(reply.model, reply.usage)
    out = _parse(PlacesResponse, reply.data)
    assert isinstance(out, PlacesResponse)
    known = {z.id for z in zones}
    wanted = set(names)
    places: dict[str, AiPlace] = {}
    for place in out.places:
        if place.name not in wanted or place.name in places:
            continue
        confidence = min(max(place.confidence, 0.0), 1.0)
        if place.applies_to == "zone" and place.zone_id not in known:
            log.notes.append(f"{place.name!r}: unknown zone id {place.zone_id} dropped")
            continue
        places[place.name] = AiPlace(
            place.applies_to,
            place.zone_id if place.applies_to == "zone" else None,
            confidence,
            place.reason,
        )
    return places
