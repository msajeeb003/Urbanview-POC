"""A small planning PDF for the pre-processing tests (pymupdf):

1. a numbered heading, a ruled parameter table whose header cell "Površina UP" and number
   "1906.09" are wrapped inside narrow cells, and a note with every Montenegrin diacritic;
2. the table continued without its header row, then an infrastructure heading and text;
3. an image-only page (a scan: no text layer);
4. a Cyrillic heading and text;
5. a blank page;
6. an AutoCAD glyph-id shifted label ("SRYUåLQH" = "površine").

Pages 1, 2, 4 and 6 carry the same bold running header (it must not count as a heading). Text is
set in pymupdf's embedded Helvetica so diacritics and Cyrillic round-trip.
"""

from __future__ import annotations

import pymupdf

RUNNING = "Izmjene i dopune DUP-a „Test” u Podgorici"
DIACRITICS = "č ć š ž đ Č Ć Š Ž Đ"
NOTE = f"Napomena: građevinska linija je na 5,0 m od regulacione linije. {DIACRITICS}"
HEADING_1 = "4. Urbanistički parametri"
HEADING_2 = "5. Infrastruktura"
HEADING_4 = "6. Инфраструктура"
CYRILLIC = "Канализација: постојећа мрежа, пречник 300 мм."
SHIFTED = "SRYUåLQH"  # "površine" after the glyph-id shift

# (header lines, width); a header line list is stacked top to bottom in its cell
COLUMNS: list[tuple[list[str], float]] = [
    (["Blok"], 40),
    (["Broj UP"], 50),
    (["Površin", "a UP"], 44),  # wrapped mid-word: the first part runs to the cell's edge
    (["Namjena"], 150),
    (["Spratnost"], 60),
    (["Indeks", "zauzetosti"], 70),
    (["Indeks", "izgrađenosti"], 76),
]
HEADERS = ["Blok", "Broj UP", "Površina UP", "Namjena", "Spratnost", "Indeks zauzetosti",
           "Indeks izgrađenosti"]  # fmt: skip
ROWS_1 = [
    ["A", "UP 1", ["1906.0", "9"], "stanovanje sa djelatnostima", "Po+P+6", "0,40", "2,4"],
    ["A", "UP 2", "850,50", "mješovita namjena", "S+P+4+Pk", "0,35", "1,8"],
    ["A", "UP 3", "1022,25", "školstvo i socijalna zaštita", "P+2", "0,30", "0,9"],
]
ROWS_2 = [
    ["B", "UP 4", "640,00", "stanovanje", "P+3", "0,45", "1,6"],
    ["B", "UP 5", "712,40", "centralne djelatnosti", "S+P+4", "0,50", "2,2"],
]
LEFT, SIZE, HEADER_HEIGHT, ROW_HEIGHT = 40.0, 9.0, 36.0, 22.0

REGULAR = pymupdf.Font("helv")
BOLD = pymupdf.Font("hebo")


def _fonts(page: pymupdf.Page) -> None:
    page.insert_font(fontname="F0", fontbuffer=REGULAR.buffer)
    page.insert_font(fontname="F1", fontbuffer=BOLD.buffer)


def _text(page: pymupdf.Page, x: float, y: float, text: str, size: float = 10, bold=False):
    page.insert_text((x, y), text, fontname="F1" if bold else "F0", fontsize=size)


def _edges() -> list[float]:
    edges = [LEFT]
    for _, width in COLUMNS:
        edges.append(edges[-1] + width)
    return edges


def _table(page: pymupdf.Page, top: float, rows: list[list], header: bool) -> float:
    edges = _edges()
    heights = ([HEADER_HEIGHT] if header else []) + [ROW_HEIGHT] * len(rows)
    bottom = top + sum(heights)
    y = top
    for height in [0.0, *heights]:
        y += height
        page.draw_line((edges[0], y), (edges[-1], y), color=(0, 0, 0), width=0.6)
    for x in edges:
        page.draw_line((x, top), (x, bottom), color=(0, 0, 0), width=0.6)
    y = top
    if header:
        for j, (lines, width) in enumerate(COLUMNS):
            for k, line in enumerate(lines):
                if k == 0 and len(lines) > 1 and line == "Površin":
                    x = edges[j] + width - 3 - REGULAR.text_length(line, SIZE)  # to the edge
                else:
                    x = edges[j] + 3
                _text(page, x, y + 14 + k * 11, line, SIZE)
        y += HEADER_HEIGHT
    for row in rows:
        for j, value in enumerate(row):
            if isinstance(value, list):  # a number wrapped inside its narrow cell
                width = COLUMNS[j][1]
                first = edges[j] + width - 3 - REGULAR.text_length(value[0], SIZE)
                _text(page, first, y + 9, value[0], SIZE)
                _text(page, edges[j] + 3, y + 19, value[1], SIZE)
            else:
                _text(page, edges[j] + 3, y + 14, value, SIZE)
        y += ROW_HEIGHT
    return bottom


def planning_pdf() -> bytes:
    doc = pymupdf.open()
    # 1: heading, table with header, note
    page = doc.new_page()
    _fonts(page)
    _text(page, LEFT, 30, RUNNING, 9, bold=True)
    _text(page, LEFT, 64, HEADING_1, 14, bold=True)
    _text(page, LEFT, 88, "Parametri su dati po urbanističkim parcelama.")
    bottom = _table(page, 110, ROWS_1, header=True)
    _text(page, LEFT, bottom + 24, NOTE)
    # 2: continuation without header, infrastructure section
    page = doc.new_page()
    _fonts(page)
    _text(page, LEFT, 30, RUNNING, 9, bold=True)
    bottom = _table(page, 60, ROWS_2, header=False)
    _text(page, LEFT, bottom + 40, HEADING_2, 14, bold=True)
    _text(page, LEFT, bottom + 64, "Vodovod: planirani priključak na uličnu mrežu.")
    # 3: a scan
    page = doc.new_page()
    scan = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 170), False)
    scan.clear_with(200)
    page.insert_image(page.rect, pixmap=scan)
    # 4: Cyrillic
    page = doc.new_page()
    _fonts(page)
    _text(page, LEFT, 30, RUNNING, 9, bold=True)
    _text(page, LEFT, 64, HEADING_4, 14, bold=True)
    _text(page, LEFT, 88, CYRILLIC)
    # 5: blank
    doc.new_page()
    # 6: a glyph-id shifted CAD label
    page = doc.new_page()
    _fonts(page)
    _text(page, LEFT, 30, RUNNING, 9, bold=True)
    _text(page, LEFT, 64, SHIFTED)
    data = doc.tobytes()
    doc.close()
    return data
