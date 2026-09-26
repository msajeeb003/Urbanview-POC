"""Rule-based reading of market tables: which rows are the header, what each column means and
which zone each row is, from the words of the profile's ``[market]`` table.

``map_sheet`` returns a :class:`~core.market.model.SheetMapping` (or ``None`` when the layout
is not recognised: then the LLM step maps it, or the sheet is reported). Rows whose area the rules
cannot place stay in the mapping with ``applies_to = "none"`` so the LLM step can place them.
The rules never read a figure; :mod:`core.market.normalise` does, from the mapping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.market.model import (
    METRICS,
    Bound,
    ColumnRole,
    Metric,
    RowPlace,
    Sheet,
    SheetMapping,
    Skipped,
    ZoneRef,
)
from core.market.parse import cell_text, fold_words, looks_numeric, parse_period
from core.municipality import MarketProfile

HEADER_SCAN_ROWS = 25
# metrics are tested in this order: "cijena građevinskog zemljišta" is a land figure, "cijena
# gradnje" a construction figure; only what is neither is a selling price
METRIC_ORDER: tuple[Metric, ...] = ("land_rate", "design_rate", "build_rate", "sale_rate")


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in patterns]


def _any(patterns: list[re.Pattern[str]], folded: str) -> bool:
    return any(p.search(folded) for p in patterns)


def area_key(text: str) -> str:
    """An area name for comparison: accent-folded, lower case, punctuation as spaces."""
    return " ".join(re.sub(r"[^\w]+", " ", fold_words(text)).split())


class HeaderRules:
    """The profile's header words as compiled patterns."""

    def __init__(self, profile: MarketProfile) -> None:
        h = profile.headers
        self.geography = _compile(h.geography)
        self.metrics = {m: _compile(getattr(h, m)) for m in METRIC_ORDER}
        self.bounds: dict[Bound, list[re.Pattern[str]]] = {
            "low": _compile(h.low),
            "high": _compile(h.high),
            "expected": _compile(h.expected),
        }
        self.period = _compile(h.period)
        self.ignore = _compile(h.ignore)
        self.thousands = list(h.thousands)
        self.periods = profile.periods

    def metric(self, text: str) -> Metric | None:
        folded = fold_words(text)
        return next((m for m in METRIC_ORDER if _any(self.metrics[m], folded)), None)

    def bound(self, text: str) -> Bound | None:
        folded = fold_words(text)
        found = [b for b, patterns in self.bounds.items() if _any(patterns, folded)]
        if "low" in found and "high" in found:  # "od – do": the cells hold ranges
            return None
        for b in ("low", "high", "expected"):
            if b in found:
                return b
        return None

    def is_geography(self, text: str) -> bool:
        return _any(self.geography, fold_words(text))

    def is_period(self, text: str) -> bool:
        return _any(self.period, fold_words(text))

    def is_ignored(self, text: str) -> bool:
        return _any(self.ignore, fold_words(text))


@dataclass(frozen=True, slots=True)
class Place:
    applies_to: str  # zone | municipality | other | none
    zone_id: int | None = None
    method: str = "exact"
    confidence: float = 1.0
    reason: str | None = None


