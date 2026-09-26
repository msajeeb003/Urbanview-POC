"""Deterministic normalisation of transcribed values; the model never does any of this.

- numbers: one number as printed, in the document's conventions (``0,4``, ``1906.09``,
  ``1.906,09``, ``40 %``); arithmetic in :class:`~decimal.Decimal`, so 0.29 as a ratio is exactly
  29 %;
- units: the field's canonical unit, converting only ratio -> percent and ha <-> m², each
  conversion named in ``normalisation`` next to the stated value;
- dates: ``d.m.yyyy`` -> ISO;
- floors: the local notation (``Po+P+6``) -> below / above-ground counts, with the tokens of the
  municipality profile (``[extraction.floor_notation]``); unknown tokens -> not derivable;
- land use: the document's wording (or the name its legend gives a code) -> a
  :class:`~core.extraction.schema.LandUseClass` through the profile's term table;
- lookup keys for urban parcels and blocks.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from core.extraction.fields import UNIT_SYMBOLS, FieldSpec
from core.extraction.schema import Flag, FloorCount, LandUseClass, StatedUnit
from core.extraction.textmatch import words_fold

# --- numbers -------------------------------------------------------------------------------------

_UNIT_SUFFIX = re.compile(r"(?:%|m²|m2|kvm|ha|m)\.?$", re.IGNORECASE)
_SUFFIX_UNITS: dict[str, StatedUnit] = {
    "%": "percent",
    "m²": "m2",
    "m2": "m2",
    "kvm": "m2",
    "ha": "ha",
    "m": "m",
}
_NUMBER = re.compile(r"[+-]?\d[\d.,']*")


@dataclass(frozen=True, slots=True)
class ParsedNumber:
    value: Decimal
    rules: tuple[str, ...] = ()
    ambiguous: bool = False
    unit_hint: StatedUnit | None = None  # from a printed suffix ("40 %", "3.5m")


def parse_number(printed: str) -> ParsedNumber | None:
    """The single number ``printed`` holds, or None when it is not exactly one number (words,
    two numbers, a formula such as ``h/2``)."""
    text = unicodedata.normalize("NFKC", printed).strip()
    unit_hint: StatedUnit | None = None
    suffix = _UNIT_SUFFIX.search(text)
    if suffix and suffix.start() > 0:
        unit_hint = _SUFFIX_UNITS[suffix.group(0).rstrip(".").casefold()]
        text = text[: suffix.start()]
    text = "".join(text.split())  # a wrapped table cell: "5705.1 7" is 5705.17
    if not _NUMBER.fullmatch(text):
        return None
    sign = ""
    if text[0] in "+-":
        sign, text = ("-" if text[0] == "-" else ""), text[1:]
    text = text.replace("'", "")
    rules: list[str] = []
    ambiguous = False
    dots, commas = text.count("."), text.count(",")
    if dots and commas:
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        group_sep = "." if decimal_sep == "," else ","
        if text.count(decimal_sep) > 1:
            return None
        whole, frac = text.split(decimal_sep)
        whole = whole.replace(group_sep, "")
        rules.append("grouped_number")
        if decimal_sep == ",":
            rules.append("decimal_comma")
    elif dots > 1 or commas > 1:
        whole, frac = text.replace(".", "").replace(",", ""), ""
        rules.append("grouped_number")
    elif dots == 1 or commas == 1:
        whole, frac = re.split(r"[.,]", text)
        if commas:
            rules.append("decimal_comma")
        # "1.500": a decimal (1.5) or a thousands separator (1500)?
        ambiguous = len(frac) == 3 and whole not in ("", "0") and len(whole) <= 3
    else:
        whole, frac = text, ""
    if not whole or not whole.isdigit() or (frac and not frac.isdigit()):
        return None
    try:
        value = Decimal(f"{sign}{whole}.{frac}" if frac else f"{sign}{whole}")
    except InvalidOperation:
        return None
    return ParsedNumber(value=value, rules=tuple(rules), ambiguous=ambiguous, unit_hint=unit_hint)


@dataclass(frozen=True, slots=True)
class NormalisedNumber:
    value: float
    unit: str | None
    rules: tuple[str, ...]
    flags: tuple[Flag, ...]


def _plain(value: Decimal) -> float:
    return float(value)


def normalise_number(
    parsed: ParsedNumber, stated_unit: StatedUnit | None, spec: FieldSpec
) -> NormalisedNumber:
    """``parsed`` in the field's canonical unit. A printed unit sign wins over the model's unit."""
    unit: StatedUnit = parsed.unit_hint or stated_unit or "none"
    value = parsed.value
    rules = list(parsed.rules)
    flags: list[Flag] = [Flag.number_ambiguous] if parsed.ambiguous else []
    canonical = spec.unit
    if spec.percent:
        if unit in ("ratio", "none") and value <= 1:
            value *= 100
            rules.append("ratio_to_percent")
            if unit == "none":
                flags.append(Flag.unit_assumed)
        elif unit == "none":
            flags.append(Flag.unit_assumed)  # a bare 40 in a percentage field
        elif unit == "ratio":
            flags.append(Flag.unit_unexpected)  # a share above 1 cannot be a ratio
        elif unit != "percent":
            flags.append(Flag.unit_unexpected)
            canonical = UNIT_SYMBOLS[unit]
    elif unit in spec.units:
        if unit == "ha" and spec.unit == "m²":
            value *= 10_000
            rules.append("ha_to_m2")
        elif unit == "m2" and spec.unit == "ha":
            value /= 10_000
            rules.append("m2_to_ha")
    elif unit == "none" and spec.default_unit is not None:
        flags.append(Flag.unit_assumed)
    elif not (unit == "none" and spec.unit is None):
        flags.append(Flag.unit_unexpected)
        canonical = UNIT_SYMBOLS[unit]
    return NormalisedNumber(_plain(value), canonical, tuple(rules), tuple(flags))


# --- dates ---------------------------------------------------------------------------------------

_DATE = re.compile(r"(?<!\d)(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{4})(?!\d)")


def parse_date(printed: str) -> date | None:
    """The one ``d.m.yyyy`` date in ``printed`` ("29.11.2012", "29. 11. 2012. godine")."""
    found = _DATE.findall(printed)
    if len(found) != 1:
        return None
    day, month, year = (int(p) for p in found[0])
    try:
        return date(year, month, day)
    except ValueError:
        return None


# --- floors --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FloorTokens:
    """The profile's floor-notation tokens, compared case-insensitively."""

    below_ground: frozenset[str]
    ground: frozenset[str]
    attic: frozenset[str]

    @classmethod
    def of(
        cls, below_ground: Iterable[str], ground: Iterable[str], attic: Iterable[str]
    ) -> FloorTokens:
        return cls(
            frozenset(t.casefold() for t in below_ground),
            frozenset(t.casefold() for t in ground),
            frozenset(t.casefold() for t in attic),
        )


