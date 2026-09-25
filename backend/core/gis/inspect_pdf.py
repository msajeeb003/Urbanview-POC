"""Inspect planning PDFs for the geometry assessment (build plan P0, gate 1).

Per page: size and paper, the text layer (with AutoCAD's glyph-id quirk decoded), embedded
images and their effective resolution, the vector content (paths, segments, fills, closed rings)
and, per optional-content group (the CAD layers a plotter keeps as PDF layers), the form the
geometry takes: closed rings, open lines, dash fragments, hatch lines, filled ribbons (wide
polylines plotted as outlines), tessellated fills, glyph outlines (text drawn as vectors) or real
text. Plus the evidence georeferencing needs: the stated scale, the viewport scale the plotter
recorded (``/VP`` ``/Measure``), coordinate labels and tables, coordinate systems named in the
text, legend and title.

This module measures; ``core.gis.assessment`` decides. Requires pymupdf (``.[gis]`` extra).
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

MM_PER_PT = 25.4 / 72
M_PER_PT = MM_PER_PT / 1000  # ground metres per PDF point at a scale of 1:1

ISO_PAPER_MM = {
    "A0": (841, 1189),
    "A1": (594, 841),
    "A2": (420, 594),
    "A3": (297, 420),
    "A4": (210, 297),
}

# Geometry forms of a layer (``LayerStats.form``).
DASH_MAX_MM = 3.0  # an open stroke path shorter than this is a dash fragment
GLYPH_MAX_MM = 12.0  # a small filled outline with many segments is a vector-drawn character
GLYPH_MIN_SEGMENTS = 10
RIBBON_MAX_WIDTH_MM = 2.0  # a thin filled outline is a wide polyline plotted as its outline
TESSELLATION_MAX_SEGMENTS = 8  # solid hatches plot as triangles / strips of few segments
HATCH_MIN_ANGLE_SHARE = 0.6
FORM_MIN_SHARE = 0.5

# AutoCAD's PDF plotter embeds some TrueType fonts without a ToUnicode map: the extracted codes
# are glyph ids, 29 below the character for the Latin block ("&RS\ULJKW" is "Copyright"), and
# the South Slavic letters come out as other Latin-1 letters. Seen in every sample sheet.
GLYPH_ID_SHIFT = 29
GLYPH_ID_LETTERS = {
    "ÿ": "Č",
    "þ": "č",
    "ý": "Ć",
    "ü": "ć",
    "ä": "Š",
    "å": "š",
    "æ": "ž",
    "ċ": "Đ",
}

_NUMBER_RUN = re.compile(r"(?<![\d.,])(\d{1,3}(?:[ .]\d{3}){1,2}|\d{6,7})(?:[.,](\d+))?(?![\d])")
_COORD_PAIR = re.compile(r"[XYxy]\s*=\s*(\d{6,7}(?:[.,]\d+)?)\s+[XYxy]\s*=\s*(\d{6,7}(?:[.,]\d+)?)")
_SCALE_TEXT = re.compile(
    r"(?:\bR\b|\bM\b|razmjer\w*|razmer\w*|scale)?\s*[=:]?\s*1\s*:\s*(\d[\d .]{1,8}\d)"
)


# --- text helpers ---------------------------------------------------------------------------------


def fold(text: str) -> str:
    """Lower case without diacritics, for keyword matching ("POVRŠINA" -> "povrsina")."""
    text = text.replace("đ", "dj").replace("Đ", "Dj")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def decode_glyph_ids(text: str) -> str:
    """Undo the AutoCAD glyph-id shift on a whole string (see ``GLYPH_ID_SHIFT``)."""
    out = []
    for c in text:
        if c in GLYPH_ID_LETTERS:
            out.append(GLYPH_ID_LETTERS[c])
        elif 0x03 <= ord(c) <= 0x5D:
            out.append(chr(ord(c) + GLYPH_ID_SHIFT))
        else:
            out.append(c)
    return "".join(out)


def fix_letters(text: str) -> str:
    """Map only the South Slavic letters in otherwise readable text ("GRAFIÿKOG")."""
    return "".join(GLYPH_ID_LETTERS.get(c, c) for c in text)


_READABLE = re.compile(r"[A-Za-z0-9ČčĆćŠšŽžĐđ]{2,}")
_VOWELS = set("aeiouAEIOU")


def _vowel_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum(c in _VOWELS for c in letters) / len(letters) if letters else 0.0


def looks_glyph_shifted(word: str) -> bool:
    """A word that only reads after the glyph-id shift ("85%$1,67,ÿ.(", "SRYUåLQH").

    Shifted text never holds lower-case ASCII letters (the shift maps them to D-]), turns
    digits into control characters and capitals into punctuation; a word made only of the
    capitals D-Z is shifted when it reads like a word only once decoded (vowels appear).
    """
    if len(word) < 3 or re.fullmatch(r"[\d.,:;/+\-()%=]+", word) or "+" in word:
        return False
    if any(not (0x03 <= ord(c) <= 0x5D or c in GLYPH_ID_LETTERS) for c in word):
        return False
    decoded = decode_glyph_ids(word)
    if not _READABLE.fullmatch(decoded):
        return False
    if any(ord(c) < 0x20 for c in word):
        return True
    if any(0x24 <= ord(c) <= 0x3D for c in word) or any(c in GLYPH_ID_LETTERS for c in word):
        return _vowel_share(decoded) >= 0.25
    return _vowel_share(decoded) >= 0.25 and _vowel_share(word) < 0.2


def readable(text: str) -> str:
    """Best-effort readable form of an extracted string, for samples and keyword search."""
    words = text.split(" ")
    return " ".join(
        decode_glyph_ids(w) if looks_glyph_shifted(w) else fix_letters(w) for w in words
    )


def layer_key(name: str) -> str:
    """The layer name without an AutoCAD xref prefix ("X-GEO_SV$0$PARCELE_GRANICE")."""
    return name.rsplit("$0$", 1)[-1] if "$0$" in name else name


def parse_number(digits: str, decimals: str | None = None) -> float:
    value = float(digits.replace(" ", "").replace(".", ""))
    if decimals:
        value += float("0." + decimals)
    return value


# --- data -----------------------------------------------------------------------------------------


@dataclass
class LayerStats:
    """What one PDF layer (optional-content group) holds on one page."""

    name: str  # as in the PDF; "" = content outside any layer
    key: str = ""  # without the xref prefix
    paths: int = 0
    segments: int = 0
    filled: int = 0
    stroked: int = 0
    closed: int = 0  # closed rings: closePath, re / qu items, or first point == last point
    single_lines: int = 0  # stroke-only paths of one straight segment
    dashes: int = 0  # open stroke paths shorter than DASH_MAX_MM on paper
    hatch_angle_share: float = 0.0  # share of single lines on the most common angle
    ribbons: int = 0  # thin filled outlines: wide polylines plotted as outlines
    tessellation: int = 0  # filled triangles / strips of a plotted solid hatch
    glyphs: int = 0  # small filled outlines with many segments: characters drawn as vectors
    text_spans: int = 0
    text_distinct: int = 0
    text_sample: list[str] = field(default_factory=list)
    bbox: tuple[float, float, float, float] | None = None
    # drawn length on paper (mm) per kind of piece: what the layer looks like, whatever the
    # count of tiny marks (vertex circles, numbers) next to its long boundaries
    length_mm: dict[str, float] = field(default_factory=dict)
    form: str = "empty"

    def as_row(self) -> dict[str, Any]:
        return {
            "layer": self.name,
            "key": self.key,
            "form": self.form,
            "paths": self.paths,
            "segments": self.segments,
            "filled": self.filled,
            "stroked": self.stroked,
            "closed": self.closed,
            "single_lines": self.single_lines,
            "dashes": self.dashes,
            "hatch_angle_share": round(self.hatch_angle_share, 2),
            "ribbons": self.ribbons,
            "tessellation": self.tessellation,
            "glyphs": self.glyphs,
            "text_spans": self.text_spans,
            "text_distinct": self.text_distinct,
            "text_sample": " | ".join(self.text_sample),
            "length_mm": ", ".join(f"{k} {v:.0f}" for k, v in sorted(self.length_mm.items())),
        }


@dataclass
class Viewport:
    bbox: tuple[float, float, float, float]
    units_per_pt: float
    scale_denominator: float | None  # model viewports: ground metres per paper metre
    paper_space: bool
    geospatial: bool  # /Measure /GEO with /GPTS: a GeoPDF


@dataclass
class GridEvidence:
    """Coordinate labels found in the text layer, matched to the profile's CRS candidates."""

    epsg: int
    crs_name: str
    eastings: list[float]
    northings: list[float]
    interval_m: float | None
    fit: dict[str, Any] | None = None  # label-position fit: scale and residuals


