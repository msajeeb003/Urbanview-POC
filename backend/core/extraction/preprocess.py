"""PDF pre-processing: a planning PDF -> the pages the extraction prompts read.

``PREPROCESS_VERSION`` names this output; a change that alters it bumps the version.

Per page, with pymupdf (the ``gis`` extra, imported lazily): the text in reading order as blocks
with boxes, the words with boxes (every citation gets its box from them), the tables as cell grids
with cell boxes, the page size and rotation, and whether the page is scanned raster. Boxes are PDF
points with the origin bottom-left in the page's own user space (rotation undone), like the review
queue and the source viewer.

- **Text** is kept as extracted, NFC-composed (a decomposed č becomes one character, nothing is
  transliterated or "corrected"), Latin and Cyrillic alike; each page reports its script. On
  pages that show the AutoCAD glyph-id shift ("SRYUåLQH" for "površine"), the shifted words are
  decoded with ``core.gis`` and the block is marked ``decoded``.
- **Tables**: pymupdf's table finder on ruled lines (``lines_strict``, then ``lines``), with a
  text-alignment layout heuristic last (kept only when it looks like a table). Cell text is
  rebuilt from the page's lines: a word or number wrapped inside a narrow cell is joined
  ("Površin" + "a UP" -> "Površina UP", "1906.0" + "9" -> "1906.09"). Leading rows without numbers
  are the header. A header-less table with the columns of the table before it continues it
  (``continues``) and carries that table's column names (``header_from``). Skipped on drawing
  sheets (more than ``table_max_paths`` vector paths or larger than ``table_max_page_area``).
- **Scanned**: a page mostly covered by images, with (almost) no vector drawing, and no text layer
  or a text density below ``min_text_density`` -> ``scanned`` with its reason. It is read only by
  a configured :class:`OcrBackend` (``method: "ocr"``); otherwise it stays unread and is listed
  for manual handling (``method: "none"``). Text is never made up.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

PREPROCESS_VERSION = "1.1"
BBox = tuple[float, float, float, float]
Source = bytes | str | Path


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- the output -----------------------------------------------------------------------------------


class PageWord(_M):
    text: str
    bbox: BBox


class PageBlock(_M):
    id: str  # "p3b12"
    text: str  # lines joined with "\n"
    bbox: BBox
    words: tuple[int, int] = Field(description="[start, end) into the page's words")
    font_size: float | None = None
    bold: bool = False
    table: str | None = Field(default=None, description="The table the block sits in")
    decoded: bool = Field(default=False, description="Glyph-id shifted words decoded")
    section: str | None = Field(default=None, description="Planning section in effect (chunking)")


class GridCell(_M):
    id: str  # "r4c11"
    text: str
    bbox: BBox | None = None


class PageTable(_M):
    id: str  # "p3t1"
    bbox: BBox
    method: Literal["lines_strict", "lines", "layout"]
    header_rows: int = Field(description="Leading rows that are this fragment's own header")
    columns: list[str] = Field(description="Column names (own header, or the continued table's)")
    rows: list[list[GridCell]] = Field(description="All rows, header rows first")
    continues: str | None = Field(default=None, description="The fragment this one continues")
    header_from: str | None = Field(
        default=None, description="The fragment whose header rows name this one's columns"
    )
    sections: list[str] = Field(default_factory=list)


class Heading(_M):
    block: str
    text: str
    level: int | None = None
    section: str | None = None


class PageData(_M):
    number: int
    width: float  # PDF points, the page's user space (the box space)
    height: float
    rotation: int
    text: str  # the blocks in reading order, joined with "\n"
    blocks: list[PageBlock] = Field(default_factory=list)
    words: list[PageWord] = Field(default_factory=list)
    tables: list[PageTable] = Field(default_factory=list)
    char_count: int = 0
    image_coverage: float = 0.0
    path_count: int = 0
    scanned: bool = False
    scanned_reason: Literal["no_text_layer", "low_text_density"] | None = None
    blank: bool = False
    method: Literal["text", "ocr", "none"] = "text"
    script: Literal["latin", "cyrillic", "mixed", "none"] = "none"
    tables_skipped: Literal["drawing_sheet", "no_text"] | None = None
    glyph_decoded: bool = False
    headings: list[Heading] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)


class DocumentPages(_M):
    version: str = PREPROCESS_VERSION
    sha256: str
    page_count: int
    pages: list[PageData]

    def page(self, number: int) -> PageData:
        return next(p for p in self.pages if p.number == number)

    def table(self, table_id: str) -> PageTable | None:
        return next((t for p in self.pages for t in p.tables if t.id == table_id), None)


# --- options -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreprocessOptions:
    """What shapes the output (its hash is the cache key next to the checksum and version)."""

    min_text_density: float = 2.0  # characters per 10 000 pt² (A4 ~ 50) under which ...
    scanned_image_coverage: float = 0.5  # ... a page this much covered by images is a scan
    scanned_max_paths: int = 200  # vector drawings: a page with more is not a scan
    table_max_paths: int = 20_000  # table finding is skipped on drawing sheets ...
    table_max_page_area: float = 2_000_000.0  # ... or on pages larger than about A2 (pt²)
    chunk_token_budget: int = 6000
    chars_per_token: float = 3.0
    ocr: str = "none"  # the OCR backend's name (part of the cache key)

    def key(self) -> str:
        digest = hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()
        return digest[:16]

    @classmethod
    def from_settings(cls, settings: Any) -> PreprocessOptions:
        return cls(
            min_text_density=settings.preprocess_min_text_density,
            scanned_image_coverage=settings.preprocess_scanned_image_coverage,
            table_max_paths=settings.preprocess_table_max_paths,
            chunk_token_budget=settings.preprocess_chunk_token_budget,
            chars_per_token=settings.preprocess_chars_per_token,
            ocr=settings.extraction_ocr_backend,
        )


def ocr_from_settings(settings: Any) -> OcrBackend | None:
    """The configured OCR backend, or None: scanned pages then stay unread (flagged)."""
    if settings.extraction_ocr_backend == "tesseract":
        return TesseractOcr(settings.extraction_ocr_languages, settings.extraction_ocr_dpi)
    return None


# --- raw text (MuPDF page space: origin top-left, rotation applied) -----------------------------


@dataclass(slots=True)
class RawLine:
    text: str
    bbox: BBox
    words: list[tuple[str, BBox]]
    size: float | None = None
    bold: bool = False
    direction: tuple[float, float] = (1.0, 0.0)  # writing direction: (1, 0) horizontal


@dataclass(slots=True)
class RawBlock:
    bbox: BBox
    lines: list[RawLine] = field(default_factory=list)


class OcrBackend(Protocol):
    """Reads a scanned page into lines with boxes (MuPDF page space). Configured explicitly."""

    name: str

    def read(self, page: Any) -> list[RawBlock]: ...


def read_textpage(page: Any, textpage: Any) -> list[RawBlock]:
    """Blocks, lines and words of a pymupdf text page, in the page's content order."""
    data = page.get_text("dict", textpage=textpage)
    by_line: dict[tuple[int, int], list[tuple[int, str, BBox]]] = defaultdict(list)
    for x0, y0, x1, y1, text, block_no, line_no, word_no in page.get_text(
        "words", textpage=textpage
    ):
        by_line[(block_no, line_no)].append((word_no, text, (x0, y0, x1, y1)))
    blocks: list[RawBlock] = []
    for block in data["blocks"]:
        if block.get("type") != 0:
            continue
        lines: list[RawLine] = []
        for index, line in enumerate(block["lines"]):
            spans = [s for s in line["spans"] if s["text"]]
            text = "".join(s["text"] for s in spans)
            if not text.strip():
                continue
            words = [(t, b) for _, t, b in sorted(by_line.get((block["number"], index), []))]
            if not words:  # no word positions: the line's box for each word
                words = [(t, tuple(line["bbox"])) for t in text.split()]
            main = max(spans, key=lambda s: len(s["text"].strip()))
            lines.append(
                RawLine(
                    text=text,
                    bbox=tuple(line["bbox"]),
                    words=words,  # type: ignore[arg-type]
                    size=round(float(main["size"]), 1),
                    bold=bool(main["flags"] & 16),
                    direction=(round(line["dir"][0], 2), round(line["dir"][1], 2)),
                )
            )
        if lines:
            blocks.append(RawBlock(bbox=tuple(block["bbox"]), lines=lines))  # type: ignore[arg-type]
    return blocks


