"""Sample extraction for the geometry assessment: can a layer's linework become polygons?

One technique covers every boundary form the plotter produces (continuous lines, closed rings,
dash / dot fragments, wide polylines plotted as filled ribbons): buffer the linework by a small
tolerance, union it, and read the faces as the holes of the result. Gaps narrower than twice the
tolerance close, so dotted and dashed boundaries still enclose their parcels. Filled areas
(tessellated solid hatches, fills) are unioned and counted as parts. Hatch-line areas are
buffered by the hatch spacing tolerance and unioned.

The counts are evidence for the classification, not the production extraction (ticket 07).
Requires shapely (``.[gis]`` extra).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import shapely
from shapely.geometry import LineString, Polygon

from core.gis.inspect_pdf import DASH_MAX_MM, M_PER_PT, MM_PER_PT

PT_PER_MM = 1 / MM_PER_PT
BOUNDARY_TOLERANCE_MM = 0.6  # closes dot / dash gaps up to 1.2 mm on paper
HATCH_TOLERANCE_MM = 1.5
MIN_FACE_M2 = 10.0  # smaller holes are glyph counters, vertex circles, slivers
MIN_FACE_MM2 = 20.0  # used when the plot scale is unknown
MAX_SAMPLE_SEGMENTS = 400_000  # larger layer sets are reported, not sampled
CROSS_MERGE_MM = 2.0  # arms of one grid cross are joined across gaps up to 4 mm


@dataclass
class SampleResult:
    method: str  # holes | fills | hatch
    faces: int
    area_m2: float | None  # total face area on the ground (scale known) else None
    median_face_m2: float | None
    segments: int
    skipped: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "faces": self.faces,
            "area_m2": None if self.area_m2 is None else round(self.area_m2),
            "median_face_m2": None
            if self.median_face_m2 is None
            else round(self.median_face_m2, 1),
            "segments": self.segments,
            "skipped": self.skipped,
        }


def _flatten(items: list[tuple]) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []

    def push(p: tuple[float, float]) -> None:
        if not pts or pts[-1] != p:
            pts.append(p)

    for it in items:
        kind = it[0]
        if kind == "l":
            push(it[1])
            push(it[2])
        elif kind == "c":
            p0, p1, p2, p3 = it[1], it[2], it[3], it[4]
            push(p0)
            for i in range(1, 5):  # four chords per curve: enough for counting faces
                t = i / 4
                a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t**2, t**3
                push(
                    (
                        a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                        a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
                    )
                )
        elif kind == "re":
            x0, y0, x1, y1 = it[1]
            for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)):
                push(p)
        elif kind == "qu":
            q = it[1]
            for p in (q[0], q[1], q[3], q[2], q[0]):
                push(p)
    return pts


def _geometries(paths: Iterable[dict[str, Any]]) -> tuple[list[Any], list[Any], int]:
    lines: list[Any] = []
    fills: list[Any] = []
    segments = 0
    for path in paths:
        segments += len(path["items"])
        pts = _flatten(path["items"])
        if len(pts) < 2:
            continue
        filled = path.get("fill") is not None
        if filled and len(pts) >= 3:
            poly = Polygon(pts)
            if not poly.is_valid:
                poly = shapely.make_valid(poly)
            if not poly.is_empty and poly.area > 0:
                fills.append(poly)
                continue
        lines.append(LineString(pts))
    return lines, fills, segments


def _area_unit(scale: float | None) -> tuple[float, float]:
    """(ground m² per pt², minimum face area in pt²)."""
    if scale:
        m2_per_pt2 = (M_PER_PT * scale) ** 2
        return m2_per_pt2, MIN_FACE_M2 / m2_per_pt2
    return 0.0, MIN_FACE_MM2 * PT_PER_MM**2


def _summary(method: str, areas: list[float], segments: int, scale: float | None) -> SampleResult:
    m2_per_pt2, _ = _area_unit(scale)
    areas.sort()
    median = areas[len(areas) // 2] if areas else None
    return SampleResult(
        method=method,
        faces=len(areas),
        area_m2=sum(areas) * m2_per_pt2 if scale else None,
        median_face_m2=(median * m2_per_pt2) if (scale and median is not None) else None,
        segments=segments,
    )


def fragment_spacing_mm(paths: list[dict[str, Any]]) -> float | None:
    """Median distance between neighbouring dash / dot pieces of a plotted linetype."""
    centres = []
    for path in paths:
        x0, y0, x1, y1 = path["rect"]
        if max(x1 - x0, y1 - y0) * MM_PER_PT <= DASH_MAX_MM:
            centres.append(((x0 + x1) / 2, (y0 + y1) / 2))
    if len(centres) < 10:
        return None
    pts = shapely.points(centres)
    tree = shapely.STRtree(pts)
    gaps = []
    for p in pts[:: max(1, len(centres) // 2000)]:  # a sample is enough for a median
        idx = tree.query_nearest(p, exclusive=True, all_matches=False)
        if len(idx):
            gaps.append(p.distance(pts[int(idx[0])]))
    gaps.sort()
    return round(gaps[len(gaps) // 2] * MM_PER_PT, 2) if gaps else None


def sample_faces(
    paths: list[dict[str, Any]],
    scale: float | None,
    hatch: bool = False,
    fills_as_areas: bool = False,
    tolerance_mm: float | None = None,
) -> SampleResult:
    """Faces the given paths enclose (see the module docstring for the three methods)."""
    segments = sum(len(p["items"]) for p in paths)
    method = "hatch" if hatch else ("fills" if fills_as_areas else "holes")
    if segments > MAX_SAMPLE_SEGMENTS:
        return SampleResult(method, 0, None, None, segments, skipped=f"{segments} segments")
    lines, fills, _ = _geometries(paths)
    if not lines and not fills:
        return SampleResult(method, 0, None, None, segments, skipped="no geometry")
    _, min_pt2 = _area_unit(scale)
    if fills_as_areas:
        merged = shapely.union_all(shapely.buffer(fills, 0.05)) if fills else None
        parts = list(getattr(merged, "geoms", [merged])) if merged is not None else []
        return _summary(method, [p.area for p in parts if p.area >= min_pt2], segments, scale)
    tolerance = (
        tolerance_mm or (HATCH_TOLERANCE_MM if hatch else BOUNDARY_TOLERANCE_MM)
    ) * PT_PER_MM
    pieces = shapely.buffer(lines, tolerance, quad_segs=2) if lines else []
    geoms = list(pieces) + [f.buffer(tolerance, quad_segs=2) for f in fills]
    merged = shapely.union_all(geoms)
    polys = list(getattr(merged, "geoms", [merged]))
    if hatch:
        return _summary(method, [p.area for p in polys if p.area >= min_pt2], segments, scale)
    holes: list[float] = []
    for poly in polys:
        for ring in getattr(poly, "interiors", []):
            area = Polygon(ring).area
            if area >= min_pt2:
                holes.append(area)
    return _summary(method, holes, segments, scale)


def lattice_spacing(paths: list[dict[str, Any]], scale: float | None) -> dict[str, Any] | None:
    """Regular grid crosses: count and spacing (ground metres when the scale is known).

    A cross is drawn as two strokes or four arms: the small paths are grown by CROSS_MERGE_MM and
    unioned, and every connected piece counts as one cross at its centroid.
    """
    boxes = []
    for path in paths:
        x0, y0, x1, y1 = path["rect"]
        if (x1 - x0) * MM_PER_PT > 12 or (y1 - y0) * MM_PER_PT > 12:
            continue
        boxes.append(shapely.box(x0, y0, x1, y1).buffer(CROSS_MERGE_MM * PT_PER_MM, quad_segs=1))
    if len(boxes) < 4:
        return None
    merged_geom = shapely.union_all(boxes)
    pieces = list(getattr(merged_geom, "geoms", [merged_geom]))
    merged = [(p.centroid.x, p.centroid.y) for p in pieces]
    if len(merged) < 4:
        return None
    pts = shapely.points(merged)
    tree = shapely.STRtree(pts)
    nearest: list[float] = []
    for p in pts:
        idx = tree.query_nearest(p, exclusive=True, all_matches=False)
        if len(idx):
            nearest.append(p.distance(pts[int(idx[0])]))
    nearest.sort()
    spacing_mm = round(nearest[len(nearest) // 2] * MM_PER_PT, 1)
    out: dict[str, Any] = {"crosses": len(merged), "spacing_mm": spacing_mm}
    if scale:
        out["spacing_m"] = round(spacing_mm * scale / 1000, 1)
        out["lattice_m"] = round_lattice(spacing_mm, scale)
    return out


LATTICE_STEPS_M = (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000)


def round_lattice(spacing_mm: float, scale: float) -> int | None:
    """The round grid interval (m) a cross spacing implies at this scale, or None (±3 %)."""
    metres = spacing_mm * scale / 1000
    step = min(LATTICE_STEPS_M, key=lambda r: abs(metres - r))
    return step if abs(metres - step) / step <= 0.03 else None


def polyline_length_km(paths: list[dict[str, Any]], scale: float | None) -> float | None:
    if not scale:
        return None
    total = 0.0
    for path in paths:
        pts = _flatten(path["items"])
        total += sum(math.dist(a, b) for a, b in zip(pts, pts[1:], strict=False))
    return round(total * M_PER_PT * scale / 1000, 2)
