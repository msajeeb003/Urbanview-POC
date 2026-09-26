"""Scoring extraction results against a corpus gold set (``core.extraction.corpus``).

Every gold cell (parcel x scored field) gets one outcome:

- ``exact``: stated in both, the same printed value (whitespace aside) and canonical value;
- ``tolerance``: the same canonical value printed differently (``1906,09`` / ``1906.09``,
  accents or case of a text) - a unit-normalised match;
- ``wrong``: stated in both, different values;
- ``missing``: the gold states it (or defers it) and the extraction does not (a false blank,
  including values the validator removed as unverified);
- ``false_value``: the extraction states a value where the gold has none, or a value where the
  document defers it - a **guessed** value, the worst outcome because it looks right (must be 0);
- ``correct_blank``: neither states it.

Correct = exact + tolerance + correct_blank; accuracy = correct / cells; stated accuracy =
(exact + tolerance) / the cells the document states or defers (blank fields aside). Parcels the
extraction invents (a number the table does not have) count every stated field as
``false_value``. A correct value whose citation names another page counts in ``wrong_page``
(citations must be right),
one citing another grid cell than the gold's in ``wrong_cell`` (the highlight box).
Confidence calibration: accuracy of the stated values per confidence bucket.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.extraction.corpus import DOCUMENT_FIELDS, Gold, GoldValue
from core.extraction.normalise import Conventions, parcel_key
from core.extraction.schema import ExtractionResult, Leaf, MissingValue, StatedValue, UrbanParcel
from core.extraction.textmatch import words_fold

OUTCOMES = ("exact", "tolerance", "wrong", "missing", "false_value", "correct_blank")
CORRECT = ("exact", "tolerance", "correct_blank")
BUCKETS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01))


def bucket_label(low: float, high: float) -> str:
    return f"{low:.1f}-{min(high, 1.0):.1f}"


def _norm(text: str) -> str:
    return " ".join(str(text).split())


def _same_number(a: Any, b: Any) -> bool:
    try:
        x, y = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(x - y) <= 1e-6 * max(1.0, abs(y))


def leaf_of(parcel: UrbanParcel, key: str) -> Leaf:
    if key == "planned_parcel_area_m2":
        return parcel.planned_parcel_area_m2
    if key == "block_ref":
        return parcel.block_ref
    return getattr(parcel.rules, key)


def compare(gold: GoldValue | None, leaf: Leaf | None) -> tuple[str, bool | None]:
    """(outcome, citation on the gold page or None when not applicable)."""
    stated = isinstance(leaf, StatedValue)
    deferred = isinstance(leaf, MissingValue) and leaf.reason == "deferred"
    if gold is None:
        if stated:
            return "false_value", None
        return ("wrong" if deferred else "correct_blank"), None
    if gold.status == "deferred":
        if deferred:
            page = leaf.source.page if leaf.source else None  # type: ignore[union-attr]
            return "exact", page == gold.page if page is not None else None
        return ("false_value" if stated else "missing"), None
    if not stated:
        return "missing", None
    assert isinstance(leaf, StatedValue)
    page_ok = leaf.source.page == gold.page
    printed = leaf.stated.value
    if isinstance(gold.value, float | int) and not isinstance(gold.value, bool):
        same_value = _same_number(leaf.value, gold.value)
    else:  # texts: a line break inside a wrapped cell ("P k" / "Pk") is layout, not content
        got, want = words_fold(str(leaf.value)), words_fold(str(gold.value))
        same_value = got == want or got.replace(" ", "") == want.replace(" ", "")
    if not same_value:
        return "wrong", page_ok
    if _norm(printed) == _norm(gold.printed):
        return "exact", page_ok
    return "tolerance", page_ok


@dataclass(slots=True)
class FieldStats:
    outcomes: Counter = field(default_factory=Counter)
    wrong_page: int = 0
    wrong_cell: int = 0  # a correct value whose cited grid cell is not the gold cell
    stated: int = 0  # cells the document states (or defers)
    stated_correct: int = 0

    @property
    def cells(self) -> int:
        return sum(self.outcomes[o] for o in OUTCOMES)

    @property
    def correct(self) -> int:
        return sum(self.outcomes[o] for o in CORRECT)

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.cells if self.cells else None

    @property
    def stated_accuracy(self) -> float | None:
        return self.stated_correct / self.stated if self.stated else None


@dataclass(slots=True)
class DocumentScore:
    document: str
    fields: dict[str, FieldStats]
    parcels_gold: int = 0
    parcels_found: int = 0
    parcels_missed: list[str] = field(default_factory=list)
    parcels_extra: list[str] = field(default_factory=list)
    duplicates: int = 0
    unverified: int = 0
    calibration: dict[str, list[int]] = field(default_factory=dict)  # bucket -> [correct, total]
    document_fields: dict[str, str] = field(default_factory=dict)
    blocks_gold: int = 0
    blocks_found: int = 0
    examples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def total(self) -> FieldStats:
        out = FieldStats()
        for stats in self.fields.values():
            out.outcomes.update(stats.outcomes)
            out.wrong_page += stats.wrong_page
            out.wrong_cell += stats.wrong_cell
            out.stated += stats.stated
            out.stated_correct += stats.stated_correct
        return out

    def to_json(self) -> dict[str, Any]:
        total = self.total()
        return {
            "document": self.document,
            "cells": total.cells,
            "accuracy": round(total.accuracy or 0.0, 4),
            "stated_cells": total.stated,
            "stated_accuracy": round(total.stated_accuracy or 0.0, 4),
            "outcomes": {o: total.outcomes[o] for o in OUTCOMES},
            "hallucinated": total.outcomes["false_value"],
            "wrong_page": total.wrong_page,
            "wrong_cell": total.wrong_cell,
            "unverified": self.unverified,
            "parcels": {
                "gold": self.parcels_gold,
                "found": self.parcels_found,
                "missed": self.parcels_missed,
                "extra": self.parcels_extra,
                "duplicates": self.duplicates,
            },
            "blocks": {"gold": self.blocks_gold, "found": self.blocks_found},
            "fields": {
                key: {
                    "accuracy": round(stats.accuracy or 0.0, 4),
                    "stated_accuracy": (
                        round(stats.stated_accuracy, 4)
                        if stats.stated_accuracy is not None
                        else None
                    ),
                    "outcomes": {o: stats.outcomes[o] for o in OUTCOMES},
                    "wrong_page": stats.wrong_page,
                    "wrong_cell": stats.wrong_cell,
                }
                for key, stats in self.fields.items()
            },
            "calibration": {
                bucket: {
                    "correct": counts[0],
                    "total": counts[1],
                    "accuracy": round(counts[0] / counts[1], 4) if counts[1] else None,
                }
                for bucket, counts in self.calibration.items()
            },
            "document_fields": self.document_fields,
            "examples": self.examples,
        }


def _example(bucket: dict[str, list], outcome: str, item: dict[str, Any], limit: int = 12) -> None:
    items = bucket.setdefault(outcome, [])
    if len(items) < limit:
        items.append(item)


def _leaf_summary(leaf: Leaf | None) -> Any:
    if isinstance(leaf, StatedValue):
        return {"value": leaf.value, "printed": leaf.stated.value, "page": leaf.source.page}
    if isinstance(leaf, MissingValue):
        return {"missing": leaf.reason, "note": leaf.note}
    return None


def score_document(
    gold: Gold,
    results: Iterable[ExtractionResult],
    *,
    municipality: str,
    pages: set[int] | None = None,
) -> DocumentScore:
    """Score the results of one document against its gold set. ``pages`` restricts the score
    to gold parcels (and extracted ones) on those pages (a sampled run)."""
    abbreviation = Conventions.from_profile(municipality).parcel_abbreviation
    results = list(results)
    gold_parcels = [p for p in gold.parcels if pages is None or p.page in pages]
    by_key = {p.key: p for p in gold_parcels}
    all_keys = {p.key for p in gold.parcels}
    extracted: dict[str, list[UrbanParcel]] = defaultdict(list)
    for result in results:
        for parcel in result.urban_parcels:
            number = parcel.urban_parcel_number
            if not isinstance(number, StatedValue):
                continue
            key = parcel.parcel_key or parcel_key(str(number.value), abbreviation)
            if key is None:
                continue
            if pages is not None and number.source.page not in pages:
                continue
            extracted[key].append(parcel)
    score = DocumentScore(document=gold.document, fields={f: FieldStats() for f in gold.fields})
    score.parcels_gold = len(gold_parcels)
    for gp in gold_parcels:
        candidates = extracted.get(gp.key, [])
        chosen = next(
            (
                c
                for c in candidates
                if isinstance(c.urban_parcel_number, StatedValue)
                and c.urban_parcel_number.source.page == gp.page
            ),
            candidates[0] if candidates else None,
        )
        if chosen is None:
            score.parcels_missed.append(gp.number)
        else:
            score.parcels_found += 1
            score.duplicates += max(0, len(candidates) - 1)
        for key in gold.fields:
            leaf = leaf_of(chosen, key) if chosen is not None else None
            outcome, page_ok = compare(gp.values.get(key), leaf)
            stats = score.fields[key]
            stats.outcomes[outcome] += 1
            if gp.values.get(key) is not None:
                stats.stated += 1
                stats.stated_correct += outcome in ("exact", "tolerance")
            if outcome in ("exact", "tolerance") and page_ok is False:
                stats.wrong_page += 1
                _example(score.examples, "wrong_page", {"parcel": gp.number, "field": key})
            gold_cell = gp.values[key].cell if gp.values.get(key) is not None else None
            if outcome in ("exact", "tolerance") and page_ok and gold_cell:
                ref = leaf.source.table_ref if isinstance(leaf, StatedValue) else None
                if ref is None or ref.cell != gold_cell:
                    stats.wrong_cell += 1
                    _example(
                        score.examples,
                        "wrong_cell",
                        {
                            "parcel": gp.number,
                            "field": key,
                            "gold_cell": gold_cell,
                            "cited": ref.cell if ref else None,
                        },
                    )
            if isinstance(leaf, MissingValue) and leaf.reason == "unverified":
                score.unverified += 1
            if outcome not in CORRECT:
                gold_value = gp.values.get(key)
                _example(
                    score.examples,
                    outcome,
                    {
                        "parcel": gp.number,
                        "page": gp.page,
                        "field": key,
                        "gold": gold_value.model_dump(exclude={"cell"}) if gold_value else None,
                        "extracted": _leaf_summary(leaf),
                    },
                )
            if isinstance(leaf, StatedValue):
                for low, high in BUCKETS:
                    if low <= leaf.confidence < high:
                        counts = score.calibration.setdefault(bucket_label(low, high), [0, 0])
                        counts[1] += 1
                        counts[0] += outcome in ("exact", "tolerance")
    for key, parcels in extracted.items():
        if key in by_key or key in all_keys:
            continue
        number = str(parcels[0].urban_parcel_number.value)  # type: ignore[union-attr]
        score.parcels_extra.append(number)
        for key_field in gold.fields:
            leaf = leaf_of(parcels[0], key_field)
            if isinstance(leaf, StatedValue):
                score.fields[key_field].outcomes["false_value"] += 1
                _example(
                    score.examples,
                    "false_value",
                    {
                        "parcel": number,
                        "field": key_field,
                        "extra_parcel": True,
                        "extracted": _leaf_summary(leaf),
                    },
                )
    score.document_fields = _score_document_fields(gold, results)
    gold_blocks = [b for b in gold.blocks if pages is None or b.page in pages]
    score.blocks_gold = len(gold_blocks)
    labels = {
        words_fold(str(b.block_ref.value))
        for r in results
        for b in r.blocks
        if isinstance(b.block_ref, StatedValue)
    }
    score.blocks_found = sum(1 for b in gold_blocks if words_fold(b.label) in labels)
    return score


def _score_document_fields(gold: Gold, results: Sequence[ExtractionResult]) -> dict[str, str]:
    documents = [r.document for r in results if r.document is not None]
    out: dict[str, str] = {}
    for key in DOCUMENT_FIELDS:
        expected = gold.document_fields.get(key)
        leaves = [getattr(d, key) for d in documents]
        stated = [leaf for leaf in leaves if isinstance(leaf, StatedValue)]
        if expected is None:
            out[key] = "false_value" if stated else "correct_blank"
            continue
        if not stated:
            out[key] = "missing"
            continue
        want = words_fold(str(expected.value))
        got = words_fold(str(stated[0].value))
        if got == want:
            out[key] = "exact"
        elif want in got or got in want:
            out[key] = "tolerance"
        else:
            out[key] = "wrong"
    return out
