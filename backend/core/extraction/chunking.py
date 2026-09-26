"""Chunks: what one extraction request reads, planned from the pre-processed pages.

- **Elements** in reading order: text blocks outside tables, and whole tables (a table is never
  split across chunks).
- **One chunk per page** by default, within ``chunk_token_budget`` (estimated as characters /
  ``chars_per_token``). A page over the budget is split between elements; an element over the
  budget alone is a chunk of its own, marked ``over_budget``. Unread pages (scanned without OCR)
  have no chunk: they are listed in the manifest, never filled in.
- **Stitched tables**: a fragment that continues a table (``header_from``) is shown with that
  table's column names, so every chunk carries its columns.
- **Sections** (methodology step 2a–d: urban parcels, regulation, land use, infrastructure):
  numbered, larger, bold or upper-case short blocks are headings; a heading matching the
  profile's ``[extraction.sections]`` patterns sets the section until a numbered heading of the
  same or a higher level that matches nothing. Running page headers / footers are not headings.
  A table's column names add the sections they match. Chunks with a planning section come first
  (``priority`` 0) and suggest the extraction tasks that fit them.
- :func:`chunk_pages` turns a chunk into the contract's :class:`~core.extraction.pages.PageInput`
  (the text and words the model's citations are checked against, the table grids, and the view the
  model reads), carrying every page number and box through.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from core.extraction.pages import PageInput, TableCell, TableGrid, Word, merged_origin
from core.extraction.preprocess import (
    DocumentPages,
    Heading,
    PageBlock,
    PageData,
    PageTable,
    PreprocessOptions,
)
from core.extraction.textmatch import words_fold

PLANNING_SECTIONS: tuple[str, ...] = ("urban_parcels", "regulation", "land_use", "infrastructure")


class ChunkPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str  # "c001", document order
    pages: list[int]
    blocks: list[str] = Field(default_factory=list, description="Text blocks, reading order")
    tables: list[str] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list, description="Extraction tasks that fit it")
    tokens: int = Field(description="Estimated tokens of the view")
    over_budget: bool = False
    priority: int = Field(description="0 = planning sections or tables, read first; 1 = other")


# --- sections -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SectionRules:
    """Section kind -> patterns on accent-folded lower-case text (Cyrillic transliterated)."""

    patterns: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = ()

    @classmethod
    def of(cls, sections: dict[str, list[str]]) -> SectionRules:
        return cls(tuple((k, tuple(re.compile(p) for p in v)) for k, v in sections.items()))

    @classmethod
    def from_profile(cls, municipality_id: str) -> SectionRules:
        from core.municipality import load_extraction_profile

        profile = load_extraction_profile(municipality_id)
        return cls.of(profile.sections if profile else {})

    def match(self, text: str) -> list[str]:
        folded = words_fold(text)
        return [kind for kind, patterns in self.patterns if any(p.search(folded) for p in patterns)]


_DECIMAL = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,4})\.?\s+\S")
_ROMAN = re.compile(r"^\s*[IVX]{1,5}\.\s+\S")
_LETTER = re.compile(r"^\s*[a-z]\)\s+\S")
_DIGITS = re.compile(r"\d+")


def heading_level(text: str) -> int | None:
    """1 for "4." / "IV.", 2 for "4.2", 3 for "4.2.1" or "a)"; None when not numbered."""
    decimal = _DECIMAL.match(text)
    if decimal:
        return decimal.group(1).count(".") + 1
    if _ROMAN.match(text):
        return 1
    if _LETTER.match(text):
        return 3
    return None


def _running(doc: DocumentPages) -> set[str]:
    """Texts repeated on many pages (running headers, footers, page numbers)."""
    if doc.page_count < 3:
        return set()
    seen: Counter[str] = Counter()
    for page in doc.pages:
        seen.update({_DIGITS.sub("#", words_fold(b.text)) for b in page.blocks if b.text.strip()})
    floor = max(3, math.ceil(0.4 * doc.page_count))
    return {text for text, count in seen.items() if count >= floor}


def _body_size(doc: DocumentPages) -> float | None:
    sizes = [
        b.font_size
        for p in doc.pages
        for b in p.blocks
        if b.font_size and b.table is None
        for _ in range(min(len(b.text), 400))
    ]
    return statistics.median(sizes) if sizes else None


def _is_heading(block: PageBlock, body: float | None, running: set[str]) -> bool:
    text = block.text.strip()
    if not text or block.table is not None or len(text) > 120:
        return False
    if text.count("\n") > 1 or len(text.split()) > 14:
        return False
    if _DIGITS.sub("#", words_fold(text)) in running:
        return False
    _, colon, rest = text.partition(":")
    if colon and rest.strip():
        return False  # "Namjena: stanovanje" is a condition, not a heading
    letters = [c for c in text if c.isalpha()]
    upper = len(letters) >= 4 and all(c.isupper() for c in letters)
    larger = bool(body and block.font_size and block.font_size >= body * 1.15)
    return heading_level(text) is not None or larger or block.bold or upper


def detect_sections(doc: DocumentPages, rules: SectionRules) -> None:
    """Fill ``headings`` and ``sections`` of the pages, ``section`` of the blocks and
    ``sections`` of the tables."""
    body, running = _body_size(doc), _running(doc)
    current: str | None = None
    current_level = 99
    for page in doc.pages:
        found: set[str] = {current} if current else set()
        page.headings = []
        for block in page.blocks:
            if _is_heading(block, body, running):
                level = heading_level(block.text)
                kinds = rules.match(block.text)
                kind = kinds[0] if kinds else None
                if kind is not None:
                    current, current_level = kind, level or 1
                elif level is not None and level <= current_level:
                    current, current_level = None, 99
                page.headings.append(
                    Heading(block=block.id, text=block.text, level=level, section=kind)
                )
            block.section = current
            if current:
                found.add(current)
        for table in page.tables:
            table.sections = rules.match(" ".join(table.columns))
            found.update(table.sections)
        if not page.blocks:
            found = set()  # a scan or a blank page: the section runs on past it
        page.sections = [s for s in PLANNING_SECTIONS if s in found] + sorted(
            found - set(PLANNING_SECTIONS)
        )


# --- chunks -------------------------------------------------------------------------------------

Element = tuple[str, PageBlock | PageTable]  # ("block", block) | ("table", table)


def elements(page: PageData) -> list[Element]:
    """Text blocks outside tables and whole tables, in reading order (a table where its first
    block reads)."""
    out: list[Element] = []
    placed: set[str] = set()
    tables = {t.id: t for t in page.tables}
    for block in page.blocks:
        if block.table is None:
            out.append(("block", block))
        elif block.table not in placed:
            placed.add(block.table)
            out.append(("table", tables[block.table]))
    out.extend(("table", t) for t in page.tables if t.id not in placed)
    return out


def merged_from(table: PageTable, index: int, col: int) -> int | None:
    """The earlier row whose cell in ``col`` is merged over row ``index`` (a value printed once
    for several rows), or None: :func:`core.extraction.pages.merged_origin`."""
    return merged_origin(table.rows, index, col, table.header_rows)


def _cells(table: PageTable, index: int) -> str:
    parts = []
    for j, c in enumerate(table.rows[index]):
        name = c.id[c.id.index("c") :]
        if c.text:
            parts.append(f"{name}: {c.text}")
        elif (k := merged_from(table, index, j)) is not None:
            parts.append(f"{name}: ^r{k}")
    return " | ".join(parts)


def render_table(table: PageTable, doc: DocumentPages) -> str:
    """A table as the model reads it: its column names (its own header, or the header of the
    table it continues), then one line per row with cell ids (rNcM) for citations; a merged cell
    shows as ``cM: ^rK`` in the rows it spans below rK (where its text is)."""
    columns = " | ".join(f"c{j} {name}" for j, name in enumerate(table.columns) if name)
    if table.header_from:
        origin = doc.table(table.header_from)
        where = f"page {origin.id[1:].split('t')[0]}" if origin else "an earlier page"
        head = (
            f"[Table {table.id}: continues {table.continues}; columns from {table.header_from} "
            f"on {where}: {columns}]"
        )
    else:
        head = f"[Table {table.id}: columns: {columns}]" if columns else f"[Table {table.id}]"
    lines = [head]
    for index in range(table.header_rows, len(table.rows)):
        cells = _cells(table, index)
        if cells:
            lines.append(f"r{index} | {cells}")
    lines.append(f"[End of table {table.id}]")
    return "\n".join(lines)


def _element_view(element: Element, doc: DocumentPages) -> str:
    kind, item = element
    return item.text if kind == "block" else render_table(item, doc)  # type: ignore[union-attr, arg-type]


def estimate_tokens(text: str, chars_per_token: float) -> int:
    return math.ceil(len(text) / chars_per_token)


def _tasks(page: PageData, parts: Sequence[Element]) -> list[str]:
    text_sections = {
        b.section
        for kind, b in parts
        if kind == "block" and b.section  # type: ignore[union-attr]
    }
    tables = [t for kind, t in parts if kind == "table"]
    table_sections = {s for t in tables for s in t.sections}  # type: ignore[union-attr]
    tasks: list[str] = []
    if page.number <= 2 and not tables:
        tasks.append("document")
    parcel_tables = table_sections & {"urban_parcels", "regulation"}
    if parcel_tables:
        tasks.append("parameter_table")  # its land-use column is read with the parameters
    if text_sections & {"urban_parcels", "regulation"}:
        tasks += ["urban_parcel", "block"]
    if "land_use" in text_sections:
        tasks += ["block", "land_use_legend"]
    if "land_use" in table_sections and not parcel_tables:
        tasks.append("land_use_legend")
    if "infrastructure" in text_sections | table_sections:
        tasks.append("infrastructure")
    return list(dict.fromkeys(tasks))


def plan_chunks(doc: DocumentPages, options: PreprocessOptions) -> list[ChunkPlan]:
    """Chunks in reading priority: planning sections and tables first, then the rest."""
    budget, per_token = options.chunk_token_budget, options.chars_per_token
    plans: list[ChunkPlan] = []
    for page in doc.pages:
        if page.method == "none":
            continue  # nothing was read: listed in the manifest, never filled in
        groups: list[list[Element]] = []
        current: list[Element] = []
        used = 0
        for element in elements(page):
            cost = estimate_tokens(_element_view(element, doc), per_token)
            if current and used + cost > budget:
                groups.append(current)
                current, used = [], 0
            current.append(element)
            used += cost
        if current:
            groups.append(current)
        for parts in groups:
            view = "\n".join(_element_view(e, doc) for e in parts)
            tokens = estimate_tokens(view, per_token)
            sections = {b.section for k, b in parts if k == "block" and b.section}  # type: ignore[union-attr]
            sections |= {s for k, t in parts if k == "table" for s in t.sections}  # type: ignore[union-attr]
            ordered = [s for s in PLANNING_SECTIONS if s in sections] + sorted(
                sections - set(PLANNING_SECTIONS)
            )
            plans.append(
                ChunkPlan(
                    id=f"c{len(plans) + 1:03d}",
                    pages=[page.number],
                    blocks=[b.id for k, b in parts if k == "block"],
                    tables=[t.id for k, t in parts if k == "table"],
                    sections=ordered,
                    tasks=_tasks(page, parts),
                    tokens=tokens,
                    over_budget=tokens > budget,
                    priority=0 if ordered else 1,
                )
            )
    return sorted(plans, key=lambda p: (p.priority, p.pages[0], p.id))


# --- chunk -> the extraction contract's pages ---------------------------------------------------


def chunk_pages(doc: DocumentPages, chunk: ChunkPlan) -> list[PageInput]:
    """The pages of a chunk as the extraction contract reads them: ``text`` and ``words`` (what
    a citation must be found in, with boxes), the table grids (cell ids and boxes), and the
    ``view`` the model is shown."""
    wanted_blocks, wanted_tables = set(chunk.blocks), set(chunk.tables)
    out: list[PageInput] = []
    for number in chunk.pages:
        page = doc.page(number)
        texts: list[str] = []
        words: list[Word] = []
        views: list[str] = []
        grids: list[TableGrid] = []
        for element in elements(page):
            kind, item = element
            if kind == "block" and item.id in wanted_blocks:
                members = [item]
            elif kind == "table" and item.id in wanted_tables:
                members = [b for b in page.blocks if b.table == item.id]
                grids.append(
                    TableGrid(
                        id=item.id,
                        rows=tuple(
                            tuple(TableCell(c.id, c.text, c.bbox) for c in row)
                            for row in item.rows  # type: ignore[union-attr]
                        ),
                    )
                )
            else:
                continue
            views.append(_element_view(element, doc))
            for block in members:  # type: ignore[union-attr]
                texts.append(block.text)
                start, end = block.words
                words.extend(Word(w.text, w.bbox) for w in page.words[start:end])
        out.append(
            PageInput(
                page=number,
                text="\n".join(texts),
                method="ocr" if page.method == "ocr" else "text",
                words=tuple(words),
                tables=tuple(grids),
                view="\n".join(views),
            )
        )
    return out
