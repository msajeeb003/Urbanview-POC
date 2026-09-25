"""A small synthetic plan sheet for the extraction tests, layered like an AutoCAD plot.

Page 600 x 420 pt at 1:1000 (1 pt = 0.3528 m on the ground), drawn with pymupdf:

- ``GRANICA``: the plan boundary (50, 50)-(550, 370);
- ``PARCELE``: the urban parcels UP 1 (100-250), UP 2 (250-400) and a UP 3 + UP 4 pair
  (400-500) whose separating edge is drawn only on the cadastral layer, all y 100-250; a stray
  line 0.2 pt inside UP 1 (a sliver); a legend box outside the boundary;
- ``KATASTAR``: cadastral lines (the fallback linework): x = 450 and a piece y = 210 east of it
  (inside the UP 3 / UP 4 pair), x = 325 across UP 2 (which must stay whole);
- ``OZNAKE``: the parcel numbers as text ("UP 9" sits in the legend);
- ``BLOKOVI``: block A as a dotted line of small filled circles 10 pt apart, (90, 90)-(410, 260),
  labelled "BLOK A" on ``BLOK_OZNAKE``;
- ``SAOBRACAJ``: a road centreline y = 310 as separate 12 pt dashes with 6 pt gaps;
- ``NAMJENA_SS``: land use SS over UP 1, filled as two triangles;
- ``BROJEVI``: glyph labels (``glyph_sheet``): text drawn as filled outlines.

Coordinates are pymupdf page coordinates (origin top-left); ``shift`` moves the whole drawing,
as a second sheet of the same plan plotted at another position would.
"""

from __future__ import annotations

import numpy as np
import pymupdf

W, H = 600.0, 420.0
SCALE = 1000
K = 0.0254 / 72 * SCALE  # ground metres per point

RULES_YAML = r"""
document: {id: synthetic, name: Synthetic plan}
sheets:
  - id: a
    file: sheet-a.pdf
    scale: 1000
    layers: [plan_boundary, urban_parcels, urban_blocks, planned_land_use, planned_traffic]
layers:
  plan_boundary:
    method: polygonize
    select: [{layer: GRANICA}]
    min_area_m2: 1000
  urban_parcels:
    method: polygonize
    select: [{layer: PARCELE}]
    fallback: [{layer: KATASTAR}]
    closing: [plan_boundary]
    keep: labelled
    labels:
      select: [{layer: OZNAKE}]
      pattern: 'UP\s*(\d+[a-z]?)'
      attribute: urban_parcel_number
  urban_blocks:
    method: polygonize
    select: [{layer: BLOKOVI}]
    gap_mm: 4.5
    labels:
      select: [{layer: BLOK_OZNAKE}]
      pattern: 'BLOK\s*([A-Z])'
      attribute: block_ref
  planned_land_use:
    method: fills
    categories:
      - {select: [{layer: NAMJENA_SS}], code: SS, name: Stanovanje}
  planned_traffic:
    method: lines
    select: [{layer: SAOBRACAJ}]
    gap_mm: 3
    road_class: street
"""


def _ocgs(doc: pymupdf.Document, names: list[str]) -> dict[str, int]:
    return {n: doc.add_ocg(n, on=True) for n in names}


def _line(page: pymupdf.Page, pts: list[tuple[float, float]], oc: int, width: float = 0.6) -> None:
    shape = page.new_shape()
    shape.draw_polyline([pymupdf.Point(*p) for p in pts])
    shape.finish(color=(0, 0, 0), width=width, closePath=False, oc=oc)
    shape.commit()


