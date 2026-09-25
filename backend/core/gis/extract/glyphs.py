"""Read labels drawn as vector glyph outlines (text the plotter turned into filled shapes).

Some sheets carry their parcel numbers as filled outlines instead of text (Novi Grad's
``!BROJEVI UP``). Each outline is rasterised on a small grid and matched against the characters
of a template font rendered the same way (MuPDF's built-in Helvetica by default, so no system
font or OCR engine is needed); glyphs are grouped into labels along the displayed baseline.
Deterministic: the same page always reads the same text.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

import numpy as np
import pymupdf
import shapely
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from core.gis.extract.rules import LabelRule
from core.gis.extract.sheet import Sheet, SheetPath, subpaths
from core.gis.inspect_pdf import MM_PER_PT

GRID = (24, 16)  # rows, columns of the comparison raster
ASPECT_WEIGHT = 0.3
HEIGHT_WEIGHT = 0.6
BASELINE_TOLERANCE = 0.25  # of the label's glyph height
SPACE_GAP = 0.45  # of the cap height: a wider gap is written as a space (digits keep
# their side bearings, "1" alone leaves ~0.3)


@dataclass(frozen=True, slots=True)
class GlyphLabel:
    text: str
    bbox: tuple[float, float, float, float]  # page frame
    score: float  # the weakest character's match score
    path_ids: tuple[str, ...]


@dataclass(slots=True)
class _Glyph:
    path: SheetPath
    rings: list[list[tuple[float, float]]]  # display frame
    bbox: tuple[float, float, float, float]  # display frame


def _ink_grid(pix: pymupdf.Pixmap) -> tuple[np.ndarray, float]:
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width) < 128
    ys, xs = np.nonzero(a)
    if len(xs) == 0:
        return np.zeros(GRID, bool), 1.0
    ink = a[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    rows, cols = GRID
    h, w = ink.shape
    yi = ((np.arange(rows) + 0.5) * h / rows).astype(int)
    xi = ((np.arange(cols) + 0.5) * w / cols).astype(int)
    return ink[yi[:, None], xi[None, :]], w / h


@cache
def _templates(font: str, chars: str) -> dict[str, tuple[np.ndarray, float, float]]:
    """char -> (grid, ink aspect, ink height relative to the capital H)."""
    f = (
        pymupdf.Font(fontfile=font)
        if font.lower().endswith((".ttf", ".otf"))
        else pymupdf.Font(font)
    )
    out: dict[str, tuple[np.ndarray, float, float]] = {}
    heights: dict[str, float] = {}
    for ch in dict.fromkeys(chars + "H"):
        doc = pymupdf.open()
        page = doc.new_page(width=220, height=220)
        tw = pymupdf.TextWriter(page.rect)
        tw.append((30, 160), ch, font=f, fontsize=120)
        tw.write_text(page)
        pix = page.get_pixmap(alpha=False, colorspace=pymupdf.csGRAY)
        grid, aspect = _ink_grid(pix)
        a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width) < 128
        ys = np.nonzero(a)[0]
        heights[ch] = float(ys.max() - ys.min() + 1) if len(ys) else 1.0
        out[ch] = (grid, aspect, 0.0)
    cap = heights["H"]
    return {ch: (g, asp, heights[ch] / cap) for ch, (g, asp, _) in out.items() if ch in chars}


def _outline(rings: list[list[tuple[float, float]]], even_odd: bool) -> BaseGeometry:
    """The filled area of a glyph's rings under its fill rule (non-zero: the rings inside the
    largest one are its counters)."""
    polys = []
    for r in rings:
        poly = Polygon(r)
        if not poly.is_valid:
            poly = shapely.make_valid(poly)
        if not poly.is_empty and poly.area > 0:
            polys.append(poly)
    if not polys:
        return Polygon()
    if even_odd:
        acc = polys[0]
        for poly in polys[1:]:
            acc = acc.symmetric_difference(poly)
        return acc
    polys.sort(key=lambda q: -q.area)
    acc = polys[0]
    for poly in polys[1:]:
        acc = acc.difference(poly) if polys[0].contains(poly) else acc.union(poly)
    return acc


def _raster(rings: list[list[tuple[float, float]]], even_odd: bool) -> tuple[np.ndarray, float]:
    """The glyph sampled at the centres of a GRID over its ink box (as ``_ink_grid`` samples
    a rendered template), and the box's aspect ratio."""
    shape = _outline(rings, even_odd)
    if shape.is_empty:
        return np.zeros(GRID, bool), 1.0
    x0, y0, x1, y1 = shape.bounds
    rows, cols = GRID
    xs = x0 + (np.arange(cols) + 0.5) * (x1 - x0) / cols
    ys = y0 + (np.arange(rows) + 0.5) * (y1 - y0) / rows
    gx, gy = np.meshgrid(xs, ys)
    inside = shapely.contains_xy(shape, gx.ravel(), gy.ravel()).reshape(rows, cols)
    return inside, (x1 - x0) / max(y1 - y0, 1e-9)


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    union = np.logical_or(a, b).sum()
    return float(np.logical_and(a, b).sum() / union) if union else 0.0