@dataclass
class PageReport:
    page: int  # 1-based
    width_mm: float
    height_mm: float
    paper: str
    rotation: int
    text_chars: int = 0
    text_words: int = 0
    shifted_words: int = 0  # words only readable after the glyph-id shift
    images: int = 0
    image_cover_pct: float = 0.0  # largest single image, share of the page
    image_dpi: float | None = None  # effective resolution of that image
    paths: int = 0
    segments: int = 0
    segments_by_kind: dict[str, int] = field(default_factory=dict)
    filled_paths: int = 0
    closed_paths: int = 0
    rect_share: float = 0.0  # share of segments that are rectangles (table cell borders)
    layered_path_share: float = 0.0  # share of paths inside a named layer
    layers: list[LayerStats] = field(default_factory=list)
    style_groups: int = 0  # distinct stroke / fill / width / dash combinations
    top_styles: list[tuple[str, int]] = field(default_factory=list)
    viewports: list[Viewport] = field(default_factory=list)
    stated_scales: list[int] = field(default_factory=list)
    title: str | None = None
    title_types: list[str] = field(default_factory=list)
    legend: bool = False
    crs_mentions: list[str] = field(default_factory=list)
    coordinate_mentions: int = 0  # "koordinat…" in the text (tables, legends)
    grid: list[GridEvidence] = field(default_factory=list)
    coordinate_pairs: int = 0  # "X = … Y = …" rows readable as text
    role: str = "unknown"  # plan_sheet | table | text | blank

    @property
    def main_scale(self) -> float | None:
        model = [v for v in self.viewports if not v.paper_space and v.scale_denominator]
        if not model:
            return None
        biggest = max(model, key=lambda v: (v.bbox[2] - v.bbox[0]) * (v.bbox[3] - v.bbox[1]))
        return biggest.scale_denominator