class TesseractOcr:
    """OCR through Tesseract via pymupdf (``get_textpage_ocr``). Needs Tesseract and its language
    data (TESSDATA_PREFIX); Montenegrin reads as Serbian Latin + Cyrillic (``srp_latn+srp``)."""

    name = "tesseract"

    def __init__(
        self, languages: str = "srp_latn+srp", dpi: int = 300, tessdata: str | None = None
    ):
        self.languages, self.dpi, self.tessdata = languages, dpi, tessdata

    def read(self, page: Any) -> list[RawBlock]:
        textpage = page.get_textpage_ocr(
            language=self.languages, dpi=self.dpi, full=True, tessdata=self.tessdata
        )
        return read_textpage(page, textpage)


# --- helpers -------------------------------------------------------------------------------------


def _pdf_box(box: Sequence[float], to_pdf: Any) -> BBox:
    import pymupdf

    rect = pymupdf.Rect(box) * to_pdf
    rect.normalize()
    return (round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1))


def _union(boxes: Sequence[BBox]) -> BBox:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _centre_in(box: BBox, area: BBox, pad: float = 1.0) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return area[0] - pad <= cx <= area[2] + pad and area[1] - pad <= cy <= area[3] + pad


def _coverage(boxes: Sequence[BBox], page: BBox, steps: int = 48) -> float:
    """Share of the page covered by the boxes, sampled on a grid."""
    if not boxes:
        return 0.0
    x0, y0, x1, y1 = page
    width, height = (x1 - x0) / steps, (y1 - y0) / steps
    hits = 0
    for i in range(steps):
        x = x0 + (i + 0.5) * width
        for j in range(steps):
            y = y0 + (j + 0.5) * height
            if any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in boxes):
                hits += 1
    return round(hits / (steps * steps), 3)