def plan_sheet(shift: tuple[float, float] = (0.0, 0.0)) -> bytes:
    dx, dy = shift

    def t(x: float, y: float) -> tuple[float, float]:
        return (x + dx, y + dy)

    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    oc = _ocgs(
        doc,
        [
            "GRANICA",
            "PARCELE",
            "KATASTAR",
            "OZNAKE",
            "BLOKOVI",
            "BLOK_OZNAKE",
            "SAOBRACAJ",
            "NAMJENA_SS",
        ],
    )
    _line(page, [t(50, 50), t(550, 50), t(550, 370), t(50, 370), t(50, 50)], oc["GRANICA"])
    # parcels: outer ring, inner edges, a stray line 0.2 pt inside UP 1, a legend box
    _line(page, [t(100, 100), t(500, 100), t(500, 250), t(100, 250), t(100, 100)], oc["PARCELE"])
    _line(page, [t(250, 100), t(250, 250)], oc["PARCELE"])
    _line(page, [t(400, 100), t(400, 250)], oc["PARCELE"])
    _line(page, [t(100, 100.2), t(250, 100.2)], oc["PARCELE"])
    _line(page, [t(555, 375), t(595, 375), t(595, 415), t(555, 415), t(555, 375)], oc["PARCELE"])
    _line(page, [t(450, 100), t(450, 250)], oc["KATASTAR"], 0.3)
    _line(page, [t(450, 210), t(500, 210)], oc["KATASTAR"], 0.3)
    _line(page, [t(325, 100), t(325, 250)], oc["KATASTAR"], 0.3)
    for text, (x, y) in {
        "UP 1": (160, 180),
        "UP 2": (295, 180),
        "UP 3": (410, 180),
        "UP 4": (460, 150),
        "UP 9": (560, 400),
    }.items():
        page.insert_text(pymupdf.Point(*t(x, y)), text, fontsize=10, oc=oc["OZNAKE"])
    # block A: filled dots every 10 pt along (90, 90)-(410, 260)
    corners = [(90, 90), (410, 90), (410, 260), (90, 260), (90, 90)]
    for (x0, y0), (x1, y1) in zip(corners, corners[1:], strict=False):
        n = round(max(abs(x1 - x0), abs(y1 - y0)) / 10)
        for i in range(n):
            cx, cy = x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n
            shape = page.new_shape()
            shape.draw_circle(pymupdf.Point(*t(cx, cy)), 1.2)
            shape.finish(color=None, fill=(0.4, 0.4, 0.4), oc=oc["BLOKOVI"])
            shape.commit()
    page.insert_text(pymupdf.Point(*t(150, 230)), "BLOK A", fontsize=12, oc=oc["BLOK_OZNAKE"])
    # road centreline: 12 pt dashes, 6 pt gaps
    x = 50.0
    while x < 550:
        _line(page, [t(x, 310), t(min(x + 12, 550), 310)], oc["SAOBRACAJ"], 0.5)
        x += 18
    # land use SS over UP 1 as two filled triangles (a tessellated solid hatch)
    for tri in ([(100, 100), (250, 100), (250, 250)], [(100, 100), (250, 250), (100, 250)]):
        shape = page.new_shape()
        shape.draw_polyline([pymupdf.Point(*t(*p)) for p in tri])
        shape.finish(color=None, fill=(0.6, 0.9, 0.6), closePath=True, oc=oc["NAMJENA_SS"])
        shape.commit()
    return doc.tobytes()


def glyph_runs(
    text: str, cap_height_pt: float
) -> tuple[list[list[tuple[float, float, float, float]]], float]:
    """``text`` as filled glyph shapes: one list of rectangles per glyph (pixel runs of the text
    rendered with MuPDF's Helvetica), baseline-left at (0, 0), y down; plus the text width."""
    tmp = pymupdf.open()
    pg = tmp.new_page(width=1200, height=300)
    tw = pymupdf.TextWriter(pg.rect)
    tw.append((20, 200), text, font=pymupdf.Font("helv"), fontsize=150)
    tw.write_text(pg)
    pix = pg.get_pixmap(alpha=False, colorspace=pymupdf.csGRAY)
    ink = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width) < 128
    rows = np.nonzero(ink.any(axis=1))[0]
    top, bottom = int(rows.min()), int(rows.max())
    cols = ink.any(axis=0)
    xs = np.nonzero(cols)[0]
    left = int(xs.min())
    scale = cap_height_pt / (bottom - top + 1)
    glyphs: list[list[tuple[float, float, float, float]]] = []
    c = left
    while c <= xs.max():
        if not cols[c]:
            c += 1
            continue
        c1 = c
        while c1 + 1 < len(cols) and cols[c1 + 1]:
            c1 += 1
        rects = []
        for r in range(top, bottom + 1):
            run = ink[r, c : c1 + 1]
            i = 0
            while i < len(run):
                if run[i]:
                    j = i
                    while j + 1 < len(run) and run[j + 1]:
                        j += 1
                    x0 = (c + i - left) * scale
                    x1 = (c + j + 1 - left) * scale
                    y0 = -(bottom + 1 - r) * scale
                    rects.append((x0, y0, x1, y0 + scale))
                    i = j + 1
                else:
                    i += 1
        glyphs.append(rects)
        c = c1 + 1
    return glyphs, (int(xs.max()) + 1 - left) * scale


def glyph_sheet(labels: dict[str, tuple[float, float]], cap_height_pt: float = 8.5) -> bytes:
    """A sheet whose labels are drawn as filled glyph outlines on layer ``BROJEVI``."""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    oc = _ocgs(doc, ["BROJEVI"])
    for text, (x, y) in labels.items():
        glyphs, _ = glyph_runs(text, cap_height_pt)
        for rects in glyphs:
            shape = page.new_shape()
            for x0, y0, x1, y1 in rects:
                shape.draw_rect(pymupdf.Rect(x + x0, y + y0, x + x1, y + y1))
            shape.finish(color=None, fill=(0, 0, 0), oc=oc["BROJEVI"])
            shape.commit()
    return doc.tobytes()