def _display(sheet: Sheet, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = sheet.rotation_matrix
    return (x * a + y * c + e, x * b + y * d + f)


def _page(sheet: Sheet, x: float, y: float) -> tuple[float, float]:
    """Inverse of the rotation matrix (display frame -> page frame)."""
    a, b, c, d, e, f = sheet.rotation_matrix
    det = a * d - b * c
    xs, ys = x - e, y - f
    return ((xs * d - ys * c) / det, (ys * a - xs * b) / det)


def read_glyph_labels(sheet: Sheet, rule: LabelRule) -> list[GlyphLabel]:
    lo, hi = rule.glyph_height_mm
    glyphs: list[_Glyph] = []
    for p in sheet.select(rule.select):
        if p.fill is None:
            continue
        rings = [
            [_display(sheet, x, y) for x, y in pts] for pts in subpaths(p.items) if len(pts) >= 3
        ]
        if not rings:
            continue
        xs = [q[0] for r in rings for q in r]
        ys = [q[1] for r in rings for q in r]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        if lo <= (bbox[3] - bbox[1]) * MM_PER_PT <= hi:
            glyphs.append(_Glyph(p, rings, bbox))
    glyphs.sort(key=lambda g: (round(g.bbox[0], 2), round(g.bbox[1], 2)))

    # glyphs of one label: on one baseline, each close after the previous one
    labels: list[list[_Glyph]] = []
    for g in glyphs:
        gh = g.bbox[3] - g.bbox[1]
        for lab in labels:
            last = lab[-1].bbox
            h = max(max(x.bbox[3] - x.bbox[1] for x in lab), gh)
            same_line = abs(last[3] - g.bbox[3]) <= BASELINE_TOLERANCE * h
            if same_line and -0.1 * h <= g.bbox[0] - last[2] <= rule.max_gap * h:
                lab.append(g)
                break
        else:
            labels.append([g])

    templates = _templates(rule.font, rule.glyph_chars)
    out: list[GlyphLabel] = []
    for lab in labels:
        cap = max(x.bbox[3] - x.bbox[1] for x in lab)
        text, scores = "", []
        for i, g in enumerate(lab):
            if i:
                gap = g.bbox[0] - lab[i - 1].bbox[2]
                if gap > SPACE_GAP * cap:
                    text += " "
            grid, aspect = _raster(g.rings, g.path.even_odd)
            rel = (g.bbox[3] - g.bbox[1]) / cap
            best, best_score = "?", -1.0
            for ch, (tg, ta, th) in templates.items():
                s = (
                    _iou(grid, tg)
                    - ASPECT_WEIGHT * abs(aspect - ta)
                    - HEIGHT_WEIGHT * abs(rel - th)
                )
                if s > best_score or (s == best_score and ch < best):
                    best, best_score = ch, s
            text += best
            scores.append(best_score)
        x0 = min(g.bbox[0] for g in lab)
        y0 = min(g.bbox[1] for g in lab)
        x1 = max(g.bbox[2] for g in lab)
        y1 = max(g.bbox[3] for g in lab)
        corners = [_page(sheet, x, y) for x, y in ((x0, y0), (x1, y1))]
        px = sorted(c[0] for c in corners)
        py = sorted(c[1] for c in corners)
        out.append(
            GlyphLabel(
                text=text,
                bbox=(px[0], py[0], px[1], py[1]),
                score=round(min(scores), 3),
                path_ids=tuple(g.path.id for g in lab),
            )
        )
    return out