def detect_script(text: str) -> Literal["latin", "cyrillic", "mixed", "none"]:
    latin = cyrillic = 0
    for char in text:
        if char.isalpha():
            name = unicodedata.name(char, "")
            if name.startswith("LATIN"):
                latin += 1
            elif name.startswith("CYRILLIC"):
                cyrillic += 1
    total = latin + cyrillic
    if not total:
        return "none"
    share = cyrillic / total
    return "cyrillic" if share >= 0.9 else "latin" if share <= 0.1 else "mixed"


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _decode_glyph_shift(blocks: list[RawBlock]) -> set[int]:
    """Decode AutoCAD glyph-id shifted words, on pages that show the shift; the indexes of the
    blocks that changed."""
    from core.gis.inspect_pdf import decode_glyph_ids, fix_letters, looks_glyph_shifted

    tokens = (t for b in blocks for line in b.lines for t, _ in line.words)
    if not any(looks_glyph_shifted(t) for t in tokens):
        return set()

    def fix(token: str) -> str:
        return decode_glyph_ids(token) if looks_glyph_shifted(token) else fix_letters(token)

    changed: set[int] = set()
    for index, block in enumerate(blocks):
        for line in block.lines:
            text = " ".join(fix(t) for t in line.text.split(" "))
            if text != line.text:
                changed.add(index)
            line.text = text
            line.words = [(fix(t), b) for t, b in line.words]
    return changed


# --- tables --------------------------------------------------------------------------------------

_NUMERIC = re.compile(r"^[+\-]?\d[\d.,\s]*(?:%|m²|m2|ha|m)?$")
_STRATEGIES = ("lines_strict", "lines")
_TAIL = re.compile(r"^[a-zčćšžđ]{1,3}(?=\s|$)")


def _horizontal(line: RawLine) -> bool:
    return abs(line.direction[0]) >= 0.9


