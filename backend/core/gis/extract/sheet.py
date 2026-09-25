"""One sheet (a page of a plan PDF) as the extraction sees it.

Three frames:

- **page**: pymupdf's coordinates of the unrotated page (points, origin top-left, y down);
  drawings and text come in it;
- **sheet**: the PDF's own user space (points, origin bottom-left, y up), the frame of every
  ``source_bbox`` (the review queue's convention) and of ``SheetRule.exclude``;
- **local**: ground metres, x east, y north, shared by all sheets of a document:
  ``local = sheet * (scale * 0.0254 / 72) + offset_m``. Not georeferenced; ticket 08 fits it.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

import pymupdf
import shapely
from shapely import affinity
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry

from core.gis.extract.rules import Selector, SheetRule
from core.gis.inspect_pdf import M_PER_PT, MM_PER_PT, fix_letters

CURVE_STEPS = 6  # chords per Bézier curve when flattening


def _hex(colour: Any) -> str | None:
    if colour is None:
        return None
    return "#" + "".join(f"{round(c * 255):02x}" for c in colour[:3])


@dataclass(slots=True)
class SheetPath:
    """A drawing path with its style and layer; ``id`` is stable for the same file."""

    id: str
    layer: str
    stroke: str | None
    fill: str | None
    width: float
    dashed: bool
    even_odd: bool
    close: bool
    items: list[tuple]
    rect: tuple[float, float, float, float]

    @property
    def size_mm(self) -> float:
        x0, y0, x1, y1 = self.rect
        return max(x1 - x0, y1 - y0) * MM_PER_PT

    def style_key(self) -> tuple:
        dash = "dashed" if self.dashed else "solid"
        return (self.layer, self.stroke or "-", self.fill or "-", round(self.width, 2), dash)


@dataclass(slots=True)
class TextSpan:
    text: str
    layer: str
    bbox: tuple[float, float, float, float]  # page frame


@dataclass
class Sheet:
    rule: SheetRule
    width_pt: float
    height_pt: float
    rotation: int
    paths: list[SheetPath]
    texts: list[TextSpan]
    rotation_matrix: tuple[float, ...] = (1, 0, 0, 1, 0, 0)

    # --- frames ----------------------------------------------------------------------------

    @property
    def metres_per_pt(self) -> float:
        return M_PER_PT * self.rule.scale

    def page_to_local(self) -> list[float]:
        """shapely affine matrix [a, b, d, e, xoff, yoff]: page frame -> local metres."""
        k = self.metres_per_pt
        ox, oy = self.rule.offset_m
        return [k, 0.0, 0.0, -k, ox, self.height_pt * k + oy]

    def local_to_page(self) -> list[float]:
        k = self.metres_per_pt
        ox, oy = self.rule.offset_m
        return [1 / k, 0.0, 0.0, -1 / k, -ox / k, self.height_pt + oy / k]

    def to_local(self, geom: BaseGeometry) -> BaseGeometry:
        return affinity.affine_transform(geom, self.page_to_local())

    def to_page(self, geom: BaseGeometry) -> BaseGeometry:
        return affinity.affine_transform(geom, self.local_to_page())

    def sheet_bbox(self, local_bounds: tuple[float, float, float, float]) -> list[float]:
        """A local-frame box as the sheet's PDF points (origin bottom-left), 1 decimal."""
        k = self.metres_per_pt
        ox, oy = self.rule.offset_m
        x0, y0, x1, y1 = local_bounds
        return [
            round((x0 - ox) / k, 1),
            round((y0 - oy) / k, 1),
            round((x1 - ox) / k, 1),
            round((y1 - oy) / k, 1),
        ]

    def excluded(self, page_rect: tuple[float, float, float, float]) -> bool:
        if not self.rule.exclude:
            return False
        x0, y0, x1, y1 = page_rect
        cx, cy = (x0 + x1) / 2, self.height_pt - (y0 + y1) / 2  # sheet frame
        return any(a <= cx <= c and b <= cy <= d for a, b, c, d in self.rule.exclude)

    # --- selection -------------------------------------------------------------------------

    def select(self, selectors: list[Selector]) -> list[SheetPath]:
        if not selectors:
            return []
        out = []
        for p in self.paths:
            if self.excluded(p.rect):
                continue
            if any(_path_matches(s, p) for s in selectors):
                out.append(p)
        return out

    def select_texts(self, selectors: list[Selector]) -> list[TextSpan]:
        return [
            t
            for t in self.texts
            if not self.excluded(t.bbox) and any(s.matches_layer(t.layer) for s in selectors)
        ]


