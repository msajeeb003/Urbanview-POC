"""A reviewer's correction of an extracted value, checked with the extraction contract's own rules
(the A2 check of 2026-09-29: "a non-numeric FAR or an unknown land use is rejected"). A correction
is typed and normalised exactly the way the validator treats a value the model read
(``core.extraction.normalise``), so an amended value is as clean as an extracted one:

- **numbers**: one number in the document's conventions ("2,5", "1.906,09", "40 %"), in the
  field's canonical unit or one the contract converts (ha for an area: the only arithmetic, the
  contract's own unit rule). Impossible values are refused (below the field's minimum, at an
  exclusive minimum, a percentage above 100); a value above the field's plausible maximum
  (``fields.FIELD_SPECS``: FAR 20, height 300 m ...) needs ``confirm_out_of_range``;
- **floors**: the plan's notation with the profile's tokens ("Po+P+6", "S+P+4+Pk"), counted;
- **land use**: a wording the document already uses for the field (its items and published
  values), or one the profile's land-use terms classify into a product class; anything else is an
  unknown land use;
- **other texts**: trimmed, 1 to ``MAX_TEXT`` characters;
- **market rates** (a ``parameter_key`` outside the field dictionary): positive numbers.

Pure: numbers and words in, a ``Correction`` or a ``CorrectionRefused`` (code, sentence, context)
out; the review service turns a refusal into a 422.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from core.extraction.fields import FIELD_SPECS, FieldSpec
from core.extraction.normalise import (
    Conventions,
    ParsedNumber,
    classify_land_use,
    normalise_number,
    parse_floors,
    parse_number,
)
from core.extraction.schema import FloorCount, StatedUnit
from core.extraction.textmatch import words_fold

MAX_TEXT = 500
# the unit a reviewer may send -> how the contract names it
UNIT_INPUTS: dict[str, StatedUnit] = {"%": "percent", "m²": "m2", "m2": "m2", "ha": "ha", "m": "m"}


@dataclass(frozen=True, slots=True)
class Correction:
    text: str | None
    number: float | None
    unit: str | None
    rules: tuple[str, ...] = ()
    out_of_range: bool = False
    land_use_class: str | None = None
    floors: FloorCount | None = None

    def details(self) -> dict[str, Any]:
        """What the audit row records about the check."""
        out: dict[str, Any] = {}
        if self.rules:
            out["normalisation"] = list(self.rules)
        if self.out_of_range:
            out["out_of_range_confirmed"] = True
        if self.land_use_class:
            out["land_use_class"] = self.land_use_class
        if self.floors is not None:
            out["floors"] = self.floors.model_dump(exclude={"rule"})
        return out


class CorrectionRefused(ValueError):
    """``code``: not_a_number | not_a_text | unit_not_accepted | below_minimum | above_maximum |
    out_of_range | unknown_floor_notation | unknown_land_use | too_long | empty."""

    def __init__(self, code: str, message: str, **context: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = context

    def problem(self) -> dict[str, Any]:
        """One entry of a 422's ``details``."""
        return {
            "loc": ["body", "value"],
            "msg": self.message,
            "type": self.code,
            "ctx": self.context,
        }


def _accepted_units(spec: FieldSpec) -> dict[str | None, StatedUnit]:
    if spec.percent:
        return {"%": "percent", None: "percent"}
    if spec.unit == "m²":
        return {"m²": "m2", "m2": "m2", "ha": "ha", None: "m2"}
    if spec.unit == "m":
        return {"m": "m", None: "m"}
    return {None: "none"}


def _parse(value: float | int | str) -> ParsedNumber:
    if isinstance(value, bool):
        raise CorrectionRefused("not_a_number", "Enter a number, e.g. 2.5 (a comma works too).")
    if isinstance(value, int | float):
        if not math.isfinite(value):
            raise CorrectionRefused("not_a_number", "Enter a finite number.")
        try:
            return ParsedNumber(value=Decimal(str(value)))
        except InvalidOperation:
            raise CorrectionRefused("not_a_number", "Enter a number.") from None
    parsed = parse_number(value)
    if parsed is None:
        raise CorrectionRefused(
            "not_a_number",
            f"“{value}” is not a number: enter one number, e.g. 2.5 (a comma works too).",
        )
    return parsed


def _bounds_text(spec: FieldSpec) -> str:
    low = f"{spec.minimum:g}" if spec.minimum is not None else "…"
    high = f"{spec.maximum:g}" if spec.maximum is not None else "…"
    return f"{low}–{high}"