def _wrapped(previous: RawLine, following: RawLine, cell: BBox) -> bool:
    """A word or number wrapped inside a narrow cell (Word breaks a word only when it does not
    fit): the first part is one token running to the cell's edge and the rest continues it:
    a tail of one to three letters ("Površin" + "a UP", "izgrađeno" + "sti") or more digits
    ("1906.0" + "9")."""
    first, rest = previous.text.strip(), following.text.strip()
    if not first or not rest or " " in first or not _horizontal(previous):
        return False
    if (first[-1].isdigit() or (first[-1] in ".," and first[:-1].isdigit())) and rest[0].isdigit():
        tail_ok = len(rest) <= 3 and rest.isdigit()
    else:
        tail_ok = first[-1].isalpha() and _TAIL.match(rest) is not None
    if not tail_ok:
        return False
    char = (previous.bbox[2] - previous.bbox[0]) / max(len(first), 1)
    return previous.bbox[2] >= cell[2] - max(8.0, 2.5 * char)


def _reading_key(line: RawLine) -> tuple[float, float]:
    """Lines of a cell in reading order: top to bottom for horizontal text; for text rotated to
    read upwards the lines stack left to right, downwards right to left."""
    dx, dy = line.direction
    if abs(dx) >= 0.9:
        return (round(line.bbox[1], 0), line.bbox[0])
    return (line.bbox[0], 0.0) if dy < 0 else (-line.bbox[2], 0.0)


@dataclass(slots=True)
class _LineIndex:
    """The lines of a table by their vertical centre, for cell lookups."""

    lines: list[RawLine]
    centres: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.lines = sorted(self.lines, key=lambda line: (line.bbox[1] + line.bbox[3]) / 2)
        self.centres = [(line.bbox[1] + line.bbox[3]) / 2 for line in self.lines]

    def within(self, cell: BBox) -> list[RawLine]:
        low = bisect.bisect_left(self.centres, cell[1] - 1.0)
        high = bisect.bisect_right(self.centres, cell[3] + 1.0)
        return [line for line in self.lines[low:high] if _centre_in(line.bbox, cell)]


def cell_text(cell: BBox, lines: _LineIndex | Sequence[RawLine]) -> str:
    index = lines if isinstance(lines, _LineIndex) else _LineIndex(list(lines))
    parts: list[str] = []
    previous: RawLine | None = None
    for line in sorted(index.within(cell), key=_reading_key):
        text = " ".join(line.text.split())
        if not text:
            continue
        if previous is not None and parts and _wrapped(previous, line, cell):
            parts[-1] += text
        else:
            parts.append(text)
        previous = line
    return " ".join(parts)


def _header_like(row: Sequence[str]) -> bool:
    cells = [c for c in row if c.strip()]
    return bool(cells) and not any(_NUMERIC.match(c.strip()) for c in cells)


def _grid(table: Any, lines: Sequence[RawLine]) -> list[list[tuple[str, BBox | None]]]:
    """Rows of (text, MuPDF box) with the empty columns dropped."""
    index = _LineIndex([ln for ln in lines if _centre_in(ln.bbox, tuple(table.bbox), pad=2.0)])
    raw: list[list[tuple[str, BBox | None]]] = []
    for row in table.rows:
        raw.append([(cell_text(tuple(c), index), tuple(c)) if c else ("", None) for c in row.cells])
    width = max((len(r) for r in raw), default=0)
    keep = [j for j in range(width) if any(j < len(r) and r[j][0] for r in raw)]
    return [[r[j] if j < len(r) else ("", None) for j in keep] for r in raw]


def _looks_like_table(rows: list[list[tuple[str, BBox | None]]]) -> bool:
    """The layout heuristic finds tables in plain text too; keep only table-shaped ones."""
    if len(rows) < 3 or len(rows[0]) < 3:
        return False
    full = sum(1 for r in rows if sum(1 for t, _ in r if t) >= 3)
    numeric = any(_NUMERIC.match(t) for r in rows for t, _ in r if t)
    return full * 2 >= len(rows) and numeric