@dataclass
class PdfReport:
    path: str
    sha256: str
    size_bytes: int
    pages: int
    creator: str
    producer: str
    created: str
    layers_declared: list[str]
    page_reports: list[PageReport]
    geospatial: bool = False


# --- the profile knowledge the inspector needs ---------------------------------------------------


@dataclass(frozen=True)
class InspectConfig:
    crs_candidates: tuple[dict[str, Any], ...] = ()
    crs_keywords: tuple[str, ...] = ()
    legend_keywords: tuple[str, ...] = ("legenda", "legend")
    coordinate_keywords: tuple[str, ...] = ("koordinat",)
    sheet_titles: dict[str, tuple[str, ...]] = field(default_factory=dict)
    text_sample_size: int = 6

    @classmethod
    def from_profile(cls, gis: Any) -> InspectConfig:
        if gis is None:
            return cls()
        return cls(
            crs_candidates=tuple(c.model_dump() for c in gis.crs_candidates),
            crs_keywords=tuple(gis.crs_keywords),
            legend_keywords=tuple(gis.legend_keywords),
            coordinate_keywords=tuple(gis.coordinate_keywords),
            sheet_titles={k: tuple(v) for k, v in gis.sheet_titles.items()},
        )


# --- geometry measurements ------------------------------------------------------------------------


def _points(items: list[tuple]) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for it in items:
        kind = it[0]
        if kind == "l":
            pts.extend((it[1], it[2]))
        elif kind == "c":
            pts.extend((it[1], it[4]))
        elif kind == "re":
            x0, y0, x1, y1 = it[1]
            pts.extend(((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)))
        elif kind == "qu":
            q = it[1]
            pts.extend((q[0], q[1], q[3], q[2], q[0]))
    return pts