_TOKEN = re.compile(r"(\d*)(\D*)")


def parse_floors(printed: str, tokens: FloorTokens) -> FloorCount | None:
    """Counts from a notation such as ``Po+P+6`` (1 below, 7 above ground), ``S+P+3+M`` or
    ``2Po+P+7``; a bare number is that many floors above ground. None when a token is unknown or
    the order is not below-ground, ground, upper floors, attic."""
    compact = "".join(unicodedata.normalize("NFKC", printed).split())
    if not compact:
        return None
    if compact.isdigit():
        return FloorCount(
            notation=printed.strip(), below_ground=0, above_ground=int(compact), attic=0
        )
    below = above = attic = 0
    stage = 0  # 0 below ground, 1 ground seen, 2 attic seen
    for part in compact.split("+"):
        match = _TOKEN.fullmatch(part)
        if not part or match is None:
            return None
        count_text, code = match.groups()
        count = int(count_text) if count_text else 1
        code = code.casefold()
        if not code:  # upper floors: "6" in Po+P+6
            if stage != 1:
                return None
            above += count
        elif code in tokens.below_ground and stage == 0:
            below += count
        elif code in tokens.ground and stage == 0 and not count_text:
            above += 1
            stage = 1
        elif code in tokens.attic and stage >= 1:
            above += count
            attic += count
            stage = 2
        else:
            return None
    if stage == 0 and below == 0:
        return None
    return FloorCount(notation=printed.strip(), below_ground=below, above_ground=above, attic=attic)