def _path_matches(s: Selector, p: SheetPath) -> bool:
    if not s.matches_layer(p.layer):
        return False
    if s.stroke is not None and (p.stroke or "none") != s.stroke:
        return False
    if s.fill is not None and (p.fill or "none") != s.fill:
        return False
    if s.width_min is not None and p.width < s.width_min:
        return False
    if s.width_max is not None and p.width > s.width_max:
        return False
    if s.dashed is not None and p.dashed != s.dashed:
        return False
    if s.filled is not None and (p.fill is not None) != s.filled:
        return False
    size = p.size_mm
    if s.min_size_mm is not None and size < s.min_size_mm:
        return False
    return not (s.max_size_mm is not None and size > s.max_size_mm)


def load_sheet(pdf: bytes, rule: SheetRule, keep: list[Selector] | None = None) -> Sheet:
    """Read one page; with ``keep``, only the paths some selector could take are kept."""
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    if rule.page < 1 or rule.page > doc.page_count:
        raise ValueError(f"sheet {rule.id}: page {rule.page} of {doc.page_count}")
    page = doc[rule.page - 1]
    paths: list[SheetPath] = []
    for d in page.get_cdrawings():
        layer = d.get("layer") or ""
        if keep is not None and not any(s.matches_layer(layer) for s in keep):
            continue
        dashes = (d.get("dashes") or "").strip()
        paths.append(
            SheetPath(
                id=f"p{rule.page}:{d.get('seqno', len(paths))}",
                layer=layer,
                stroke=_hex(d.get("color")) if "s" in (d.get("type") or "s") else None,
                fill=_hex(d.get("fill")) if "f" in (d.get("type") or "f") else None,
                width=float(d.get("width") or 0.0),
                dashed=bool(dashes) and not dashes.startswith("[] "),
                even_odd=bool(d.get("even_odd")),
                close=bool(d.get("closePath")),
                items=d["items"],
                rect=tuple(d["rect"]),  # type: ignore[arg-type]
            )
        )
    texts = []
    for span in page.get_texttrace():
        text = fix_letters("".join(chr(c[0]) for c in span["chars"])).strip()
        if text:
            texts.append(
                TextSpan(text=text, layer=span.get("layer") or "", bbox=tuple(span["bbox"]))
            )  # type: ignore[arg-type]
    m = page.rotation_matrix
    return Sheet(
        rule=rule,
        width_pt=page.mediabox.width,
        height_pt=page.mediabox.height,
        rotation=page.rotation,
        paths=paths,
        texts=texts,
        rotation_matrix=(m.a, m.b, m.c, m.d, m.e, m.f),
    )


# --- path geometry (page frame) ----------------------------------------------------------------


def _bezier(p0: tuple, p1: tuple, p2: tuple, p3: tuple) -> list[tuple[float, float]]:
    pts = []
    for i in range(1, CURVE_STEPS + 1):
        t = i / CURVE_STEPS
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t**2, t**3
        pts.append(
            (
                a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
            )
        )
    return pts