def _ring_measure(pts: list[tuple[float, float]]) -> tuple[float, float]:
    """(area, perimeter) of the polygon through the points, closing it."""
    if len(pts) < 3:
        return 0.0, 0.0
    area = 0.0
    perimeter = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        area += x0 * y1 - x1 * y0
        perimeter += math.hypot(x1 - x0, y1 - y0)
    return abs(area) / 2, perimeter


def _is_closed(path: dict[str, Any]) -> bool:
    items = path["items"]
    if path.get("closePath"):
        return True
    if len(items) == 1 and items[0][0] in ("re", "qu"):
        return True
    first, last = items[0], items[-1]
    if first[0] in ("l", "c") and last[0] in ("l", "c"):
        start, end = first[1], last[-1]
        return abs(start[0] - end[0]) < 0.01 and abs(start[1] - end[1]) < 0.01 and len(items) > 2
    return False


def _path_length(items: list[tuple]) -> float:
    """Drawn length in points (curves by their chord)."""
    total = 0.0
    for it in items:
        kind = it[0]
        if kind == "l":
            total += math.dist(it[1], it[2])
        elif kind == "c":
            total += math.dist(it[1], it[4])
        elif kind == "re":
            x0, y0, x1, y1 = it[1]
            total += 2 * (abs(x1 - x0) + abs(y1 - y0))
        elif kind == "qu":
            q = it[1]
            total += math.dist(q[0], q[1]) + math.dist(q[1], q[3])
            total += math.dist(q[3], q[2]) + math.dist(q[2], q[0])
    return total


class _LayerAccumulator:
    def __init__(self, name: str) -> None:
        self.stats = LayerStats(name=name, key=layer_key(name))
        self.angles: Counter[int] = Counter()
        self.bbox: list[float] | None = None
        self.length: Counter[str] = Counter()

    def add(self, path: dict[str, Any]) -> None:
        s = self.stats
        items = path["items"]
        s.paths += 1
        s.segments += len(items)
        filled = path.get("fill") is not None and "f" in (path.get("type") or "f")
        stroked = path.get("color") is not None and "s" in (path.get("type") or "s")
        if filled:
            s.filled += 1
        if stroked:
            s.stroked += 1
        closed = _is_closed(path)
        if closed:
            s.closed += 1
        x0, y0, x1, y1 = path["rect"]
        if self.bbox is None:
            self.bbox = [x0, y0, x1, y1]
        else:
            b = self.bbox
            b[0], b[1], b[2], b[3] = min(b[0], x0), min(b[1], y0), max(b[2], x1), max(b[3], y1)
        w_mm, h_mm = (x1 - x0) * MM_PER_PT, (y1 - y0) * MM_PER_PT
        longest = max(w_mm, h_mm)
        length = _path_length(items) * MM_PER_PT
        if filled and longest <= GLYPH_MAX_MM and len(items) >= GLYPH_MIN_SEGMENTS:
            if longest <= 5 * max(min(w_mm, h_mm), 0.01):
                s.glyphs += 1
                self.length["glyphs"] += length
                return
        if longest <= DASH_MAX_MM and (stroked or filled):
            # a linetype plotted piece by piece: dashes, or dots (small filled / closed circles)
            s.dashes += 1
            self.length["dashes"] += length
            return
        if filled:
            if len(items) <= TESSELLATION_MAX_SEGMENTS:
                s.tessellation += 1
                self.length["tessellation"] += length
                return
            area, perimeter = _ring_measure(_points(items))
            if perimeter > 0 and 2 * area / perimeter * MM_PER_PT <= RIBBON_MAX_WIDTH_MM:
                s.ribbons += 1
                self.length["ribbons"] += length
            else:
                self.length["fills"] += length
            return
        if not stroked:
            return
        if len(items) == 1 and items[0][0] == "l":
            s.single_lines += 1
            self.length["single_lines"] += length
            (ax, ay), (bx, by) = items[0][1], items[0][2]
            angle = int(round(math.degrees(math.atan2(by - ay, bx - ax)) % 180 / 5)) % 36
            self.angles[angle] += 1
        else:
            self.length["rings" if closed else "lines"] += length

    def finish(self) -> LayerStats:
        s = self.stats
        if self.angles and s.single_lines:
            s.hatch_angle_share = self.angles.most_common(1)[0][1] / s.single_lines
        if self.bbox:
            s.bbox = tuple(round(v, 1) for v in self.bbox)  # type: ignore[assignment]
        s.length_mm = {k: round(v, 1) for k, v in self.length.items() if v}
        s.form = layer_form(s)
        return s