def _tables(page: Any, number: int, lines: Sequence[RawLine], to_pdf: Any) -> list[PageTable]:
    found: list[tuple[str, list[list[tuple[str, BBox | None]]], BBox]] = []
    for strategy in _STRATEGIES:
        for table in page.find_tables(strategy=strategy).tables:
            rows = _grid(table, lines)
            if len(rows) >= 2 and rows and len(rows[0]) >= 2:
                found.append((strategy, rows, tuple(table.bbox)))
        if found:
            break
    if not found:
        for table in page.find_tables(strategy="text").tables:
            rows = _grid(table, lines)
            if _looks_like_table(rows):
                found.append(("layout", rows, tuple(table.bbox)))
    out: list[PageTable] = []
    for index, (method, rows, box) in enumerate(found, start=1):
        texts = [[t for t, _ in row] for row in rows]
        header = 0
        while header < min(3, len(rows) - 1) and _header_like(texts[header]):
            header += 1
        columns = [
            " ".join(texts[r][j] for r in range(header) if texts[r][j]) for j in range(len(rows[0]))
        ]
        out.append(
            PageTable(
                id=f"p{number}t{index}",
                bbox=_pdf_box(box, to_pdf),
                method=method,  # type: ignore[arg-type]
                header_rows=header,
                columns=columns,
                rows=[
                    [
                        GridCell(id=f"r{i}c{j}", text=text, bbox=_pdf_box(b, to_pdf) if b else None)
                        for j, (text, b) in enumerate(row)
                    ]
                    for i, row in enumerate(rows)
                ],
            )
        )
    return out


def _column_edges(table: PageTable) -> list[float]:
    for row in table.rows:
        if all(cell.bbox is not None for cell in row):
            return [cell.bbox[0] for cell in row if cell.bbox is not None]
    return []


def _same_columns(a: PageTable, b: PageTable, tolerance: float = 3.0) -> bool:
    if not a.rows or not b.rows or len(a.rows[0]) != len(b.rows[0]):
        return False
    edges_a, edges_b = _column_edges(a), _column_edges(b)
    if not edges_a or not edges_b:
        return True  # same column count and no full row to compare edges with
    return all(abs(x - y) <= tolerance for x, y in zip(edges_a, edges_b, strict=True))


def stitch_tables(pages: Sequence[PageData]) -> None:
    """Stitch a table that runs over pages: a fragment without a header row that has the columns
    of the table before it continues it and takes the column names of the fragment that has
    them (repeated in its chunk, so the model always sees the column names)."""
    by_id: dict[str, PageTable] = {}
    previous: PageTable | None = None
    for page in pages:
        for table in page.tables:
            if previous is not None and table.header_rows == 0 and _same_columns(previous, table):
                root = by_id[previous.header_from] if previous.header_from else previous
                if root.header_rows > 0:
                    table.continues, table.header_from = previous.id, root.id
                    table.columns = list(root.columns)
            by_id[table.id] = table
            previous = table


# --- pages ---------------------------------------------------------------------------------------


def open_pdf(source: Source) -> Any:
    import pymupdf

    if isinstance(source, bytes):
        return pymupdf.open(stream=source, filetype="pdf")
    return pymupdf.open(str(source))


