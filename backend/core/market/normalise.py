"""Normalisation: from a table as read to zone-level market inputs.

For a table: the rules map each sheet (:mod:`core.market.rules`, the profile's ``[market]``
words; a sheet or an area name they cannot map is skipped with its reason); code reads every
figure from the mapped cells, converts units to EUR per m² and picks, per zone and metric, one
figure: a zone's own row before a municipality-wide one, the latest period first. Every row,
column or figure left out is in the report with its reason; nothing is dropped silently.

Ranges, never invented: a stated range (low / high columns, or a range in one cell) is kept
(``stated``; no expected figure → the midpoint, flagged ``expected_midpoint``); a single figure
takes the configured range factors of the zone's current assumptions (else the municipality-wide
row's): ``derived``, flagged ``range_derived``; with none configured the range stays empty
(``unavailable``, flagged ``range_unavailable``) and approving needs a reviewer's amendment.
No AI: every mapping is a rule, every number is code arithmetic.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from core.market.model import (
    Bound,
    ColumnRole,
    ImportKind,
    MarketInput,
    Metric,
    NormaliseResult,
    RangeFactors,
    RawTable,
    RowPlace,
    Sheet,
    SheetMapping,
    Skipped,
    ZoneRef,
)
from core.market.parse import (
    Amount,
    Period,
    UnitInfo,
    cell_text,
    merge_units,
    parse_amount,
    parse_period,
    parse_unit,
)
from core.market.rules import (
    HeaderRules,
    SheetReader,
    ZoneMatcher,
    allowed_metrics,
    column_skips,
)
from core.municipality import MarketProfile


@dataclass(slots=True)
class NormaliseContext:
    municipality: str
    kind: ImportKind
    source: str
    retrieved_on: date
    zones: list[ZoneRef]
    profile: MarketProfile
    factors: dict[int | None, RangeFactors] = field(default_factory=dict)
    low_confidence: float = 0.7


@dataclass(slots=True)
class Candidate:
    sheet: int
    sheet_name: str
    row: int
    cells: list
    zone_ids: list[int]
    scope: str  # zone | municipality
    metric: Metric
    values: dict[str, float]
    range_in_cell: bool
    unit: UnitInfo
    period: Period | None
    place: RowPlace
    columns: dict[Bound, tuple[ColumnRole, str]]
    structure: str


NORMALISER = "rules"


class Normaliser:
    def __init__(self, ctx: NormaliseContext) -> None:
        self.ctx = ctx
        self.rules = HeaderRules(ctx.profile)
        self.matcher = ZoneMatcher(ctx.zones, ctx.profile)
        self.allowed = allowed_metrics(ctx.profile, ctx.kind)
        self.reader = SheetReader(self.rules, self.matcher, self.allowed)
        self.skipped: list[Skipped] = []
        self.issues: list[str] = []

    # --- tables -----------------------------------------------------------------------------------

    def table(self, table: RawTable) -> NormaliseResult:
        mappings: list[SheetMapping] = []
        for index, sheet in enumerate(table.sheets):
            mapping = self._map_sheet(sheet, index)
            if mapping is not None:
                mappings.append(mapping)
                self.skipped.extend(column_skips(mapping))
        candidates: list[Candidate] = []
        for mapping in mappings:
            candidates.extend(self._candidates(table.sheets[mapping.sheet], mapping))
        inputs = self._choose(candidates)
        return NormaliseResult(
            inputs=inputs,
            skipped=self.skipped,
            mappings=mappings,
            normaliser=NORMALISER,
            issues=self.issues,
        )

    def _map_sheet(self, sheet: Sheet, index: int) -> SheetMapping | None:
        analysis = self.reader.map_sheet(sheet, index)
        mapping = analysis.mapping
        if mapping is None:
            self.issues.extend(analysis.issues)
            self.skipped.append(
                Skipped(
                    "structure_not_recognised",
                    sheet=index,
                    detail="; ".join(analysis.issues) or sheet.name,
                )
            )
            return None
        self.skipped.extend(analysis.skipped)
        return mapping

    def _candidates(self, sheet: Sheet, mapping: SheetMapping) -> list[Candidate]:
        values = [c for c in mapping.columns if c.role == "value"]
        thousands = self.rules.thousands
        table_unit = parse_unit(mapping.table_unit_as_printed, thousands=thousands)
        table_period = parse_period(mapping.table_period_as_printed, self.rules.periods)
        all_zones = [z.id for z in self.ctx.zones]
        out: list[Candidate] = []
        for place in mapping.rows:
            where = place.geography_as_printed
            if place.applies_to in ("other", "none"):
                reason = "other_area" if place.applies_to == "other" else "no_zone_match"
                detail = f"{where!r}: {place.reason}" if place.reason else repr(where)
                self.skipped.append(
                    Skipped(reason, sheet=mapping.sheet, row=place.row, detail=detail)
                )
                continue
            groups: dict[tuple[str, str | None], dict[Bound, tuple[ColumnRole, Amount, str]]] = (
                defaultdict(dict)
            )
            for col in values:
                metric = col.metric or place.metric
                cell = sheet.cell(place.row, col.column)
                text = cell_text(cell)
                if not text:
                    continue
                if metric is None:
                    self.skipped.append(
                        Skipped(
                            "metric_unknown",
                            sheet=mapping.sheet,
                            row=place.row,
                            column=col.column,
                            detail=text,
                        )
                    )
                    continue
                if metric not in self.allowed:
                    self.skipped.append(
                        Skipped(
                            "metric_not_for_kind",
                            sheet=mapping.sheet,
                            row=place.row,
                            column=col.column,
                            detail=metric,
                        )
                    )
                    continue
                amount = parse_amount(cell)
                if amount is None:
                    self.skipped.append(
                        Skipped(
                            "unreadable_value",
                            sheet=mapping.sheet,
                            row=place.row,
                            column=col.column,
                            detail=text,
                        )
                    )
                    continue
                bound: Bound = col.bound or "expected"
                slot = groups[(metric, col.period_as_printed)]
                if bound in slot:
                    self.skipped.append(
                        Skipped(
                            "ambiguous_columns",
                            sheet=mapping.sheet,
                            row=place.row,
                            column=col.column,
                            detail=f"a second {bound} figure for {metric}",
                        )
                    )
                    continue
                slot[bound] = (col, amount, text)
            for (metric, column_period), slot in groups.items():
                candidate = self._candidate(
                    sheet,
                    mapping,
                    place,
                    metric,
                    column_period,
                    slot,
                    table_unit,
                    table_period,
                    all_zones,
                )
                if candidate is not None:
                    out.append(candidate)
        return out

    def _candidate(
        self,
        sheet: Sheet,
        mapping: SheetMapping,
        place: RowPlace,
        metric: Metric,
        column_period: str | None,
        slot: dict[Bound, tuple[ColumnRole, Amount, str]],
        table_unit: UnitInfo,
        table_period: Period | None,
        all_zones: list[int],
    ) -> Candidate | None:
        numbers: dict[str, float] = {}
        range_in_cell = False
        for bound, (_, amount, _) in slot.items():
            if amount.is_range:
                range_in_cell = True
                numbers.setdefault("low", amount.values[0])
                numbers.setdefault("high", amount.values[1])
            else:
                numbers[bound] = amount.values[0]
        first_col = next(iter(slot.values()))[0]
        unit = merge_units(
            *(
                parse_unit(col.unit_as_printed or col.header, thousands=self.rules.thousands)
                for col, _, _ in slot.values()
            ),
            table_unit,
        )
        if unit.currency == "other":
            self.skipped.append(
                Skipped(
                    "currency_not_eur",
                    sheet=mapping.sheet,
                    row=place.row,
                    column=first_col.column,
                    detail=unit.text[:200],
                )
            )
            return None
        factor = unit.to_eur_m2
        numbers = {b: round(v * factor, 4) for b, v in numbers.items()}
        periods = self.rules.periods
        period = (
            parse_period(place.period_as_printed, periods)
            or parse_period(column_period, periods)
            or table_period
        )
        zone_ids = [place.zone_id] if place.applies_to == "zone" and place.zone_id else all_zones
        return Candidate(
            sheet=mapping.sheet,
            sheet_name=sheet.name,
            row=place.row,
            cells=list(sheet.rows[place.row]),
            zone_ids=zone_ids,
            scope="zone" if place.applies_to == "zone" else "municipality",
            metric=metric,
            values=numbers,
            range_in_cell=range_in_cell,
            unit=unit,
            period=period,
            place=place,
            columns={b: (col, text) for b, (col, _, text) in slot.items()},
            structure=mapping.method,
        )

    def _choose(self, candidates: list[Candidate]) -> list[MarketInput]:
        by_key: dict[tuple[int, str], list[Candidate]] = defaultdict(list)
        for candidate in candidates:
            for zone_id in candidate.zone_ids:
                by_key[(zone_id, candidate.metric)].append(candidate)
        inputs = []
        for (zone_id, metric), group in sorted(by_key.items()):
            ranked = sorted(
                group,
                key=lambda c: (
                    c.scope != "zone",
                    -(c.period.reference_date.toordinal() if c.period else 0),
                    c.sheet,
                    c.row,
                ),
            )
            winner = ranked[0]
            for other in ranked[1:]:
                if winner.scope == "zone" and other.scope == "municipality":
                    reason = "zone_figure_preferred"
                elif winner.period and (
                    other.period is None
                    or other.period.reference_date < winner.period.reference_date
                ):
                    reason = "older_period"
                else:
                    reason = "duplicate"
                self.skipped.append(
                    Skipped(
                        reason, sheet=other.sheet, row=other.row, detail=f"zone {zone_id} {metric}"
                    )
                )
            built = self._build(winner, zone_id)
            if built is not None:
                inputs.append(built)
        return inputs

    def _build(self, c: Candidate, zone_id: int) -> MarketInput | None:
        low, expected, high = c.values.get("low"), c.values.get("expected"), c.values.get("high")
        flags: list[str] = []
        if expected is None and low is not None and high is not None:
            expected = round((low + high) / 2, 2)
            flags.append("expected_midpoint")
        if expected is None:
            self.skipped.append(
                Skipped(
                    "no_expected_value",
                    sheet=c.sheet,
                    row=c.row,
                    detail=f"zone {zone_id} {c.metric}: only one bound",
                )
            )
            return None
        low, high, basis, range_info = self.complete_range(zone_id, expected, low, high, flags)
        if c.scope == "municipality":
            flags.append("municipality_level")
        if c.range_in_cell:
            flags.append("range_in_one_cell")
        if not (c.unit.per or c.unit.currency):
            flags.append("unit_assumed_eur_m2")
        if c.unit.to_eur_m2 != 1.0:
            flags.append("unit_converted")
        if c.period is None:
            flags.append("date_is_retrieval")
        confidence = round(min(max(c.place.confidence, 0.0), 1.0), 3)
        self._quality_flags(c.metric, expected, low, high, confidence, flags)
        headers = [col.header for col, _ in c.columns.values() if col.header]
        scope = "whole municipality" if c.scope == "municipality" else None
        period_label = c.period.label if c.period else None
        notes = "; ".join(
            dict.fromkeys(filter(None, [*headers, period_label, scope, c.place.reason]))
        )
        return MarketInput(
            zone_id=zone_id,
            metric=c.metric,
            expected=expected,
            low=low,
            high=high,
            range_basis=basis,
            source=self.ctx.source,
            source_date=self._as_of(c.period.reference_date if c.period else None),
            confidence=confidence,
            notes=notes or None,
            flags=flags,
            raw={
                "sheet": c.sheet_name,
                "sheet_index": c.sheet,
                "row": c.row,
                "cells": c.cells,
                "columns": {
                    bound: {"column": col.column, "header": col.header, "cell": text}
                    for bound, (col, text) in c.columns.items()
                },
                "unit_as_printed": c.unit.text or None,
                "period_as_printed": period_label,
            },
            mapping={
                "structure": c.structure,
                "method": c.place.method,
                "geography_as_printed": c.place.geography_as_printed,
                "applies_to": c.place.applies_to,
                "confidence": c.place.confidence,
                "reason": c.place.reason,
                "unit": {
                    "per": c.unit.per or "m2",
                    "currency": c.unit.currency or "EUR",
                    "scale": c.unit.scale,
                    "to_eur_m2": c.unit.to_eur_m2,
                },
                "range": range_info,
            },
            normaliser=NORMALISER,
        )

    def _as_of(self, day: date | None) -> date:
        """The date a figure refers to: its period's last day, but never after it was
        retrieved ("septembar 2026" read on the 24th is as of the 24th); no period = the
        retrieval date (flagged ``date_is_retrieval``)."""
        return min(day, self.ctx.retrieved_on) if day else self.ctx.retrieved_on

    def complete_range(
        self,
        zone_id: int,
        expected: float,
        low: float | None,
        high: float | None,
        flags: list[str],
    ) -> tuple[float | None, float | None, str, dict | None]:
        if low is not None and high is not None:
            return low, high, "stated", None
        if low is not None or high is not None:
            flags.append("range_incomplete")
            return low, high, "stated", None
        factors = self.ctx.factors.get(zone_id) or self.ctx.factors.get(None)
        if factors is None:
            flags.append("range_unavailable")
            return None, None, "unavailable", None
        flags.append("range_derived")
        info = {
            "from": "financial_assumptions",
            "assumption_id": factors.assumption_id,
            "zone_id": factors.zone_id,
            "low_factor": factors.low,
            "high_factor": factors.high,
        }
        return round(expected * factors.low, 2), round(expected * factors.high, 2), "derived", info

    def _quality_flags(
        self,
        metric: str,
        expected: float,
        low: float | None,
        high: float | None,
        confidence: float,
        flags: list[str],
    ) -> None:
        if (low is not None and low > expected) or (high is not None and high < expected):
            flags.append("bounds_inconsistent")
        plausible = self.ctx.profile.plausible_eur_m2.get(metric)
        if plausible and not plausible[0] <= expected <= plausible[1]:
            flags.append("implausible")
        if confidence < self.ctx.low_confidence:
            flags.append("low_confidence")


def normalise_table(table: RawTable, ctx: NormaliseContext) -> NormaliseResult:
    return Normaliser(ctx).table(table)
