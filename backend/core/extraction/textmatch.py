"""Finding transcribed text on a page: the check behind "every value carries a page that actually
contains its raw text".

Matching is done on folded text: case, diacritics (``š`` = ``s``, ``đ`` = ``dj``), typographic
quotes and dashes, and all whitespace are ignored, so a table cell wrapped over two lines
(``5705.1`` / ``7``) matches ``5705.17`` and a transcription that dropped a diacritic still finds
its text. What is stored is never the model's copy: once found, ``raw_text`` and the value are
taken from the page itself (whitespace runs collapsed), so the stored text is on the page by
construction.

The bbox comes from the page's words (PDF points, origin bottom-left). When the text occurs more
than once on the page, a table row label picks the occurrence on that row; otherwise the bbox is
left out (``bbox_ambiguous``) rather than highlighting the wrong place.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.extraction.pages import PageInput

BBox = tuple[float, float, float, float]

_CHAR_MAP = {
    "„": '"',
    "“": '"',
    "”": '"',
    "«": '"',
    "»": '"',
    "‘": "'",
    "’": "'",
    "‚": "'",
    "–": "-",
    "—": "-",
    "−": "-",
    "‐": "-",
    "‑": "-",
    "đ": "dj",
    "Đ": "dj",
}
# Montenegrin / Serbian Cyrillic folds to the same letters as its Latin spelling (ђ = đ = dj,
# ч = č = c, љ = lj ...), so keywords and citations match in either script.
_CYRILLIC = dict(
    zip(
        "абвгдђежзијклљмнњопрстћуфхцчџш",
        "a b v g d dj e z z i j k l lj m n nj o p r s t c u f h c c dz s".split(),
        strict=True,
    )
)
_CHAR_MAP.update(_CYRILLIC)
_CHAR_MAP.update({cyr.upper(): lat for cyr, lat in _CYRILLIC.items()})
_DROP = {"­", "​", "‌", "‍", "﻿"}


def fold_char(ch: str) -> str:
    if ch.isspace() or ch in _DROP:
        return ""
    mapped = _CHAR_MAP.get(ch)
    if mapped is not None:
        return mapped
    decomposed = unicodedata.normalize("NFKD", ch)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def fold(text: str) -> str:
    """Case-, accent- and whitespace-insensitive form used for every containment check."""
    return "".join(fold_char(c) for c in text)


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """Folded text plus, for every folded character, the index of its source character."""
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        for f in fold_char(ch):
            chars.append(f)
            index.append(i)
    return "".join(chars), index


def words_fold(text: str) -> str:
    """Case- and accent-folded text with whitespace collapsed to single spaces (term matching)."""
    return " ".join(fold(word) for word in text.split())


def collapse_ws(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True, slots=True)
class Occurrence:
    start: int  # indices into the page text
    end: int
    text: str  # the page text of the occurrence, whitespace collapsed


def _alnum(text: str, i: int) -> bool:
    return 0 <= i < len(text) and text[i].isalnum()


def find_in(text: str, needle: str) -> list[Occurrence]:
    """Occurrences of ``needle`` in ``text`` on whole-word boundaries (folded match): "1" is not
    found inside "1906.09", "UP 2" not inside "UP 21"."""
    folded, index = fold_with_map(text)
    target = fold(needle)
    out: list[Occurrence] = []
    if not target:
        return out
    pos = folded.find(target)
    while pos != -1:
        start = index[pos]
        end = index[pos + len(target) - 1] + 1
        if not _alnum(text, start - 1) and not _alnum(text, end):
            out.append(Occurrence(start, end, collapse_ws(text[start:end])))
        pos = folded.find(target, pos + 1)
    return out


@dataclass(slots=True)
class PageIndex:
    """Folded forms of one page's words, built once per page, for boxes."""

    page: PageInput
    _word_stream: str = ""
    _word_owner: list[int] = field(default_factory=list)
    _starts: set[int] = field(default_factory=set)
    _ends: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        chars: list[str] = []
        owner: list[int] = []
        for i, word in enumerate(self.page.words):
            f = fold(word.text)
            if not f:
                continue
            self._starts.add(len(owner))
            chars.append(f)
            owner.extend([i] * len(f))
            self._ends.add(len(owner) - 1)
        self._word_stream = "".join(chars)
        self._word_owner = owner

    def find(self, needle: str) -> list[Occurrence]:
        return find_in(self.page.text, needle)

    def word_boxes(self, needle: str) -> list[BBox]:
        """One bbox per occurrence of ``needle`` made of whole words (union of their boxes)."""
        target = fold(needle)
        if not target or not self._word_stream:
            return []
        boxes: list[BBox] = []
        pos = self._word_stream.find(target)
        while pos != -1:
            last_char = pos + len(target) - 1
            if pos in self._starts and last_char in self._ends:
                first, last = self._word_owner[pos], self._word_owner[last_char]
                boxes.append(union(w.bbox for w in self.page.words[first : last + 1]))
            pos = self._word_stream.find(target, pos + 1)
        return boxes


def union(boxes: Iterable[BBox]) -> BBox:
    xs0, ys0, xs1, ys1 = zip(*boxes, strict=True)
    return (min(xs0), min(ys0), max(xs1), max(ys1))


COLUMN_MARGIN_PT = 10.0


def pick_box(candidates: list[BBox], rows: list[BBox], columns: list[BBox]) -> BBox | None:
    """The one candidate in the cell a row label and a column header point at: on the row
    label's line (vertical centres within about a line) and, among those, clearly the closest to
    the column header horizontally. None when that is not unambiguous."""
    if len(candidates) == 1:
        return candidates[0]
    if not candidates or not (rows or columns):
        return None

    def cy(b: BBox) -> float:
        return (b[1] + b[3]) / 2

    def cx(b: BBox) -> float:
        return (b[0] + b[2]) / 2

    pool = candidates
    if rows:
        line = max(rows[0][3] - rows[0][1], 1.0)
        distance = [min(abs(cy(c) - cy(a)) for a in rows) for c in pool]
        best = min(distance)
        if best > 1.5 * line:
            return None
        pool = [c for c, d in zip(pool, distance, strict=True) if d - best < 0.5 * line]
    if len(pool) > 1 and columns:
        distance = [min(abs(cx(c) - cx(a)) for a in columns) for c in pool]
        order = sorted(range(len(pool)), key=distance.__getitem__)
        if distance[order[1]] - distance[order[0]] < COLUMN_MARGIN_PT:
            return None
        pool = [pool[order[0]]]
    return pool[0] if len(pool) == 1 else None


def round_box(box: BBox) -> BBox:
    return (round(box[0], 1), round(box[1], 1), round(box[2], 1), round(box[3], 1))
