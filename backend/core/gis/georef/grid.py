"""Control points from the grid crosses printed on a sheet.

The plan sheets carry the state grid as small crosses at round coordinates (every 100 m on the
Podgorica DUP and UP sheets, layer MREZA). Their positions on the page are exact but carry no
labels, so one seed coordinate is enough: every cross's approximate coordinate (seed + the
distance on the page at the sheet's proven scale, turned by the lattice's measured angle) is
rounded to the grid interval, which gives it its exact coordinate. A seed must be within half an
interval (50 m) of the truth: a seed 100 m off shifts every cross by 100 m while the fit stays
perfect, so the seed's source goes into the report and the cadastral overlap check catches a gross
shift. The display orientation of the page (its /Rotate) is taken as north-up.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import shapely

from core.gis.extract.rules import Selector, SheetRule
from core.gis.extract.sheet import Sheet, subpaths
from core.gis.georef.fit import sheet_k
from core.gis.georef.points import ControlPoint
from core.gis.inspect_pdf import MM_PER_PT

CROSS_MAX_MM = 12.0  # arms of one cross are smaller than this
CROSS_MERGE_MM = 2.0  # arms closer than twice this are one cross
LATTICE_TOLERANCE = 0.02  # a cross off its lattice node by more than 2 % of the interval is dropped


@dataclass(frozen=True, slots=True)
class Cross:
    x_pt: float  # sheet frame: PDF points, origin bottom-left
    y_pt: float


@dataclass
class GridSuggestion:
    points: list[ControlPoint]
    crosses: int
    angle_deg: float  # the lattice's angle on the displayed page
    seed_offset_m: tuple[float, float]  # where the seed sits inside its grid cell (the check)
    warnings: list[str]


def find_crosses(sheet: Sheet, layer_regex: str) -> list[Cross]:
    """Cross centres on the sheet: small paths of the grid layer, merged, one centroid each."""
    boxes = []
    grow = CROSS_MERGE_MM / MM_PER_PT  # a straight arm's box has no height: grow, never buffer
    for p in sheet.select([Selector(layer_regex=layer_regex)]):
        # the box of the path's own points: pymupdf's path rect can miss a subpath's start
        pts = np.array([xy for run in subpaths(p.items) for xy in run] or [(0.0, 0.0)])
        (x0, y0), (x1, y1) = pts.min(axis=0), pts.max(axis=0)
        if not len(p.items) or max(x1 - x0, y1 - y0) * MM_PER_PT > CROSS_MAX_MM:
            continue
        boxes.append(shapely.box(x0 - grow, y0 - grow, x1 + grow, y1 + grow))
    if not boxes:
        return []
    merged = shapely.union_all(boxes)
    crosses = []
    for piece in getattr(merged, "geoms", [merged]):
        c = piece.centroid
        crosses.append(Cross(round(c.x, 3), round(sheet.height_pt - c.y, 3)))
    return sorted(crosses, key=lambda c: (c.y_pt, c.x_pt))


def _display(sheet: Sheet, x_pt: float, y_pt: float) -> tuple[float, float]:
    """Sheet frame -> the displayed page with y up (north-up if the plan is drawn north-up)."""
    a, b, c, d, e, f = sheet.rotation_matrix
    x, y = x_pt, sheet.height_pt - y_pt  # page frame, y down
    return (x * a + y * c + e, -(x * b + y * d + f))


def lattice_angle(points: np.ndarray) -> float:
    """The lattice's angle (degrees, in (-45, 45]) from nearest-neighbour directions."""
    if len(points) < 2:
        return 0.0
    tree = shapely.STRtree(shapely.points(points))
    angles = []
    for i, p in enumerate(points):
        j = int(tree.query_nearest(shapely.Point(p), exclusive=True, all_matches=False)[0])
        dx, dy = points[j] - points[i]
        angle = math.degrees(math.atan2(dy, dx)) % 90.0
        angles.append(angle - 90.0 if angle > 45.0 else angle)
    return float(np.median(angles))


def suggest_grid_points(
    sheet: Sheet,
    rule: SheetRule,
    *,
    seed: tuple[float, float, float, float],
    interval_m: float = 100.0,
    layer_regex: str = "^mreza$",
    prefix: str | None = None,
) -> GridSuggestion:
    """Grid-cross control points of one sheet from one seed ``(x_pt, y_pt, easting, northing)``."""
    crosses = find_crosses(sheet, layer_regex)
    warnings: list[str] = []
    if len(crosses) < 2:
        return GridSuggestion([], len(crosses), 0.0, (0.0, 0.0), ["fewer than 2 grid crosses"])
    k = sheet_k(rule)
    display = np.array([_display(sheet, c.x_pt, c.y_pt) for c in crosses])
    angle = lattice_angle(display)
    theta = math.radians(angle)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    sx, sy = _display(sheet, seed[0], seed[1])
    delta = (display - (sx, sy)) * k
    # turn the page vectors back by the lattice angle: grid axes = east / north
    ground = np.column_stack(
        (delta[:, 0] * cos_t + delta[:, 1] * sin_t, -delta[:, 0] * sin_t + delta[:, 1] * cos_t)
    )
    approx = ground + (seed[2], seed[3])
    exact = np.round(approx / interval_m) * interval_m
    offsets = approx - exact
    common = np.median(offsets, axis=0)
    off_lattice = np.hypot(*(offsets - common).T) > LATTICE_TOLERANCE * interval_m
    if off_lattice.any():
        warnings.append(f"{int(off_lattice.sum())} crosses off the lattice were left out")
    margin = interval_m / 2 - np.abs(common)
    if (margin < interval_m * 0.1).any():
        warnings.append(
            f"the seed sits {common[0]:+.1f} / {common[1]:+.1f} m from the nearest grid node, "
            "close to half an interval: take a seed read more precisely"
        )
    tag = prefix or rule.id
    points = [
        ControlPoint(
            id=f"{tag}-g{i:03d}",
            sheet=rule.id,
            x_pt=c.x_pt,
            y_pt=c.y_pt,
            easting=float(e),
            northing=float(n),
            source="grid",
            note=f"grid cross, {interval_m:g} m lattice",
        )
        for i, (c, (e, n), bad) in enumerate(zip(crosses, exact, off_lattice, strict=True), start=1)
        if not bad
    ]
    return GridSuggestion(
        points=points,
        crosses=len(crosses),
        angle_deg=round(angle, 4),
        seed_offset_m=(round(float(common[0]), 2), round(float(common[1]), 2)),
        warnings=warnings,
    )