def layer_form(s: LayerStats) -> str:
    """Dominant geometry form of a layer, by drawn length (see the module docstring)."""
    if s.paths == 0:
        return "text" if s.text_spans else "empty"
    length = s.length_mm
    total = sum(length.values())
    if total <= 0:
        return "lines"
    hatch = s.hatch_angle_share >= HATCH_MIN_ANGLE_SHARE
    shares = {
        "glyphs": length.get("glyphs", 0.0) / total,
        "ribbons": length.get("ribbons", 0.0) / total,
        "tessellation": length.get("tessellation", 0.0) / total,
        "dashes": length.get("dashes", 0.0) / total,
        "hatch": length.get("single_lines", 0.0) / total if hatch else 0.0,
        "rings": length.get("rings", 0.0) / total,
    }
    best = max(shares, key=lambda k: shares[k])
    if shares[best] >= FORM_MIN_SHARE:
        return best
    return "lines"


# --- viewports (AutoCAD /VP /Measure) ----------------------------------------------------------


_VP_BBOX = re.compile(r"/BBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]")
_VP_X = re.compile(r"/X\s*\[\s*<<\s*/C\s*([-\d.]+)")


def parse_viewports(doc: pymupdf.Document, page: pymupdf.Page) -> list[Viewport]:
    """Viewports the plotter recorded; ``units_per_pt`` is drawing units per PDF point.

    A viewport covering the whole page with 0.3528 units per point is paper space in
    millimetres; a model viewport's factor in metres per point gives the plot scale (the scale
    the PDF actually has, which may differ from the title block when a sheet was fitted to paper).
    """
    kind, value = doc.xref_get_key(page.xref, "VP")
    if kind != "array":
        return []
    page_area = page.mediabox.width * page.mediabox.height
    out: list[Viewport] = []
    for chunk in value.split("/Type/Viewport")[1:]:
        bb = _VP_BBOX.search(chunk)
        cx = _VP_X.search(chunk)
        if not bb or not cx:
            continue
        box = tuple(float(v) for v in bb.groups())
        factor = float(cx.group(1))
        area = abs((box[2] - box[0]) * (box[3] - box[1]))
        paper = abs(factor - MM_PER_PT) < 0.002 and area >= 0.9 * page_area
        denominator = None if paper or factor <= 0 else round(factor / M_PER_PT, 1)
        out.append(
            Viewport(
                bbox=box,  # type: ignore[arg-type]
                units_per_pt=factor,
                scale_denominator=denominator,
                paper_space=paper,
                geospatial="/GPTS" in chunk or "/Subtype/GEO" in chunk,
            )
        )
    return out


# --- coordinate labels ------------------------------------------------------------------------


def _grid_interval(values: list[float]) -> float | None:
    distinct = sorted(set(round(v, 2) for v in values))
    if len(distinct) < 2:
        return None
    diffs = Counter(round(b - a, 2) for a, b in zip(distinct, distinct[1:], strict=False))
    step, _ = diffs.most_common(1)[0]
    return step if step > 0 else None