def subpaths(items: list[tuple]) -> list[list[tuple[float, float]]]:
    """The item list as point sequences: a new sequence where the pen jumps."""
    out: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []

    def start(p: tuple) -> None:
        nonlocal cur
        if cur and math.dist(cur[-1], p) > 1e-3:
            out.append(cur)
            cur = []
        if not cur:
            cur.append((p[0], p[1]))

    for it in items:
        kind = it[0]
        if kind == "l":
            start(it[1])
            cur.append((it[2][0], it[2][1]))
        elif kind == "c":
            start(it[1])
            cur.extend(_bezier(it[1], it[2], it[3], it[4]))
        elif kind == "re":
            if cur:
                out.append(cur)
                cur = []
            x0, y0, x1, y1 = it[1]
            out.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])
        elif kind == "qu":
            if cur:
                out.append(cur)
                cur = []
            q = it[1]
            out.append([tuple(q[0]), tuple(q[1]), tuple(q[3]), tuple(q[2]), tuple(q[0])])  # type: ignore[list-item]
    if cur:
        out.append(cur)
    return [s for s in out if len(s) >= 2]


def path_lines(p: SheetPath) -> list[LineString]:
    lines = []
    for pts in subpaths(p.items):
        if p.close and math.dist(pts[0], pts[-1]) > 1e-3:
            pts = [*pts, pts[0]]
        if len(pts) >= 2 and any(
            math.dist(a, b) > 1e-6 for a, b in zip(pts, pts[1:], strict=False)
        ):
            lines.append(LineString(pts))
    return lines


def path_polygon(p: SheetPath) -> BaseGeometry | None:
    """The filled area of a path: its subpaths as rings, combined by the path's fill rule
    (even-odd: symmetric difference; non-zero: union, holes kept by orientation)."""
    rings = [pts for pts in subpaths(p.items) if len(pts) >= 3]
    if not rings:
        return None
    polys = []
    for pts in rings:
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = shapely.make_valid(poly)
        if not poly.is_empty and poly.area > 0:
            polys.append(poly)
    if not polys:
        return None
    if len(polys) == 1:
        return polys[0]
    if p.even_odd:
        acc = polys[0]
        for poly in polys[1:]:
            acc = acc.symmetric_difference(poly)
        return acc
    # non-zero winding: rings of opposite orientation to the largest one are holes
    outer = max(polys, key=lambda g: g.area)
    acc = outer
    for poly in polys:
        if poly is outer:
            continue
        acc = acc.difference(poly) if outer.contains(poly) else acc.union(poly)
    return acc


# --- the helper for writing rules -------------------------------------------------------------


def style_clusters(sheet: Sheet, limit_samples: int = 4) -> list[dict[str, Any]]:
    """Paths grouped by (layer, stroke, fill, width, dash) with counts, size and text samples:
    what a rules file selects from."""
    groups: dict[tuple, dict[str, Any]] = defaultdict(
        lambda: {"paths": 0, "segments": 0, "sizes": [], "ids": []}
    )
    for p in sheet.paths:
        g = groups[p.style_key()]
        g["paths"] += 1
        g["segments"] += len(p.items)
        g["sizes"].append(p.size_mm)
        if len(g["ids"]) < limit_samples:
            g["ids"].append(p.id)
    texts: dict[str, Counter[str]] = defaultdict(Counter)
    for t in sheet.texts:
        texts[t.layer][t.text] += 1
    rows = []
    for (layer, stroke, fill, width, dash), g in groups.items():
        sizes = sorted(g["sizes"])
        rows.append(
            {
                "layer": layer,
                "stroke": stroke,
                "fill": fill,
                "width": width,
                "dash": dash,
                "paths": g["paths"],
                "segments": g["segments"],
                "median_size_mm": round(sizes[len(sizes) // 2], 1),
                "max_size_mm": round(sizes[-1], 1),
                "sample_ids": g["ids"],
                "texts": [t for t, _ in texts.get(layer, Counter()).most_common(limit_samples)],
            }
        )
    rows.sort(key=lambda r: (r["layer"], -r["paths"]))
    text_only = [
        {
            "layer": layer,
            "stroke": "-",
            "fill": "-",
            "width": 0,
            "dash": "text",
            "paths": 0,
            "segments": 0,
            "median_size_mm": 0,
            "max_size_mm": 0,
            "sample_ids": [],
            "texts": [t for t, _ in c.most_common(limit_samples)],
        }
        for layer, c in texts.items()
        if not any(r["layer"] == layer for r in rows)
    ]
    return rows + sorted(text_only, key=lambda r: r["layer"])
