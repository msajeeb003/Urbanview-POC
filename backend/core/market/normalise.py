"""Normalisation: from a table as read (or pasted listings) to zone-level market inputs.

For a table: the rules map each sheet (:mod:`core.market.rules`); the LLM maps what they cannot
(``MARKET_NORMALISE_LLM``: ``auto`` = only then, ``always``, ``never``) and places the area names
they cannot (:mod:`core.market.llm_map`); then code reads every figure from the mapped cells,
converts units to EUR per m² and picks, per zone and metric, one figure: a zone's own row before
a municipality-wide one, the latest period first. Every row, column or figure left out is in the
report with its reason; nothing is dropped silently.

Ranges, never invented: a stated range (low / high columns, or a range in one cell) is kept
(``stated``; no expected figure → the midpoint, flagged ``expected_midpoint``); a single figure
takes the configured range factors of the zone's current assumptions (else the municipality-wide
row's): ``derived``, flagged ``range_derived``; with none configured the range stays empty
(``unavailable``, flagged ``range_unavailable``) and approving needs a reviewer's amendment.
Listings give the median and the configured percentiles (``listings``, flagged
``asking_prices``). The AI interprets; every number is code arithmetic.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from core.extraction.llm import ModelError, StructuredModel
from core.market.listings import Listing, ListingBatch, percentile
from core.market.llm_map import (
    MARKET_PROMPT_VERSION,
    AiPlace,
    LlmLog,
    map_structure,
    place_names,
)
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
    SheetAnalysis,
    SheetReader,
    ZoneMatcher,
    allowed_metrics,
    column_skips,
    row_place,
)
from core.municipality import MarketProfile

LlmMode = Literal["auto", "never", "always"]
MIN_AI_CONFIDENCE = 0.6  # an AI placement below it stays unplaced


@dataclass(slots=True)
class NormaliseContext:
    municipality: str
    kind: ImportKind
    source: str
    retrieved_on: date
    zones: list[ZoneRef]
    profile: MarketProfile
    factors: dict[int | None, RangeFactors] = field(default_factory=dict)
    mode: LlmMode = "auto"
    low_confidence: float = 0.7
    llm_max_rows: int = 150


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


def _normaliser(log: LlmLog) -> str:
    return "rules" if log.calls == 0 else f"rules+llm:{log.model}@{MARKET_PROMPT_VERSION}"


class Normaliser:
    def __init__(self, ctx: NormaliseContext, model: StructuredModel | None = None) -> None:
        self.ctx = ctx
        self.model = model if ctx.mode != "never" else None
        self.rules = HeaderRules(ctx.profile)
        self.matcher = ZoneMatcher(ctx.zones, ctx.profile)
        self.allowed = allowed_metrics(ctx.profile, ctx.kind)
        self.reader = SheetReader(self.rules, self.matcher, self.allowed)
        self.log = LlmLog()
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
        self._place_unresolved(mappings)
        candidates: list[Candidate] = []
        for mapping in mappings:
            candidates.extend(self._candidates(table.sheets[mapping.sheet], mapping))
        inputs = self._choose(candidates)
        return NormaliseResult(
            inputs=inputs,
            skipped=self.skipped,
            mappings=mappings,
            normaliser=_normaliser(self.log),
            issues=self.issues,
            llm=self.log.to_json() if self.log.calls else None,
        )

    def _map_sheet(self, sheet: Sheet, index: int) -> SheetMapping | None:
        analysis = self.reader.map_sheet(sheet, index)
        rules_skipped = analysis.skipped
        mapping = analysis.mapping
        if self.model is not None and (mapping is None or self.ctx.mode == "always"):
            try:
                llm_mapping, row_metrics, non_data = map_structure(
                    self.model,
                    sheet,
                    index,
                    kind=self.ctx.kind,
                    source=self.ctx.source,
                    allowed=self.allowed,
                    zones=self.ctx.zones,
                    municipality=self.ctx.municipality,
                    max_rows=self.ctx.llm_max_rows,
                    log=self.log,
                )
            except ModelError as exc:
                self.issues.append(f"sheet {sheet.name!r}: the LLM could not map it ({exc})")
            else:
                if any(c.role == "value" for c in llm_mapping.columns):
                    sub = SheetAnalysis(None)
                    first = max(llm_mapping.header_rows, default=-1) + 1
                    llm_mapping.rows = self.reader.place_rows(
                        sheet,
                        index,
                        llm_mapping,
                        first,
                        sub,
                        row_metrics=row_metrics,
                        skip_rows=non_data,
                    )
                    rules_skipped = sub.skipped
                    mapping = llm_mapping
                else:
                    self.issues.append(f"sheet {sheet.name!r}: the LLM found no figure column")
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
        self.skipped.extend(rules_skipped)
        return mapping

    def _place_unresolved(self, mappings: list[SheetMapping]) -> None:
        names = sorted(
            {
                row.geography_as_printed
                for m in mappings
                for row in m.rows
                if row.applies_to == "none" and row.geography_as_printed
            }
        )
        placed = self._ask_places(names)
        for m in mappings:
            changed = False
            for i, row in enumerate(m.rows):
                ai = placed.get(row.geography_as_printed or "")
                if row.applies_to != "none" or ai is None:
                    continue
                m.rows[i] = _ai_row(row, ai)
                changed = changed or m.rows[i].method == "llm"
            if changed and m.method == "rules":
                m.method = "rules+llm"

    def _ask_places(self, names: list[str]) -> dict[str, AiPlace]:
        if not names or self.model is None:
            return {}
        try:
            return place_names(
                self.model,
                names,
                zones=self.ctx.zones,
                municipality=self.ctx.municipality,
                municipality_names=self.ctx.profile.municipality_names,
                log=self.log,
            )
        except ModelError as exc:
            self.issues.append(f"the LLM could not place {len(names)} area names ({exc})")
            return {}

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
        normaliser = _normaliser(self.log)
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
            built = self._build(winner, zone_id, normaliser)
            if built is not None:
                inputs.append(built)
        return inputs

    def _build(self, c: Candidate, zone_id: int, normaliser: str) -> MarketInput | None:
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
        if c.place.method == "llm":
            flags.append("zone_mapped_by_ai")
        if c.structure == "llm":
            flags.append("columns_mapped_by_ai")
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
            normaliser=normaliser,
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

    # --- listings ---------------------------------------------------------------------------------

    def listings(
        self,
        batch: ListingBatch,
        *,
        metric: Metric,
        min_listings: int,
        low_percentile: float,
        high_percentile: float,
    ) -> NormaliseResult:
        self.skipped.extend(batch.skipped)
        places = {
            listing.line: row_place(
                listing.line, listing.location, self.matcher.match(listing.location)
            )
            for listing in batch.listings
        }
        unresolved = {p.geography_as_printed for p in places.values() if p.applies_to == "none"}
        ai = self._ask_places(sorted(n for n in unresolved if n))
        by_zone: dict[int, list[tuple[Listing, float, str]]] = defaultdict(list)
        plausible = self.ctx.profile.plausible_eur_m2.get(metric)
        for listing in batch.listings:
            place = places[listing.line]
            if place.applies_to == "none" and listing.location in ai:
                place = _ai_row(place, ai[listing.location])
            zone_id = place.zone_id
            if place.applies_to != "zone" or zone_id is None:
                why = {"municipality": "not_zone_specific", "other": "other_area"}.get(
                    place.applies_to, "no_zone_match"
                )
                self.skipped.append(Skipped(why, row=listing.line, detail=listing.location))
                continue
            confidence, method = place.confidence, place.method
            if plausible and not plausible[0] <= listing.price_eur_m2 <= plausible[1]:
                self.skipped.append(
                    Skipped(
                        "implausible_price",
                        row=listing.line,
                        detail=f"{listing.price_eur_m2} EUR/m² ({listing.location})",
                    )
                )
                continue
            by_zone[zone_id].append((listing, confidence, method))
        normaliser = _normaliser(self.log)
        names = {z.id: z.name for z in self.ctx.zones}
        inputs = []
        for zone_id, entries in sorted(by_zone.items()):
            if len(entries) < min_listings:
                self.skipped.append(
                    Skipped(
                        "too_few_listings",
                        detail=f"{names.get(zone_id, zone_id)}: {len(entries)} listings, "
                        f"{min_listings} needed",
                    )
                )
                continue
            prices = sorted(e[0].price_eur_m2 for e in entries)
            dates = sorted(e[0].listed_on for e in entries)
            expected = round(percentile(prices, 50), 2)
            low = round(percentile(prices, low_percentile), 2)
            high = round(percentile(prices, high_percentile), 2)
            ai_placed = sum(1 for e in entries if e[2] == "llm")
            confidence = round(min(e[1] for e in entries), 3)
            flags = ["asking_prices"]
            if ai_placed:
                flags.append("zone_mapped_by_ai")
            self._quality_flags(metric, expected, low, high, confidence, flags)
            inputs.append(
                MarketInput(
                    zone_id=zone_id,
                    metric=metric,
                    expected=expected,
                    low=low,
                    high=high,
                    range_basis="listings",
                    source=self.ctx.source,
                    source_date=self._as_of(dates[-1]),
                    confidence=confidence,
                    notes=(
                        f"{len(prices)} asking prices listed {dates[0].isoformat()} to "
                        f"{dates[-1].isoformat()}: median, P{low_percentile:g} to "
                        f"P{high_percentile:g}"
                    ),
                    flags=flags,
                    raw={"listings": [e[0].to_json() for e in entries]},
                    mapping={
                        "method": "listings",
                        "listings": len(prices),
                        "ai_placed": ai_placed,
                        "percentiles": [low_percentile, 50, high_percentile],
                        "min": prices[0],
                        "max": prices[-1],
                    },
                    normaliser=normaliser,
                )
            )
        return NormaliseResult(
            inputs=inputs,
            skipped=self.skipped,
            mappings=[],
            normaliser=normaliser,
            issues=self.issues,
            llm=self.log.to_json() if self.log.calls else None,
        )


def _ai_row(row: RowPlace, ai: AiPlace) -> RowPlace:
    if ai.applies_to == "none" or (ai.applies_to == "zone" and ai.confidence < MIN_AI_CONFIDENCE):
        reason = f"the LLM could not place it: {ai.reason}" if ai.reason else "ai_unsure"
        return row.model_copy(update={"reason": reason})
    return row.model_copy(
        update={
            "applies_to": ai.applies_to,
            "zone_id": ai.zone_id,
            "confidence": ai.confidence,
            "method": "llm",
            "reason": ai.reason,
        }
    )


def normalise_table(
    table: RawTable, ctx: NormaliseContext, model: StructuredModel | None = None
) -> NormaliseResult:
    return Normaliser(ctx, model).table(table)


def normalise_listings(
    batch: ListingBatch,
    ctx: NormaliseContext,
    *,
    metric: Metric = "sale_rate",
    min_listings: int = 5,
    low_percentile: float = 25,
    high_percentile: float = 75,
    model: StructuredModel | None = None,
) -> NormaliseResult:
    return Normaliser(ctx, model).listings(
        batch,
        metric=metric,
        min_listings=min_listings,
        low_percentile=low_percentile,
        high_percentile=high_percentile,
    )