def find_grid(
    words: list[tuple[float, float, float, float, str]], candidates: tuple[dict[str, Any], ...]
) -> list[GridEvidence]:
    """Round coordinate values in the text, with the positions of their labels.

    A label is a number inside a candidate CRS's easting or northing range. A fit of label
    position against value (x ~ easting for labels along the top / bottom frame, y ~ northing
    along the sides) yields the metres per point the grid implies, a check on the viewport scale.
    """
    found: list[GridEvidence] = []
    for cand in candidates:
        e_lo, e_hi = cand["easting"]
        n_lo, n_hi = cand["northing"]
        east: list[tuple[float, float]] = []  # (value, x centre)
        north: list[tuple[float, float]] = []  # (value, y centre)
        for x0, y0, x1, y1, text in words:
            for m in _NUMBER_RUN.finditer(text):
                value = parse_number(m.group(1), m.group(2))
                if e_lo <= value <= e_hi:
                    east.append((value, (x0 + x1) / 2))
                elif n_lo <= value <= n_hi:
                    north.append((value, (y0 + y1) / 2))
        e_vals = [v for v, _ in east]
        n_vals = [v for v, _ in north]
        # a grid needs several distinct round values on both axes
        round_e = {v for v in e_vals if v % 50 == 0}
        round_n = {v for v in n_vals if v % 50 == 0}
        if len(round_e) >= 2 and len(round_n) >= 2:
            found.append(
                GridEvidence(
                    epsg=cand["epsg"],
                    crs_name=cand["name"],
                    eastings=sorted(round_e),
                    northings=sorted(round_n),
                    interval_m=_grid_interval(sorted(round_e | round_n)),
                    fit=_label_fit(
                        [p for p in east if p[0] in round_e], [p for p in north if p[0] in round_n]
                    ),
                )
            )
    return found