def _read_page(
    page: Any, number: int, options: PreprocessOptions, ocr: OcrBackend | None
) -> PageData:
    import pymupdf

    to_pdf = ~page.transformation_matrix
    display = tuple(page.rect)
    mediabox = page.mediabox
    textpage = page.get_textpage(
        flags=pymupdf.TEXT_PRESERVE_LIGATURES
        | pymupdf.TEXT_PRESERVE_WHITESPACE
        | pymupdf.TEXT_MEDIABOX_CLIP
    )
    raw = read_textpage(page, textpage)
    char_count = sum(len("".join(line.text.split())) for b in raw for line in b.lines)
    images = [tuple(info["bbox"]) for info in page.get_image_info()]
    coverage = _coverage(images, display)  # type: ignore[arg-type]
    path_count = sum(1 for kind, _ in page.get_bboxlog() if "path" in kind)
    area = abs(mediabox.width * mediabox.height)
    density = char_count / max(area / 10_000, 1e-9)

    scanned_reason: Literal["no_text_layer", "low_text_density"] | None = None
    if coverage >= options.scanned_image_coverage and path_count <= options.scanned_max_paths:
        if char_count == 0:
            scanned_reason = "no_text_layer"
        elif density < options.min_text_density:
            scanned_reason = "low_text_density"
    blank = char_count == 0 and scanned_reason is None and coverage < 0.05 and path_count == 0
    method: Literal["text", "ocr", "none"] = "text"
    if scanned_reason is not None:
        if ocr is not None:
            raw, method = ocr.read(page), "ocr"
        else:
            raw, method = [], "none"  # unread: listed for manual handling, never guessed
    elif char_count == 0:
        method = "none"

    raw.sort(key=lambda b: (round(b.bbox[1], 0), b.bbox[0]))
    decoded = _decode_glyph_shift(raw) if method == "text" else set()
    lines = [line for block in raw for line in block.lines]
    tables: list[PageTable] = []
    skipped: Literal["drawing_sheet", "no_text"] | None = None
    if not lines:
        skipped = "no_text"
    elif path_count > options.table_max_paths or area > options.table_max_page_area:
        skipped = "drawing_sheet"
    else:
        tables = _tables(page, number, lines, to_pdf)

    table_boxes = [(t.id, t.bbox) for t in tables]
    blocks: list[PageBlock] = []
    words: list[PageWord] = []
    for index, block in enumerate(raw):
        start = len(words)
        for line in block.lines:
            words.extend(PageWord(text=_nfc(t), bbox=_pdf_box(b, to_pdf)) for t, b in line.words)
        box = _pdf_box(block.bbox, to_pdf)
        sizes = [line.size for line in block.lines if line.size]
        blocks.append(
            PageBlock(
                id=f"p{number}b{index + 1}",
                text=_nfc("\n".join(" ".join(line.text.split()) for line in block.lines)),
                bbox=box,
                words=(start, len(words)),
                font_size=max(sizes) if sizes else None,
                bold=any(line.bold for line in block.lines),
                table=next((tid for tid, tbox in table_boxes if _centre_in(box, tbox)), None),
                decoded=index in decoded,
            )
        )
    text = "\n".join(block.text for block in blocks)
    return PageData(
        number=number,
        width=round(abs(mediabox.width), 1),
        height=round(abs(mediabox.height), 1),
        rotation=int(page.rotation),
        text=text,
        blocks=blocks,
        words=words,
        tables=tables,
        char_count=char_count,
        image_coverage=coverage,
        path_count=path_count,
        scanned=scanned_reason is not None,
        scanned_reason=scanned_reason,
        blank=blank,
        method=method,
        script=detect_script(text),
        tables_skipped=skipped,
        glyph_decoded=bool(decoded),
    )


def extract_pages(
    source: Source,
    *,
    options: PreprocessOptions | None = None,
    ocr: OcrBackend | None = None,
    pages: Sequence[int] | None = None,
) -> DocumentPages:
    """Every page (or the given 1-based ``pages``) of a PDF, with continued tables linked."""
    options = options or PreprocessOptions()
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    document = open_pdf(data)
    try:
        numbers = list(pages) if pages is not None else list(range(1, document.page_count + 1))
        read = [_read_page(document[n - 1], n, options, ocr) for n in numbers]
    finally:
        document.close()
    stitch_tables(read)
    return DocumentPages(sha256=hashlib.sha256(data).hexdigest(), page_count=len(read), pages=read)


# --- page images ---------------------------------------------------------------------------------


def iter_page_images(
    source: Source, *, dpi: int, max_pixels: int
) -> Iterator[tuple[int, bytes, int]]:
    """(page, PNG, effective dpi) for every page; large sheets are rendered at a lower dpi so no
    image exceeds ``max_pixels``."""
    import pymupdf

    document = open_pdf(source)
    try:
        for index in range(document.page_count):
            page = document[index]
            scale = dpi / 72
            pixels = page.rect.width * page.rect.height * scale * scale
            if pixels > max_pixels:
                scale = math.sqrt(max_pixels / (page.rect.width * page.rect.height))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            yield index + 1, pixmap.tobytes("png"), round(scale * 72)
    finally:
        document.close()