class ZoneMatcher:
    """Area names to zones: zone names and aliases matched whole (accent-folded), the names that
    mean the whole municipality, and a name that contains exactly one zone name."""

    def __init__(self, zones: list[ZoneRef], profile: MarketProfile) -> None:
        self.zones = {z.id: z for z in zones}
        self.lookup: dict[str, tuple[int, str]] = {}
        by_name = {area_key(z.name): z.id for z in zones}
        for zone in zones:
            self.lookup.setdefault(area_key(zone.name), (zone.id, "exact"))
            for alias in zone.aliases:
                self.lookup.setdefault(area_key(alias), (zone.id, "alias"))
        for zone_name, aliases in profile.zone_aliases.items():
            zone_id = by_name.get(area_key(zone_name))
            if zone_id is None:  # an alias of a zone this database does not have
                continue
            for alias in aliases:
                self.lookup.setdefault(area_key(alias), (zone_id, "alias"))
        self.lookup.pop("", None)
        self.municipality = {area_key(n) for n in profile.municipality_names} - {""}

    def zones_in(self, text: str) -> set[int]:
        hay = f" {area_key(text)} "
        return {zid for key, (zid, _) in self.lookup.items() if f" {key} " in hay}

    def match(self, text: str | None) -> Place:
        key = area_key(text or "")
        if not key:
            return Place("none", method="exact", confidence=0.0, reason="no_area_name")
        if key in self.lookup:
            zone_id, method = self.lookup[key]
            return Place("zone", zone_id, method, 1.0 if method == "exact" else 0.95)
        if key in self.municipality:
            return Place("municipality", None, "municipality", 1.0)
        contained = self.zones_in(text or "")
        if len(contained) == 1:
            zone_id = next(iter(contained))
            return Place(
                "zone",
                zone_id,
                "alias",
                0.85,
                reason=f"the name contains the zone name {self.zones[zone_id].name!r}",
            )
        if len(contained) > 1:
            return Place("none", method="exact", confidence=0.0, reason="several_zones_named")
        hay = f" {key} "
        if any(f" {name} " in hay for name in self.municipality):
            return Place("municipality", None, "municipality", 0.9, reason="names the municipality")
        return Place("none", method="exact", confidence=0.0, reason="no_zone_match")


@dataclass(slots=True)
class SheetAnalysis:
    mapping: SheetMapping | None
    skipped: list[Skipped] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    first_data_row: int | None = None


def row_place(row: int, text: str | None, place: Place, **extra: object) -> RowPlace:
    return RowPlace(
        row=row,
        geography_as_printed=text,
        applies_to=place.applies_to,  # type: ignore[arg-type]
        zone_id=place.zone_id,
        confidence=place.confidence,
        method=place.method,  # type: ignore[arg-type]
        reason=place.reason,
        **extra,  # type: ignore[arg-type]
    )