def _linear_fit(pairs: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """Least squares pos = a * value + b; returns (a, b, rms residual in value units)."""
    if len({v for v, _ in pairs}) < 2:
        return None
    n = len(pairs)
    mv = sum(v for v, _ in pairs) / n
    mp = sum(p for _, p in pairs) / n
    sxx = sum((v - mv) ** 2 for v, _ in pairs)
    sxy = sum((v - mv) * (p - mp) for v, p in pairs)
    a = sxy / sxx
    b = mp - a * mv
    if a == 0:
        return None
    rms_pt = math.sqrt(sum((p - (a * v + b)) ** 2 for v, p in pairs) / n)
    return a, b, rms_pt / abs(a)


def _label_fit(
    east: list[tuple[float, float]], north: list[tuple[float, float]]
) -> dict[str, Any] | None:
    fe = _linear_fit(east)
    fn = _linear_fit(north)
    if not fe and not fn:
        return None
    out: dict[str, Any] = {"labels": len(east) + len(north)}
    if fe:
        out["east_m_per_pt"] = round(1 / abs(fe[0]), 4)
        out["east_scale"] = round(1 / abs(fe[0]) / M_PER_PT)
        out["east_rms_m"] = round(fe[2], 1)
    if fn:
        out["north_m_per_pt"] = round(1 / abs(fn[0]), 4)
        out["north_scale"] = round(1 / abs(fn[0]) / M_PER_PT)
        out["north_rms_m"] = round(fn[2], 1)
        # PDF y grows downwards: a northing axis pointing up has a negative slope
        out["north_up"] = fn[0] < 0
    return out


# --- page and document ------------------------------------------------------------------------


def paper_name(width_mm: float, height_mm: float) -> str:
    short, long_ = sorted((width_mm, height_mm))
    for name, (s, lng) in ISO_PAPER_MM.items():
        if abs(short - s) / s < 0.03 and abs(long_ - lng) / lng < 0.03:
            return name
    return f"custom {round(width_mm)}x{round(height_mm)}"


def _style_key(path: dict[str, Any]) -> str:
    def hexc(c: Any) -> str:
        if c is None:
            return "-"
        return "#" + "".join(f"{round(v * 255):02x}" for v in c[:3])

    width = round(path.get("width") or 0, 1)
    return f"{hexc(path.get('color'))}/{hexc(path.get('fill'))}/{width}/{path.get('dashes') or ''}"


def _office_producer(creator: str, producer: str) -> bool:
    text = f"{creator} {producer}".lower()
    return any(k in text for k in ("word", "excel", "libreoffice", "openoffice", "writer"))


def inspect_page(
    doc: pymupdf.Document,
    page: pymupdf.Page,
    config: InspectConfig,
    office: bool = False,
    drawings: list[dict[str, Any]] | None = None,
) -> PageReport:
    """Measure one page; pass ``drawings`` (``page.get_cdrawings()``) to reuse an extraction."""
    if drawings is None:
        drawings = page.get_cdrawings()
    rect = page.rect
    width_mm, height_mm = rect.width * MM_PER_PT, rect.height * MM_PER_PT
    report = PageReport(
        page=page.number + 1,
        width_mm=round(width_mm, 1),
        height_mm=round(height_mm, 1),
        paper=paper_name(width_mm, height_mm),
        rotation=page.rotation,
    )

    # text layer (and which layer every span sits on)
    words = page.get_text("words")
    report.text_words = len(words)
    report.shifted_words = sum(1 for w in words if looks_glyph_shifted(w[4]))
    raw_text = page.get_text("text")
    report.text_chars = len(raw_text.strip())
    spans_by_layer: dict[str, list[str]] = {}
    for span in page.get_texttrace():
        text = "".join(chr(c[0]) for c in span["chars"]).strip()
        if text:
            spans_by_layer.setdefault(span.get("layer") or "", []).append(text)
    readable_text = readable(raw_text)
    folded = fold(readable_text + "\n" + decode_glyph_ids(raw_text))

    # images
    page_area = rect.width * rect.height
    infos = page.get_image_info()
    report.images = len(infos)
    if infos and page_area:
        biggest = max(infos, key=lambda i: pymupdf.Rect(i["bbox"]).get_area())
        box = pymupdf.Rect(biggest["bbox"]) & rect
        report.image_cover_pct = round(box.get_area() / page_area * 100, 1)
        width_in = max(pymupdf.Rect(biggest["bbox"]).width, 1e-6) / 72
        height_in = max(pymupdf.Rect(biggest["bbox"]).height, 1e-6) / 72
        report.image_dpi = round(max(biggest["width"] / width_in, biggest["height"] / height_in))

    # vector content per layer
    accumulators: dict[str, _LayerAccumulator] = {}
    kinds: Counter[str] = Counter()
    styles: Counter[str] = Counter()
    layered = 0
    for path in drawings:
        name = path.get("layer") or ""
        acc = accumulators.get(name)
        if acc is None:
            acc = accumulators[name] = _LayerAccumulator(name)
        acc.add(path)
        if name:
            layered += 1
        for it in path["items"]:
            kinds[it[0]] += 1
        styles[_style_key(path)] += 1
    for name, spans in spans_by_layer.items():
        acc = accumulators.get(name)
        if acc is None:
            acc = accumulators[name] = _LayerAccumulator(name)
        acc.stats.text_spans = len(spans)
        acc.stats.text_distinct = len(set(spans))
        seen: list[str] = []
        for s in spans:
            r = readable(s)
            if r not in seen:
                seen.append(r)
            if len(seen) >= config.text_sample_size:
                break
        acc.stats.text_sample = seen
    report.layers = sorted(
        (a.finish() for a in accumulators.values()), key=lambda s: (-s.paths, -s.text_spans)
    )
    report.paths = sum(s.paths for s in report.layers)
    report.segments = sum(kinds.values())
    report.segments_by_kind = dict(kinds)
    report.filled_paths = sum(s.filled for s in report.layers)
    report.closed_paths = sum(s.closed for s in report.layers)
    report.rect_share = round(kinds.get("re", 0) / report.segments, 2) if report.segments else 0.0
    report.layered_path_share = round(layered / report.paths, 3) if report.paths else 0.0
    report.style_groups = len(styles)
    report.top_styles = styles.most_common(8)

    # scale, title, legend, coordinate systems, coordinate labels
    report.viewports = parse_viewports(doc, page)
    scales: list[int] = []
    for m in _SCALE_TEXT.finditer(readable_text):
        value = int(re.sub(r"\D", "", m.group(1)))
        if 100 <= value <= 1_000_000 and value not in scales:
            scales.append(value)
    report.stated_scales = scales
    for sheet_type, patterns in config.sheet_titles.items():
        for pattern in patterns:
            m = re.search(pattern, folded)
            if m:
                report.title_types.append(sheet_type)
                report.title = report.title or m.group(0)
                break
    report.legend = any(re.search(k, folded) for k in config.legend_keywords)
    report.crs_mentions = sorted(
        {m.group(0) for k in config.crs_keywords for m in re.finditer(k, folded)}
    )
    report.coordinate_mentions = sum(len(re.findall(k, folded)) for k in config.coordinate_keywords)
    # word boxes in the displayed (rotated) orientation, so eastings run along x
    rotation = page.rotation_matrix
    display_words = []
    for w in words:
        box = pymupdf.Rect(w[:4]) * rotation
        text = decode_glyph_ids(w[4]) if looks_glyph_shifted(w[4]) else w[4]
        display_words.append((box.x0, box.y0, box.x1, box.y1, text))
    report.grid = find_grid(display_words, config.crs_candidates)
    report.coordinate_pairs = len(_COORD_PAIR.findall(readable_text))

    report.role = page_role(report, office)
    return report


def page_role(report: PageReport, office: bool) -> str:
    """plan_sheet | table | text | blank."""
    long_mm = max(report.width_mm, report.height_mm)
    if report.paths < 20 and report.images == 0 and report.text_words < 10:
        return "blank"
    if office or long_mm < 400:
        return "table" if report.rect_share >= 0.6 and report.text_words >= 30 else "text"
    if report.paths >= 50 or report.image_cover_pct >= 50:
        return "plan_sheet"
    return "text"


PageHook = Callable[[pymupdf.Page, PageReport, list[dict[str, Any]]], None]


def inspect_pdf(path: Path, config: InspectConfig, on_page: PageHook | None = None) -> PdfReport:
    """Inspect every page; ``on_page`` sees each page with its drawings while they are in memory
    (the assessment samples geometry and renders previews there, so a page is extracted once)."""
    data = path.read_bytes()
    doc = pymupdf.open(stream=data, filetype="pdf")
    meta = doc.metadata or {}
    creator, producer = meta.get("creator") or "", meta.get("producer") or ""
    office = _office_producer(creator, producer)
    try:
        declared = [v["name"] for v in (doc.get_ocgs() or {}).values()]
    except Exception:  # noqa: BLE001 - broken optional-content trees still inspect
        declared = []
    pages = []
    for page in doc:
        drawings = page.get_cdrawings()
        report = inspect_page(doc, page, config, office, drawings)
        if on_page is not None:
            on_page(page, report, drawings)
        del drawings
        pages.append(report)
    return PdfReport(
        path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        pages=doc.page_count,
        creator=creator,
        producer=producer,
        created=meta.get("creationDate") or "",
        layers_declared=declared,
        page_reports=pages,
        geospatial=any(v.geospatial for p in pages for v in p.viewports),
    )


# --- layer isolation previews ----------------------------------------------------------------


def redraw_layers(
    source: pymupdf.Page,
    groups: list[tuple[list[str], tuple[float, float, float]]],
    target_width_px: int = 1400,
    drawings: list[dict[str, Any]] | None = None,
) -> bytes:
    """PNG of only the given layers, each group drawn in one colour on a blank sheet.

    The paths are re-drawn from the extracted vector items (not a crop of the plot), so the
    picture shows exactly what the extraction step would get from those layers.
    """
    wanted: dict[str, tuple[float, float, float]] = {}
    for names, colour in groups:
        for n in names:
            wanted[n] = colour
    out = pymupdf.open()
    target = out.new_page(width=source.mediabox.width, height=source.mediabox.height)
    shape = target.new_shape()
    for path in drawings if drawings is not None else source.get_cdrawings():
        colour = wanted.get(path.get("layer") or "")
        if colour is None:
            continue
        for it in path["items"]:
            kind = it[0]
            if kind == "l":
                shape.draw_line(it[1], it[2])
            elif kind == "c":
                shape.draw_bezier(it[1], it[2], it[3], it[4])
            elif kind == "re":
                shape.draw_rect(pymupdf.Rect(it[1]))
            elif kind == "qu":
                shape.draw_quad(pymupdf.Quad(it[1]))
        filled = path.get("fill") is not None
        shape.finish(
            color=None if filled and path.get("color") is None else colour,
            fill=colour if filled else None,
            width=max(0.3, float(path.get("width") or 0.3)),
            closePath=bool(path.get("closePath")),
        )
    shape.commit()
    target.set_rotation(source.rotation)
    long_pt = max(target.rect.width, target.rect.height)
    zoom = target_width_px / long_pt
    pix = target.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")
