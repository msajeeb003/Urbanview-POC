"""The pages an extraction reads: page text (exactly what the model sees and what every citation is
checked against), the words with their boxes, and optional table grids with cell ids.

``read_pdf_pages`` builds them from a PDF with pymupdf (the ``gis`` extra, imported lazily).
Coordinates are PDF points with the origin bottom-left, like the review queue and the source
viewer. ``render_pages`` is the page block of the prompt: page markers carry the 1-based page
number the model cites; table grids name every cell ``rNcM`` so a value can cite its cell.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.extraction.textmatch import BBox, round_box


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    bbox: BBox  # PDF points, origin bottom-left


@dataclass(frozen=True, slots=True)
class TableCell:
    id: str  # "r4c11"
    text: str
    bbox: BBox | None = None


@dataclass(frozen=True, slots=True)
class TableGrid:
    id: str  # "p7t1": page 7, first table
    rows: tuple[tuple[TableCell, ...], ...]


def merged_origin(
    rows: Sequence[Sequence[Any]], index: int, col: int, first_row: int = 0
) -> int | None:
    """The earlier row whose cell in ``col`` is merged over row ``index``: a value printed once
    for several rows keeps its text (and a box spanning them) in the first row and leaves the
    other rows' cells empty and without a box. Cells need ``text`` and ``bbox``."""
    row = rows[index]
    if col >= len(row) or row[col].text or row[col].bbox is not None:
        return None
    boxes = [c.bbox for c in row if c.bbox is not None]
    if not boxes:
        return None
    low, high = min(b[1] for b in boxes), max(b[3] for b in boxes)
    for k in range(index - 1, first_row - 1, -1):
        if col >= len(rows[k]):
            return None
        cell = rows[k][col]
        if cell.bbox is None:
            continue
        if cell.bbox[1] <= low + 1 and cell.bbox[3] >= high - 1 and cell.text:
            return k
        return None
    return None


@dataclass(frozen=True, slots=True)
class PageInput:
    page: int  # 1-based page of the source PDF
    text: str  # what every citation on this page is checked against
    method: Literal["text", "ocr"] = "text"
    words: tuple[Word, ...] = ()
    tables: tuple[TableGrid, ...] = ()
    view: str | None = None  # what the model reads (pre-processed chunks); None = text + tables

    def cell(self, cell_id: str, table_id: str | None = None) -> TableCell | None:
        for table in self.tables:
            if table_id and table.id != table_id:
                continue
            for row in table.rows:
                for cell in row:
                    if cell.id == cell_id:
                        return cell
        return None

    def origin_cell(self, cell_id: str, table_id: str | None = None) -> TableCell | None:
        """The cell whose merged text covers ``cell_id`` (an empty cell of a row the value is
        printed once for), or None."""
        for table in self.tables:
            if table_id and table.id != table_id:
                continue
            for r, row in enumerate(table.rows):
                for c, cell in enumerate(row):
                    if cell.id == cell_id:
                        k = merged_origin(table.rows, r, c)
                        return table.rows[k][c] if k is not None else None
        return None


def render_pages(pages: Sequence[PageInput]) -> str:
    """The pages as the model reads them."""
    blocks: list[str] = []
    for page in pages:
        if page.view is not None:
            blocks.append(
                f"=== Page {page.page} ===\n{page.view.rstrip()}\n=== End of page {page.page} ==="
            )
            continue
        lines = [f"=== Page {page.page} ===", page.text.rstrip()]
        for table in page.tables:
            lines.append(f"--- Table {table.id} (page {page.page}); cells are named rNcM ---")
            for r, row in enumerate(table.rows):
                cells = [f"{c.id}: {' '.join(c.text.split())}" for c in row if c.text.strip()]
                if cells:
                    lines.append(f"r{r} | " + " | ".join(cells))
        lines.append(f"=== End of page {page.page} ===")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# --- PDF ------------------------------------------------------------------------------------------


def _pdf_box(rect: Any, to_pdf: Any) -> BBox:
    import pymupdf

    r = pymupdf.Rect(rect) * to_pdf
    r.normalize()
    return round_box((r.x0, r.y0, r.x1, r.y1))


def _grids(page: Any, number: int, to_pdf: Any) -> tuple[TableGrid, ...]:
    grids: list[TableGrid] = []
    for t_index, table in enumerate(page.find_tables().tables, start=1):
        texts = table.extract()
        if not texts:
            continue
        width = max(len(row) for row in texts)
        keep = [
            j
            for j in range(width)
            if any(j < len(row) and row[j] and row[j].strip() for row in texts)
        ]
        rows: list[tuple[TableCell, ...]] = []
        for i, row in enumerate(texts):
            boxes = table.rows[i].cells if i < len(table.rows) else []
            cells = []
            for new_j, j in enumerate(keep):
                text = (row[j] or "") if j < len(row) else ""
                box = boxes[j] if j < len(boxes) else None
                cells.append(
                    TableCell(
                        id=f"r{i}c{new_j}",
                        text=" ".join(text.split()),
                        bbox=_pdf_box(box, to_pdf) if box else None,
                    )
                )
            rows.append(tuple(cells))
        grids.append(TableGrid(id=f"p{number}t{t_index}", rows=tuple(rows)))
    return tuple(grids)


def read_pdf_pages(
    source: str | Path | bytes, pages: Iterable[int] | None = None, *, tables: bool = False
) -> list[PageInput]:
    """Text, words and (optionally) table grids of the given 1-based pages (all by default)."""
    import pymupdf

    doc = (
        pymupdf.open(stream=source, filetype="pdf")
        if isinstance(source, bytes)
        else pymupdf.open(str(source))
    )
    try:
        numbers = list(pages) if pages is not None else list(range(1, doc.page_count + 1))
        out: list[PageInput] = []
        for number in numbers:
            page = doc[number - 1]
            to_pdf = ~page.transformation_matrix
            words = tuple(
                Word(text=w[4], bbox=_pdf_box(w[:4], to_pdf))
                for w in page.get_text("words")
                if w[4].strip()
            )
            out.append(
                PageInput(
                    page=number,
                    text=page.get_text("text"),
                    words=words,
                    tables=_grids(page, number, to_pdf) if tables else (),
                )
            )
        return out
    finally:
        doc.close()