def _number(
    spec: FieldSpec, value: float | int | str, unit: str | None, confirm_out_of_range: bool
) -> Correction:
    parsed = _parse(value)
    accepted = _accepted_units(spec)
    key = unit.strip() if isinstance(unit, str) and unit.strip() else None
    if key not in accepted:
        allowed = [u for u in accepted if u is not None] or ["no unit"]
        raise CorrectionRefused(
            "unit_not_accepted",
            f"This field takes {' or '.join(allowed)}, not “{unit}”.",
            accepted=allowed,
        )
    stated: StatedUnit = accepted[key]
    if parsed.unit_hint is not None:  # a unit printed with the number wins (the contract's rule)
        if parsed.unit_hint not in accepted.values():
            raise CorrectionRefused(
                "unit_not_accepted", f"This field does not take the unit printed in “{value}”."
            )
        stated = parsed.unit_hint
    number = normalise_number(ParsedNumber(parsed.value, parsed.rules), stated, spec)
    amount = number.value
    if spec.minimum is not None and (
        amount < spec.minimum or (spec.exclusive_minimum and amount == spec.minimum)
    ):
        word = "above" if spec.exclusive_minimum else "at least"
        raise CorrectionRefused(
            "below_minimum",
            f"{amount:g} is impossible here: the value must be {word} {spec.minimum:g}.",
            minimum=spec.minimum,
            exclusive=spec.exclusive_minimum,
        )
    out_of_range = False
    if spec.maximum is not None and amount > spec.maximum:
        if spec.percent:
            raise CorrectionRefused(
                "above_maximum", "A percentage is at most 100.", maximum=spec.maximum
            )
        if not confirm_out_of_range:
            raise CorrectionRefused(
                "out_of_range",
                f"{amount:g} is outside the usual range for this field ({_bounds_text(spec)}); "
                "confirm it if the plan really says so.",
                minimum=spec.minimum,
                maximum=spec.maximum,
            )
        out_of_range = True
    return Correction(None, amount, spec.unit, rules=number.rules, out_of_range=out_of_range)


def _text(value: float | int | str) -> str:
    if not isinstance(value, str):
        raise CorrectionRefused("not_a_text", "This field takes a text.")
    text = " ".join(value.split())
    if not text:
        raise CorrectionRefused("empty", "Enter the corrected value.")
    if len(text) > MAX_TEXT:
        raise CorrectionRefused(
            "too_long", f"Keep the value under {MAX_TEXT} characters.", maximum=MAX_TEXT
        )
    return text


def _floors(value: float | int | str, conventions: Conventions | None) -> Correction:
    text = "+".join(part.strip() for part in _text(value).split("+"))
    if conventions is None:
        return Correction(text, None, None)
    counted = parse_floors(text, conventions.floor_tokens)
    if counted is None:
        tokens = conventions.floor_tokens
        known = ", ".join(
            sorted({*tokens.below_ground, *tokens.ground, *tokens.attic}, key=str.casefold)
        )
        raise CorrectionRefused(
            "unknown_floor_notation",
            f"“{text}” is not the plan's floor notation: use its tokens, e.g. P+4+Pk "
            f"(known: {known}; below ground, ground, then upper floors, attic last).",
            tokens=sorted({*tokens.below_ground, *tokens.ground, *tokens.attic}),
        )
    return Correction(text, None, None, floors=counted)


def _land_use(
    value: float | int | str, conventions: Conventions | None, known_wordings: Iterable[str]
) -> Correction:
    text = _text(value)
    folded = words_fold(text)
    for wording in known_wordings:
        if wording and words_fold(wording) == folded:
            rules = conventions.land_use_rules if conventions else ()
            category = classify_land_use(wording, rules)
            return Correction(
                " ".join(wording.split()), None, None, land_use_class=category and category.value
            )
    if conventions is None:
        return Correction(text, None, None)
    category = classify_land_use(text, conventions.land_use_rules)
    if category is None:
        raise CorrectionRefused(
            "unknown_land_use",
            f"“{text}” is not a land use this document uses or the profile's land-use terms "
            "know: pick one of the document's designations, or type the plan's wording.",
        )
    return Correction(text, None, None, land_use_class=category.value)


def check_correction(
    field_key: str | None,
    value: float | int | str,
    unit: str | None = None,
    *,
    conventions: Conventions | None,
    known_wordings: Iterable[str] = (),
    confirm_out_of_range: bool = False,
) -> Correction:
    """The correction as it would be stored (canonical value and unit), or ``CorrectionRefused``.
    ``field_key`` None = a market rate (a positive number, its unit kept)."""
    if field_key is None:
        parsed = _parse(value)
        amount = float(parsed.value)
        if amount <= 0:
            raise CorrectionRefused(
                "below_minimum", "A market rate is a positive number.", minimum=0, exclusive=True
            )
        return Correction(None, amount, unit.strip() if unit and unit.strip() else None)
    spec = FIELD_SPECS.get(field_key)
    if spec is None or spec.kind == "text" or spec.kind in ("code", "date"):
        return Correction(_text(value), None, None)
    if spec.kind == "number":
        return _number(spec, value, unit, confirm_out_of_range)
    if spec.kind == "floors":
        return _floors(value, conventions)
    return _land_use(value, conventions, known_wordings)


@lru_cache(maxsize=16)
def conventions_for(municipality_id: str) -> Conventions | None:
    """The municipality's floor tokens and land-use terms (``[extraction]`` of the profile), or
    None when the profile has none (numbers are still checked)."""
    try:
        return Conventions.from_profile(municipality_id)
    except LookupError:
        return None
