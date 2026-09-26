"""Deterministic reading of market cells: numbers (``1.234,56``, ``1,234.56``, ``1 234``), ranges
in one cell (``1.200 – 1.500``, ``od 1.200 do 1.500``), units (EUR per m², per ha, per ar, in
thousands, another currency) and periods (``IV kvartal 2025``, ``Q3 2026``, ``I polugodište
2026``, ``septembar 2026``, ``2025``, ISO dates) turned into the reference date (the period's
last day). The LLM step may point at a cell or copy a label; the reading is always done here."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

from core.extraction.textmatch import words_fold
from core.market.model import Cell
from core.municipality import MarketPeriods

# A number token: grouped thousands (1.234.567,89 / 1,234 / 1 234) or plain (1234 / 1234,5).
_NUMBER = re.compile(r"\d{1,3}(?:[ .,'  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")
_RANGE_SEPARATOR = re.compile(r"^\s*(?:-|–|—|do|to|÷|/|bis)\s*$", re.IGNORECASE)
# digits that belong to a unit or a period label, not to the figure ("€/m2", "Q4", "H1")
_UNIT_TOKENS = re.compile(r"(?i)m\s*[2²]|m\^2|\bq[1-4]\b|\bh[12]\b")
_EMPTY_MARKERS = {"", "-", "–", "—", "…", "...", ".", "x", "n/a", "na", "nd", "n.d.", "*"}

ENGLISH_MONTHS = (
    "january february march april may june july august september october november december"
).split()


def fold_words(text: str) -> str:
    return words_fold(text)


def cell_text(cell: Cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell).strip()


def parse_number_token(token: str) -> float | None:
    compact = re.sub(r"[ '  ]", "", token)
    dots, commas = compact.count("."), compact.count(",")
    if dots and commas:
        decimal = "." if compact.rfind(".") > compact.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        compact = compact.replace(thousands, "").replace(decimal, ".")
    elif dots + commas > 1:
        compact = compact.replace(".", "").replace(",", "")
    elif dots + commas == 1:
        separator = "." if dots else ","
        whole, _, fraction = compact.partition(separator)
        if len(fraction) == 3 and 1 <= len(whole) <= 3 and whole != "0":
            compact = (
                whole + fraction
            )  # "1.620" / "1,620": thousands (3-decimal rates are not a thing)
        else:
            compact = whole + "." + fraction
    try:
        return float(compact)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Amount:
    """A figure read from one cell: one value, or a stated range (two values)."""

    values: tuple[float, ...]
    text: str

    @property
    def is_range(self) -> bool:
        return len(self.values) == 2


def parse_amount(cell: Cell) -> Amount | None:
    """The figure(s) of a cell, or None when the cell holds no figure (or an ambiguous one)."""
    if cell is None or isinstance(cell, bool):
        return None
    if isinstance(cell, int | float):
        return Amount((float(cell),), cell_text(cell))
    original = str(cell).strip()
    if original.lower() in _EMPTY_MARKERS:
        return None
    # blank the digits of units and period labels, keeping offsets for the range check
    text = _UNIT_TOKENS.sub(lambda m: " " * len(m.group()), original)
    values = []
    for m in _NUMBER.finditer(text):
        value = parse_number_token(m.group())
        if value is None:
            return None
        values.append((m, value))
    if len(values) == 1:
        return Amount((values[0][1],), original)
    if len(values) == 2:  # a range, or a figure next to a year ("1.850 (2025)"): ambiguous
        (m1, v1), (m2, v2) = values
        if _RANGE_SEPARATOR.match(text[m1.end() : m2.start()]):
            low, high = sorted((v1, v2))
            return Amount((low, high), original)
    return None


def looks_numeric(cell: Cell) -> bool:
    """A cell that is a figure (not a label that happens to contain a digit)."""
    if isinstance(cell, int | float) and not isinstance(cell, bool):
        return True
    if not isinstance(cell, str):
        return False
    letters = re.sub(r"(?i)eur|euro|€|m2|m²|ha|ar|od|do|to|km|/|\s", "", cell)
    return parse_amount(cell) is not None and not re.search(r"[^\W\d_]", letters)


# --- units ----------------------------------------------------------------------------------------

_OTHER_CURRENCY = re.compile(
    r"(\$|£|\busd\b|\brsd\b|\bdin(ar)?\b|\bhrk\b|\bkuna\b|\bbam\b|\bkm\b|\bchf\b|\bgbp\b)"
)
_EURO = re.compile(r"(€|\beur\b|\beura?\b|\beuro\w*\b)")
_PER_M2 = re.compile(r"(m2|m\^2|kvadrat|sq\.? ?m|\bsqm\b)")
_PER_HA = re.compile(r"(/ ?ha\b|\bpo ha\b|hektar|hectare|\bper ha\b)")
_PER_AR = re.compile(r"(/ ?ar\b|\bpo aru?\b|\bper are\b)")


@dataclass(frozen=True, slots=True)
class UnitInfo:
    """What a unit label says: the area the figure is per, the currency and a scale."""

    per: str | None  # m2 | ha | ar | None (not stated)
    currency: str | None  # EUR | other | None (not stated)
    scale: float = 1.0  # 1000 for "in thousands"
    text: str = ""

    @property
    def stated(self) -> bool:
        return self.per is not None or self.currency is not None or self.scale != 1.0

    @property
    def to_eur_m2(self) -> float:
        """Multiply a figure in this unit by this to get EUR per m²."""
        per = {"ha": 1 / 10_000, "ar": 1 / 100}.get(self.per or "m2", 1.0)
        return per * self.scale


def parse_unit(text: str | None, *, thousands: list[str] | None = None) -> UnitInfo:
    if not text:
        return UnitInfo(None, None, text="")
    folded = fold_words(text)
    per = None
    if _PER_HA.search(folded):
        per = "ha"
    elif _PER_AR.search(folded):
        per = "ar"
    elif _PER_M2.search(folded):
        per = "m2"
    currency = None
    if _OTHER_CURRENCY.search(folded):
        currency = "other"
    elif _EURO.search(folded) or "€" in text:
        currency = "EUR"
    scale = 1.0
    if any(re.search(p, folded) for p in thousands or []):
        scale = 1000.0
    return UnitInfo(per, currency, scale, text)


def merge_units(*units: UnitInfo) -> UnitInfo:
    """The first stated part of each: a column's unit beats the table's."""
    per = next((u.per for u in units if u.per), None)
    currency = next((u.currency for u in units if u.currency), None)
    scale = next((u.scale for u in units if u.scale != 1.0), 1.0)
    text = " | ".join(u.text for u in units if u.text)
    return UnitInfo(per, currency, scale, text)


# --- periods --------------------------------------------------------------------------------------

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}


@dataclass(frozen=True, slots=True)
class Period:
    reference_date: date  # the last day of the period
    label: str  # the words naming it (folded for quarters, halves and months)
    precision: str  # day | month | quarter | half | year


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _ordinal(token: str) -> int | None:
    token = token.lower().rstrip(".")
    if token in _ROMAN:
        return _ROMAN[token]
    return int(token) if token.isdigit() else None


def parse_period(text: str | None, words: MarketPeriods | None = None) -> Period | None:
    """The period a label names, as its last day; None when it names none."""
    if not text:
        return None
    raw = str(text)
    folded = fold_words(raw)
    words = words or MarketPeriods()
    iso = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", raw)
    if iso:
        try:
            return Period(date(*map(int, iso.groups())), iso.group(0), "day")
        except ValueError:
            return None
    dmy = re.search(r"\b(\d{1,2})[./](\d{1,2})[./]((?:19|20)\d{2})\b", raw)
    if dmy:
        day, month, year = map(int, dmy.groups())
        try:
            return Period(date(year, month, day), dmy.group(0), "day")
        except ValueError:
            return None
    quarter_words = "|".join(["quarter", "qtr", *words.quarter])
    half_words = "|".join(["half", "semester", *words.half])
    ordinal = r"(iv|iii|ii|i|[1-4])\.?"
    patterns = (
        (rf"\b{ordinal}\s*(?:{quarter_words})\w*\.?\s*((?:19|20)\d{{2}})", "quarter", 1, 2),
        (r"\bq\s*([1-4])\s*[-/ ]?\s*((?:19|20)\d{2})\b", "quarter", 1, 2),
        (r"\b((?:19|20)\d{2})\s*[-/ ]?\s*q\s*([1-4])\b", "quarter", 2, 1),
        (rf"\b(ii|i|[12])\.?\s*(?:{half_words})\w*\.?\s*((?:19|20)\d{{2}})", "half", 1, 2),
        (r"\bh\s*([12])\s*[-/ ]?\s*((?:19|20)\d{2})\b", "half", 1, 2),
    )
    for pattern, precision, n_group, y_group in patterns:
        m = re.search(pattern, folded)
        if m:
            n, year = _ordinal(m.group(n_group)), int(m.group(y_group))
            if n is None:
                continue
            month = n * 3 if precision == "quarter" else n * 6
            if month > 12:
                continue
            return Period(_month_end(year, month), m.group(0), precision)
    months = [*ENGLISH_MONTHS, *[fold_words(m) for m in words.months]]
    for index, name in enumerate(months):
        variants = {name, name[:3], name + "a"}
        if name.endswith("bar"):
            variants.add(name[:-2] + "ra")  # septembar -> septembra (genitive)
        alternatives = "|".join(sorted(map(re.escape, variants), key=len, reverse=True))
        m = re.search(rf"\b(?:{alternatives})\.?\s*((?:19|20)\d{{2}})\b", folded)
        if m:
            return Period(_month_end(int(m.group(1)), index % 12 + 1), m.group(0), "month")
    ym = re.search(r"\b((?:19|20)\d{2})[-/.](\d{1,2})\b", raw)
    if ym and 1 <= int(ym.group(2)) <= 12:
        return Period(_month_end(int(ym.group(1)), int(ym.group(2))), ym.group(0), "month")
    my = re.search(r"\b(\d{1,2})[/.]((?:19|20)\d{2})\b", raw)
    if my and 1 <= int(my.group(1)) <= 12:
        return Period(_month_end(int(my.group(2)), int(my.group(1))), my.group(0), "month")
    years = [int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", raw)]
    if years:
        return Period(date(max(years), 12, 31), str(max(years)), "year")
    return None


def parse_date(text: str | None) -> date | None:
    """A listing date: ISO, d.m.yyyy, d/m/yyyy (the day must be stated)."""
    period = parse_period(text)
    return period.reference_date if period is not None and period.precision == "day" else None
