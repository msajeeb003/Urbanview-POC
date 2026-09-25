"""Geometry building and cleanup (shapely), all in the local frame (ground metres).

Builders:

- ``polygonize_lines``: node on a precision grid, polygonize: faces of linework. Dotted and
  dashed linetypes plotted piece by piece are first reduced to what they stand for (a dot to its
  centre, a dash to its axis: ``reduce_piece``) and their gaps closed by connectors between free
  ends (``bridge``), so the faces sit exactly on the lines' centres;
- ``hole_faces``: wide polylines plotted as filled ribbons: buffer every piece so small gaps
  close, union, take the holes and grow them back by the same distance plus the ribbons' half
  width (mitred, so corners stay sharp);
- ``union_fills``: the filled pieces of one category (solid hatches plot as triangles) merged;
- ``merge_lines``: stroke pieces joined into lines, dash gaps bridged by clustering endpoints.

Cleanup: invalid rings repaired (``make_valid``), parts below the minimum area or thinner than
the sliver width dropped, near-identical duplicates removed, coordinates rounded to a millimetre
so the same PDF always yields the same polygons.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

import numpy as np
import shapely
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry

PRECISION_M = 0.001  # output coordinates are rounded to 1 mm
SLIVER_WIDTH_M = 0.25  # a part thinner than this on average (2 * area / perimeter) is a sliver


def polygon_parts(geom: BaseGeometry | None) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    parts: list[Polygon] = []
    for g in getattr(geom, "geoms", []):
        parts.extend(polygon_parts(g))
    return parts


def line_parts(geom: BaseGeometry | None) -> list[LineString]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, MultiLineString):
        return list(geom.geoms)
    parts: list[LineString] = []
    for g in getattr(geom, "geoms", []):
        parts.extend(line_parts(g))
    return parts


def mean_width(poly: Polygon) -> float:
    """2 * area / perimeter: about the width of a long thin shape (a conservative sliver test)."""
    return 2 * poly.area / poly.length if poly.length else 0.0


def ribbon_width(poly: Polygon) -> float:
    """The width of a ribbon (a wide polyline plotted as its filled outline), from
    area = w * L and perimeter = 2 (w + L); falls back to ``mean_width`` for round shapes."""
    p, a = poly.length, poly.area
    disc = p * p - 16 * a
    return (p - math.sqrt(disc)) / 4 if disc >= 0 else mean_width(poly)


# --- builders ---------------------------------------------------------------------------------


def polygonize_lines(lines: Sequence[BaseGeometry], snap_m: float) -> list[Polygon]:
    """Faces of linework: noded on a ``snap_m`` grid (near-coincident vertices merge), then
    polygonized."""
    if not lines:
        return []
    merged = shapely.union_all(np.asarray(lines, dtype=object), grid_size=snap_m)
    return polygon_parts(shapely.polygonize(line_parts(merged)))


def reduce_piece(poly: BaseGeometry) -> BaseGeometry:
    """A dot or a dash plotted as a small closed shape, as the line it stands for: an elongated
    piece becomes its long axis, a round one its centre point."""
    rect = poly.minimum_rotated_rectangle
    if not isinstance(rect, Polygon):
        return rect.centroid
    c = list(rect.exterior.coords)
    a, b = math.dist(c[0], c[1]), math.dist(c[1], c[2])
    if max(a, b) < 2.5 * max(min(a, b), 1e-9):
        return rect.centroid
    if a >= b:  # the long sides are c0-c1 and c2-c3: join the middles of the short sides
        return LineString(
            [
                ((c[1][0] + c[2][0]) / 2, (c[1][1] + c[2][1]) / 2),
                ((c[3][0] + c[0][0]) / 2, (c[3][1] + c[0][1]) / 2),
            ]
        )
    return LineString(
        [
            ((c[0][0] + c[1][0]) / 2, (c[0][1] + c[1][1]) / 2),
            ((c[2][0] + c[3][0]) / 2, (c[2][1] + c[3][1]) / 2),
        ]
    )


def bridge(pieces: Sequence[BaseGeometry], gap_m: float) -> list[LineString]:
    """Connectors that close a plotted linetype's gaps: every free end (a line's end point, a
    dot) is joined to the free ends of other pieces within ``gap_m``, and to the nearest point of
    any other line within ``gap_m`` (an undershoot at a T junction)."""
    ends: list[tuple[float, float]] = []
    owner: list[int] = []
    for i, geom in enumerate(pieces):
        if isinstance(geom, Point):
            ends.append((geom.x, geom.y))
            owner.append(i)
            continue
        for ln in line_parts(geom):
            if ln.is_closed:
                continue
            ends.extend((ln.coords[0][:2], ln.coords[-1][:2]))
            owner.extend((i, i))
    if not ends or gap_m <= 0:
        return []
    pts = shapely.points(np.array(ends, dtype=float))
    connectors: dict[tuple, LineString] = {}

    def add(a: tuple[float, float], b: tuple[float, float]) -> None:
        if a == b:
            return
        key = (a, b) if a < b else (b, a)
        connectors.setdefault(key, LineString(key))

    left, right = shapely.STRtree(pts).query(pts, predicate="dwithin", distance=gap_m)
    for i, j in zip(left.tolist(), right.tolist(), strict=True):
        if i < j and owner[i] != owner[j]:
            add(ends[i], ends[j])
    line_idx = [i for i, geom in enumerate(pieces) if not isinstance(geom, Point)]
    if line_idx:
        lines = np.asarray([pieces[i] for i in line_idx], dtype=object)
        tree = shapely.STRtree(lines)
        src, dst = tree.query(pts, predicate="dwithin", distance=gap_m)
        best: dict[int, tuple[float, int]] = {}
        for e, t in zip(src.tolist(), dst.tolist(), strict=True):
            if line_idx[t] == owner[e]:
                continue
            d = lines[t].distance(pts[e])
            if e not in best or d < best[e][0]:
                best[e] = (d, t)
        for e, (d, t) in sorted(best.items()):
            if d > 0:
                q = shapely.shortest_line(pts[e], lines[t]).coords[-1]
                add(ends[e], (q[0], q[1]))
    return [connectors[k] for k in sorted(connectors)]


def extend_dangles(
    lines: Sequence[BaseGeometry], extend_m: float, snap_m: float
) -> list[LineString]:
    """Extensions that close the gaps a dotted or dashed linetype leaves where it meets other
    linework: every dangling end is extended along its last direction to the nearest of (a)
    another line, (b) the point where its ray crosses the ray of another dangling end (an X or
    T junction whose arms all stop short), (c) the end of an opposite, collinear dangle, all
    within ``extend_m``. Ends that find nothing stay as they are."""
    if not lines or extend_m <= 0:
        return []
    noded = shapely.union_all(np.asarray(lines, dtype=object), grid_size=snap_m)
    degree: dict[tuple[float, float], int] = {}
    for ln in line_parts(noded):
        if ln.is_closed:
            continue
        for c in (ln.coords[0], ln.coords[-1]):
            degree[c] = degree.get(c, 0) + 1
    ends: list[tuple[tuple[float, float], tuple[float, float]]] = []  # (point, unit direction)
    for chain in line_parts(shapely.line_merge(noded)):
        if chain.is_closed or chain.length <= 0:
            continue
        back = min(extend_m, chain.length / 2)
        for at_start in (True, False):
            e = chain.coords[0] if at_start else chain.coords[-1]
            if degree.get(e, 0) != 1:
                continue
            q = chain.interpolate(back if at_start else chain.length - back)
            dx, dy = e[0] - q.x, e[1] - q.y
            n = math.hypot(dx, dy)
            if n > 0:
                ends.append(((e[0], e[1]), (dx / n, dy / n)))
    if not ends:
        return []
    rays = [LineString([e, (e[0] + d[0] * extend_m, e[1] + d[1] * extend_m)]) for e, d in ends]
    best: list[tuple[float, tuple[float, float]] | None] = [None] * len(ends)

    def offer(i: int, t: float, p: tuple[float, float]) -> None:
        if t > snap_m and t <= extend_m * 2 and (best[i] is None or t < best[i][0]):
            best[i] = (t, p)

    for i, ((e, _), ray) in enumerate(zip(ends, rays, strict=True)):  # (a) other linework
        hit = ray.intersection(noded)
        for part in getattr(hit, "geoms", [hit]):
            if part.is_empty:
                continue
            for c in part.coords if not isinstance(part, Point) else [(part.x, part.y)]:
                t = math.dist(e, c)
                if t <= extend_m:
                    offer(i, t, (c[0], c[1]))
    tree = shapely.STRtree(np.asarray(rays, dtype=object))
    left, right = tree.query(np.asarray(rays, dtype=object), predicate="intersects")
    for i, j in zip(left.tolist(), right.tolist(), strict=True):
        if i >= j:
            continue
        (ei, di), (ej, dj) = ends[i], ends[j]
        hit = rays[i].intersection(rays[j])
        if isinstance(hit, Point):  # (b) crossing rays
            c = (hit.x, hit.y)
            offer(i, math.dist(ei, c), c)
            offer(j, math.dist(ej, c), c)
        elif not hit.is_empty and di[0] * dj[0] + di[1] * dj[1] < -0.9:  # (c) facing ends
            t = math.dist(ei, ej)
            offer(i, t, ej)
            offer(j, t, ei)
    out = []
    for (e, _), b in zip(ends, best, strict=True):
        if b is not None:
            out.append(LineString([e, b[1]]))
    return out


def hole_faces(
    pieces: Sequence[BaseGeometry], gap_m: float, thickness_m: float | None = None
) -> list[Polygon]:
    """Faces enclosed by wide polylines plotted as filled ribbons (see the module docstring).
    ``thickness_m`` defaults to the ribbons' median width."""
    if not pieces:
        return []
    if thickness_m is None:
        widths = sorted(ribbon_width(p) for g in pieces for p in polygon_parts(g))
        thickness_m = widths[len(widths) // 2] if widths else 0.0
    radius = max(gap_m / 2, 1e-4)
    grown = shapely.buffer(np.asarray(pieces, dtype=object), radius, quad_segs=4)
    network = shapely.union_all(grown)
    faces = []
    back = radius + thickness_m / 2
    for part in polygon_parts(network):
        for ring in part.interiors:
            hole = Polygon(ring)
            if hole.area <= 0:
                continue
            faces.append(hole.buffer(back, join_style="mitre", mitre_limit=10.0))
    return faces


def union_fills(polys: Sequence[BaseGeometry], eps_m: float = 0.01) -> list[Polygon]:
    """Touching fill pieces (triangles, strips) merged into areas."""
    if not polys:
        return []
    grown = shapely.buffer(np.asarray(polys, dtype=object), eps_m, quad_segs=2, join_style="mitre")
    merged = shapely.union_all(grown).buffer(-eps_m, join_style="mitre")
    return polygon_parts(merged)


def merge_lines(lines: Sequence[BaseGeometry], gap_m: float) -> list[LineString]:
    """Stroke pieces as continuous lines: endpoints closer than ``gap_m`` are joined."""
    parts = [ln for g in lines for ln in line_parts(g) if ln.length > 0]
    if not parts:
        return []
    if gap_m > 0:
        ends = np.array([c for ln in parts for c in (ln.coords[0], ln.coords[-1])], dtype=float)
        cells: dict[tuple[int, int], list[int]] = {}
        for i, (x, y) in enumerate(ends):
            cells.setdefault((int(math.floor(x / gap_m)), int(math.floor(y / gap_m))), []).append(i)
        parent = list(range(len(ends)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for (cx, cy), members in cells.items():
            near = [
                j
                for dx in (-1, 0, 1)
                for dy in (-1, 0, 1)
                for j in cells.get((cx + dx, cy + dy), [])
            ]
            for i in members:
                for j in near:
                    # never join the two ends of one piece (the dash itself)
                    if j > i and j // 2 != i // 2 and math.dist(ends[i], ends[j]) <= gap_m:
                        parent[find(j)] = find(i)
        groups: dict[int, list[int]] = {}
        for i in range(len(ends)):
            groups.setdefault(find(i), []).append(i)
        centre = {
            i: tuple(ends[members].mean(axis=0)) for members in groups.values() for i in members
        }
        rebuilt = []
        for k, ln in enumerate(parts):
            coords = list(ln.coords)
            coords[0], coords[-1] = centre[2 * k], centre[2 * k + 1]
            if len(coords) == 2 and coords[0] == coords[1]:
                continue
            rebuilt.append(LineString(coords))
        parts = rebuilt
    merged = shapely.line_merge(shapely.union_all(np.asarray(parts, dtype=object), grid_size=1e-3))
    return line_parts(merged)


# --- cleanup ----------------------------------------------------------------------------------


def clean_polygon(
    geom: BaseGeometry, min_area_m2: float
) -> tuple[BaseGeometry | None, set[str], dict[str, int]]:
    """Valid, millimetre-rounded polygon(s) without small or sliver parts."""
    flags: set[str] = set()
    counts = {"invalid_fixed": 0, "slivers_removed": 0, "small_removed": 0}
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
        flags.add("invalid_fixed")
        counts["invalid_fixed"] += 1
    kept = []
    for part in polygon_parts(geom):
        if part.area < min_area_m2:
            counts["small_removed"] += 1
            if kept or len(polygon_parts(geom)) > 1:
                flags.add("sliver_removed")
            continue
        if mean_width(part) < SLIVER_WIDTH_M:
            counts["slivers_removed"] += 1
            flags.add("sliver_removed")
            continue
        kept.append(part)
    if not kept:
        return None, flags, counts
    out = kept[0] if len(kept) == 1 else MultiPolygon(kept)
    out = shapely.set_precision(out, PRECISION_M)
    if not out.is_valid:  # rounding can pinch a ring
        out = shapely.make_valid(out)
        flags.add("invalid_fixed")
        counts["invalid_fixed"] += 1
    parts = [
        p for p in polygon_parts(out) if p.area >= min_area_m2 and mean_width(p) >= SLIVER_WIDTH_M
    ]
    if not parts:
        return None, flags, counts
    return (parts[0] if len(parts) == 1 else MultiPolygon(parts)), flags, counts


def clean_line(geom: BaseGeometry, min_length_m: float) -> BaseGeometry | None:
    parts = [p for p in line_parts(geom) if p.length >= min_length_m]
    if not parts:
        return None
    out = parts[0] if len(parts) == 1 else MultiLineString(parts)
    return shapely.set_precision(out, PRECISION_M)


def duplicate_of(
    geom: BaseGeometry, kept: Iterable[BaseGeometry], tolerance: float = 0.9
) -> int | None:
    """Index of a kept polygon this one overlaps by more than ``tolerance`` (intersection over
    the smaller area), or None."""
    for i, other in enumerate(kept):
        if not geom.intersects(other):
            continue
        inter = geom.intersection(other).area
        smaller = min(geom.area, other.area)
        if smaller > 0 and inter / smaller >= tolerance:
            return i
    return None


def is_sliver(geom: BaseGeometry, min_area_m2: float) -> bool:
    return any(p.area < min_area_m2 or mean_width(p) < SLIVER_WIDTH_M for p in polygon_parts(geom))


# --- sheets of one drawing --------------------------------------------------------------------


def vertices(geoms: Iterable[BaseGeometry], limit: int = 50000) -> np.ndarray:
    pts: list[tuple[float, float]] = []
    for g in geoms:
        for ln in line_parts(g) or [ln for p in polygon_parts(g) for ln in (p.exterior,)]:
            pts.extend(ln.coords)
        if len(pts) >= limit:
            break
    return np.round(np.array(pts[:limit], dtype=float), 3) if pts else np.zeros((0, 2))


def estimate_offset(
    a: np.ndarray, b: np.ndarray, bin_m: float = 0.5, sample: int = 1000
) -> tuple[float, float, int, float]:
    """Translation that maps sheet ``b``'s local frame onto sheet ``a``'s: the vertex difference
    most pairs agree on (both sheets draw the same geometry where they overlap).
    Returns (dx, dy, votes, share of the sampled vertices of ``a`` that agree)."""
    if len(a) == 0 or len(b) == 0:
        return 0.0, 0.0, 0, 0.0
    rng = np.random.default_rng(7)  # a fixed seed: the same sheets give the same answer
    sa = a[rng.choice(len(a), size=min(sample, len(a)), replace=False)]
    sb = b[rng.choice(len(b), size=min(sample * 4, len(b)), replace=False)]
    diffs = (sa[:, None, :] - sb[None, :, :]).reshape(-1, 2)
    cells = np.floor(diffs / bin_m).astype(np.int64)
    keys = (cells[:, 0] << 32) + (cells[:, 1] & 0xFFFFFFFF)
    uniq, counts = np.unique(keys, return_counts=True)
    peak = uniq[int(np.argmax(counts))]
    px, py = peak >> 32, np.int64(np.int32(peak & 0xFFFFFFFF))
    # the peak can straddle bins: refine on the 3 x 3 bins around it
    near = (np.abs(cells[:, 0] - px) <= 1) & (np.abs(cells[:, 1] - py) <= 1)
    dx, dy = np.median(diffs[near], axis=0)
    votes = int(near.sum())
    return round(float(dx), 3), round(float(dy), 3), votes, round(min(1.0, votes / len(sa)), 3)