class SheetReader:
    """Rule-based mapping of one sheet."""

    def __init__(
        self, rules: HeaderRules, matcher: ZoneMatcher, allowed_metrics: tuple[str, ...]
    ) -> None:
        self.rules = rules
        self.matcher = matcher
        self.allowed = allowed_metrics

    # --- header -----------------------------------------------------------------------------------

    def _header_like(self, texts: list[str], numeric: list[bool]) -> bool:
        cells = [t for t in texts if t]
        distinct = set(cells)
        n_numeric = sum(numeric)
        if len(distinct) < 2 or len(cells) - n_numeric < n_numeric:
            return False
        rules = self.rules
        return any(
            rules.metric(t) or rules.bound(t) or rules.is_geography(t) or rules.is_period(t)
            for t in distinct
        )

    def header_block(self, sheet: Sheet) -> tuple[list[int], int] | None:
        """(header rows, first data row) or None."""
        texts = [[cell_text(c) for c in row] for row in sheet.rows]
        numeric = [[looks_numeric(c) for c in row] for row in sheet.rows]
        for h in range(min(HEADER_SCAN_ROWS, len(sheet.rows))):
            if not self._header_like(texts[h], numeric[h]):
                continue
            rows = [h]
            r = h + 1
            while r < len(sheet.rows) and not any(numeric[r]):
                if any(texts[r]):
                    rows.append(r)
                r += 1
                if len(rows) > 3:
                    break
            if r < len(sheet.rows) and any(numeric[r]):
                return rows, r
        return None

    @staticmethod
    def column_headers(sheet: Sheet, header_rows: list[int]) -> list[str]:
        width = sheet.width
        layers = []
        for i, r in enumerate(header_rows):
            texts = [cell_text(sheet.cell(r, c)) for c in range(width)]
            lower = header_rows[i + 1 :]
            last = ""
            for c in range(width):
                if texts[c]:
                    last = texts[c]
                elif last and any(cell_text(sheet.cell(lr, c)) for lr in lower):
                    texts[c] = last  # a spanning header cell of a CSV export
            layers.append(texts)
        headers = []
        for c in range(width):
            parts: list[str] = []
            for layer in layers:
                if layer[c] and layer[c] not in parts:
                    parts.append(layer[c])
            headers.append(" ".join(parts))
        return headers

    # --- mapping ----------------------------------------------------------------------------------

    def map_sheet(self, sheet: Sheet, index: int) -> SheetAnalysis:
        block = self.header_block(sheet)
        if block is None:
            return SheetAnalysis(None, issues=[f"sheet {sheet.name!r}: no header row recognised"])
        header_rows, first_data = block
        headers = self.column_headers(sheet, header_rows)
        preamble = " ".join(
            cell_text(c) for r in range(header_rows[0]) for c in sheet.rows[r] if cell_text(c)
        )
        title_metric = self.rules.metric(preamble) if preamble else None
        data_rows = [
            r
            for r in range(first_data, len(sheet.rows))
            if any(looks_numeric(c) for c in sheet.rows[r])
        ]
        analysis = SheetAnalysis(None, first_data_row=first_data)
        columns: list[ColumnRole] = []
        geography_col: int | None = None
        label_col: int | None = None
        period_col: int | None = None
        for c, header in enumerate(headers):
            role = self._column_role(sheet, c, header, data_rows, title_metric)
            if role.role == "geography":
                if geography_col is not None:
                    role = ColumnRole(
                        column=c, role="ignore", header=header, reason="second_area_column"
                    )
                else:
                    geography_col = c
            elif role.role == "metric_label":
                if label_col is not None:
                    role = ColumnRole(
                        column=c, role="ignore", header=header, reason="second_label_column"
                    )
                else:
                    label_col = c
            elif role.role == "period" and period_col is None:
                period_col = c
            columns.append(role)
        if geography_col is None:
            geography_col = self._unlabelled_area_column(sheet, columns, data_rows)
            if geography_col is not None:
                columns[geography_col] = ColumnRole(
                    column=geography_col, role="geography", header=headers[geography_col] or None
                )
        for i, col in enumerate(columns):
            if col.role != "value":
                continue
            if col.metric is None and label_col is None:
                columns[i] = col.model_copy(update={"role": "ignore", "reason": "no_metric"})
            elif col.metric is not None and col.metric not in self.allowed:
                columns[i] = col.model_copy(
                    update={"role": "ignore", "reason": "metric_not_for_kind"}
                )
        values = [c for c in columns if c.role == "value"]
        if not values:
            analysis.issues.append(f"sheet {sheet.name!r}: no figure column recognised")
            return analysis
        table_geography = None
        if geography_col is None:
            place = self.matcher.match(preamble) if preamble else Place("none")
            if place.applies_to == "none" and preamble:
                zones = self.matcher.zones_in(preamble)
                if len(zones) == 1:
                    place = Place("zone", next(iter(zones)), "alias", 0.85)
            if place.applies_to == "none":
                analysis.issues.append(f"sheet {sheet.name!r}: no area column recognised")
                return analysis
            table_geography = preamble
        table_period = parse_period(preamble, self.rules.periods) if preamble else None
        mapping = SheetMapping(
            sheet=index,
            header_rows=header_rows,
            columns=columns,
            table_geography=table_geography,
            table_period_as_printed=table_period.label if table_period else None,
            table_unit_as_printed=preamble or None,
            method="rules",
        )
        mapping.rows = self.place_rows(sheet, index, mapping, first_data, analysis)
        analysis.mapping = mapping
        return analysis

    def _column_role(
        self,
        sheet: Sheet,
        c: int,
        header: str,
        data_rows: list[int],
        title_metric: Metric | None,
    ) -> ColumnRole:
        rules = self.rules
        cells = [sheet.cell(r, c) for r in data_rows]
        filled = [x for x in cells if cell_text(x)]
        if not filled:
            return ColumnRole(column=c, role="ignore", header=header or None, reason="empty")
        if header and rules.is_ignored(header):
            return ColumnRole(column=c, role="ignore", header=header, reason="header_ignored")
        numeric_share = sum(looks_numeric(x) for x in filled) / len(filled)
        period = parse_period(header, rules.periods) if header else None
        if header and rules.is_period(header) and not rules.metric(header):
            if all(parse_period(cell_text(x), rules.periods) for x in filled):
                return ColumnRole(column=c, role="period", header=header)
        if numeric_share >= 0.5:
            metric = rules.metric(header) if header else None
            bound = rules.bound(header) if header else None
            if metric is None and period is not None and title_metric is not None:
                metric = title_metric  # a time series: the title names the metric
            if metric is None and bound is None and period is None:
                return ColumnRole(
                    column=c, role="ignore", header=header or None, reason="no_metric"
                )
            return ColumnRole(
                column=c,
                role="value",
                metric=metric,
                bound=bound or "expected",
                unit_as_printed=header or None,
                period_as_printed=period.label if period else None,
                header=header or None,
            )
        texts = [cell_text(x) for x in filled]
        if header and rules.is_geography(header):
            return ColumnRole(column=c, role="geography", header=header)
        if sum(1 for t in texts if parse_period(t, rules.periods)) >= len(texts) / 2:
            return ColumnRole(column=c, role="period", header=header or None)
        if sum(1 for t in texts if rules.metric(t)) >= len(texts) / 2:
            return ColumnRole(column=c, role="metric_label", header=header or None)
        return ColumnRole(column=c, role="ignore", header=header or None, reason="text")

    def _unlabelled_area_column(
        self, sheet: Sheet, columns: list[ColumnRole], data_rows: list[int]
    ) -> int | None:
        """A text column without a recognised header whose cells name zones or the municipality."""
        for col in columns:
            if col.role != "ignore" or col.reason != "text":
                continue
            names = [cell_text(sheet.cell(r, col.column)) for r in data_rows]
            if any(self.matcher.match(n).applies_to != "none" for n in names if n):
                return col.column
        return None

    def place_rows(
        self,
        sheet: Sheet,
        index: int,
        mapping: SheetMapping,
        first_data: int,
        analysis: SheetAnalysis,
        *,
        row_metrics: dict[int, Metric | None] | None = None,
        skip_rows: set[int] | None = None,
    ) -> list[RowPlace]:
        """Every data row with its area placed by the rules (``none`` when they cannot), its
        metric (long tables: the label cell, or ``row_metrics`` from the LLM) and period."""
        geography = next((c.column for c in mapping.columns if c.role == "geography"), None)
        label = next((c.column for c in mapping.columns if c.role == "metric_label"), None)
        period = next((c.column for c in mapping.columns if c.role == "period"), None)
        value_cols = [c.column for c in mapping.columns if c.role == "value"]
        places: list[RowPlace] = []
        for r in range(first_data, len(sheet.rows)):
            texts = [cell_text(x) for x in sheet.rows[r]]
            if not any(texts):
                continue
            if (skip_rows and r in skip_rows) or not any(
                looks_numeric(sheet.cell(r, c)) for c in value_cols
            ):
                detail = " | ".join(t for t in texts if t)[:200]
                analysis.skipped.append(Skipped("not_data", sheet=index, row=r, detail=detail))
                continue
            if geography is not None:
                name = cell_text(sheet.cell(r, geography)) or None
                place = self.matcher.match(name)
            else:
                name = mapping.table_geography
                place = self.matcher.match(name)
                if place.applies_to == "none":
                    zones = self.matcher.zones_in(name or "")
                    if len(zones) == 1:
                        place = Place("zone", next(iter(zones)), "table", 0.85)
                place = Place(
                    place.applies_to, place.zone_id, "table", place.confidence, place.reason
                )
            metric = None
            if row_metrics and r in row_metrics:
                metric = row_metrics[r]
            elif label is not None:
                metric = self.rules.metric(cell_text(sheet.cell(r, label)))
            period_text = cell_text(sheet.cell(r, period)) if period is not None else None
            places.append(
                row_place(r, name, place, metric=metric, period_as_printed=period_text or None)
            )
        return places


def allowed_metrics(profile: MarketProfile, kind: str) -> tuple[str, ...]:
    return tuple(profile.kind_metrics.get(kind) or METRICS)


def column_skips(mapping: SheetMapping) -> list[Skipped]:
    """The columns a mapping leaves out, for the report (empty columns are not worth a line)."""
    return [
        Skipped(col.reason or "ignored", sheet=mapping.sheet, column=col.column, detail=col.header)
        for col in mapping.columns
        if col.role == "ignore" and col.reason != "empty"
    ]