# --- land use ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LandUseRule:
    pattern: re.Pattern[str]
    category: LandUseClass


def compile_land_use_terms(entries: Iterable[tuple[str, str]]) -> tuple[LandUseRule, ...]:
    """``(pattern, class)`` pairs from the profile; patterns match accent-folded lower-case
    text. An unknown class is a configuration error."""
    return tuple(
        LandUseRule(re.compile(pattern), LandUseClass(category)) for pattern, category in entries
    )


def classify_land_use(
    wording: str,
    rules: Sequence[LandUseRule],
    legend: Mapping[str, str] | None = None,
) -> LandUseClass | None:
    """The class of a land-use wording. A legend code (``SS``) is first replaced by the name the
    document's legend gives it; a code without a legend entry has no class."""
    text = words_fold(wording)
    if legend:
        named = legend.get(text.replace(" ", ""))
        if named is not None:
            text = words_fold(named)
    for rule in rules:
        if rule.pattern.search(text):
            return rule.category
    return None


# --- place-specific conventions ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Conventions:
    """What the validator needs to know about a municipality's documents, from its profile."""

    parcel_abbreviation: str
    block_label_words: tuple[str, ...]
    floor_tokens: FloorTokens
    land_use_rules: tuple[LandUseRule, ...]
    document_types: tuple[str, ...]

    @classmethod
    def from_profile(cls, municipality_id: str) -> Conventions:
        from core.municipality import load_extraction_profile, load_profile

        profile = load_profile(municipality_id)
        extraction = load_extraction_profile(municipality_id)
        if extraction is None:
            raise LookupError(f"municipality '{municipality_id}' has no [extraction] profile")
        notation = extraction.floor_notation
        return cls(
            parcel_abbreviation=profile.terminology.urban_parcel.abbreviation,
            block_label_words=tuple(extraction.block_label_words),
            floor_tokens=FloorTokens.of(notation.below_ground, notation.ground, notation.attic),
            land_use_rules=compile_land_use_terms(
                (t.pattern, t.category) for t in extraction.land_use_terms
            ),
            document_types=tuple(profile.terminology.document_types),
        )


# --- codes and keys ------------------------------------------------------------------------------


def match_code(value: str, allowed: Iterable[str]) -> str | None:
    """``value`` as one of the allowed codes (case-insensitive), or None."""
    wanted = value.strip().casefold()
    return next((code for code in allowed if code.casefold() == wanted), None)


def parcel_key(printed: str, abbreviation: str) -> str | None:
    """Lookup key of an urban parcel number: without the abbreviation, lower case, no spaces
    ("UP 12" -> "12", "UP 82 a" -> "82a", "A116/1" -> "a116/1")."""
    text = unicodedata.normalize("NFKC", printed).strip().casefold()
    abbr = abbreviation.casefold()
    if abbr and text.startswith(abbr) and not text[len(abbr) : len(abbr) + 1].isalpha():
        text = text[len(abbr) :].lstrip(" .:-")
    key = "".join(text.split())
    return key or None


def block_key(printed: str, label_words: Sequence[str]) -> str | None:
    """Lookup key of a block label: the label without the profile's label words, lower case, no
    spaces ("Blok A" -> "a", "UKUPNO BLOK A" -> "a", "ukupno A" -> "a")."""
    words = words_fold(printed).split()
    stop = {words_fold(w) for w in label_words}
    while words and words[0] in stop:
        words = words[1:]
    key = "".join(words)
    return key or None
