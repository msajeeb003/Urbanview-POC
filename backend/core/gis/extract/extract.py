"""The extraction pipeline: a document's rules and sheets -> target layers, attributes, QA.

Layers are built in ``TARGET_LAYERS`` order, so the plan boundary exists before the parcels it
closes and clips, and the parcels before the blocks and land use derived from them. Per sheet:
select the paths, build candidate polygons (or lines) in the local frame, clean them, read the
labels, assign each label to the polygon containing it (else the nearest within
``max_distance_mm``). Sheets of one drawing are merged afterwards (same label value: touching
pieces are unioned, overlapping copies deduplicated). Everything is sorted before it is numbered,
so the same PDF and rules always give the same features, keys and digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pymupdf
import shapely
from shapely.geometry import MultiLineString, MultiPolygon, Point
from shapely.geometry.base import BaseGeometry

from core.gis.extract import geometry as g
from core.gis.extract.glyphs import read_glyph_labels
from core.gis.extract.rules import (
    LINE_LAYERS,
    STAGED_AS,
    TARGET_LAYERS,
    Category,
    DocumentRules,
    LabelRule,
    LayerRule,
    Selector,
    SheetRule,
)
from core.gis.extract.sheet import (
    Sheet,
    SheetPath,
    load_sheet,
    path_lines,
    path_polygon,
    subpaths,
)

EXTRACTOR_VERSION = "vector-pdf-1"
# every flag a feature can carry (the ticket's four first)
QA_FLAGS = (
    "unlabelled",
    "invalid_fixed",
    "sliver_removed",
    "multi_label",
    "label_nearest",
    "label_low_score",
    "fallback_face",
    "merged_faces",
    "duplicate_label",
)
ATTRIBUTES = {  # the attribute each layer's labels fill, and its staged property name
    "urban_parcels": "urban_parcel_number",
    "urban_blocks": "block_ref",
    "planned_land_use": "code",
    "planned_traffic": "road_class",
}

# (sheet rule, sha256 or None) -> the sheet's PDF bytes; (path, sha256) -> a table PDF or None
SheetLoader = Callable[[SheetRule], bytes]
FileLoader = Callable[[str, str | None], bytes | None]


@dataclass(frozen=True, slots=True)
class Label:
    value: str  # the attribute value (the pattern's group 1)
    text: str  # the label as read, after overrides
    point: Point  # local frame
    sheet: str
    bbox: tuple[float, float, float, float]  # sheet PDF points, origin bottom-left
    score: float | None  # glyph labels: the weakest character's match score
    path_ids: tuple[str, ...]


@dataclass
class Feature:
    layer: str
    geom: BaseGeometry  # local frame
    sheet: str
    page: int
    source_paths: list[str]
    attrs: dict[str, Any] = field(default_factory=dict)
    labels: list[Label] = field(default_factory=list)
    qa_flags: set[str] = field(default_factory=set)
    key: str = ""
    source_bbox: list[float] | None = None

    @property
    def label_text(self) -> str | None:
        texts = sorted({lab.text for lab in self.labels})
        return " | ".join(texts) if texts else None

    def properties(self, document_id: int | None, document_ref: str) -> dict[str, Any]:
        """The feature's attributes as the GeoPackage and the drafts table store them."""
        props: dict[str, Any] = {
            "feature_key": self.key,
            "document_id": document_id,
            "document_ref": document_ref,
            "sheet": self.sheet,
            "page": self.page,
            "source_paths": sorted(set(self.source_paths), key=_path_order),
            "source_bbox": self.source_bbox,
            "label_text": self.label_text,
            "label_bbox": list(self.labels[0].bbox) if self.labels else None,
            "qa_flags": [f for f in QA_FLAGS if f in self.qa_flags],
        }
        if self.layer in LINE_LAYERS:
            props["length_m"] = round(self.geom.length, 2)
        else:
            props["area_m2"] = round(self.geom.area, 1)
        props.update(self.attrs)
        return props


@dataclass
class Extraction:
    rules: DocumentRules
    document_id: int | None
    layers: dict[str, list[Feature]]
    qa: dict[str, Any]
    sheets: dict[str, Sheet]

    def staged_layer(self, layer: str) -> str:
        return STAGED_AS[layer]


def _path_order(path_id: str) -> tuple[int, int]:
    page, _, seq = path_id[1:].partition(":")
    return (int(page or 0), int(seq or 0))


# --- small helpers ------------------------------------------------------------------------------


def _local(sheet: Sheet, geoms: list[BaseGeometry]) -> list[BaseGeometry]:
    """Page frame -> the document's local frame (ground metres)."""
    if not geoms:
        return []
    k = sheet.metres_per_pt
    ox, oy = sheet.rule.offset_m
    h = sheet.height_pt

    def fn(xy: np.ndarray) -> np.ndarray:
        return np.column_stack((xy[:, 0] * k + ox, (h - xy[:, 1]) * k + oy))

    return list(shapely.transform(np.asarray(geoms, dtype=object), fn))


def _sheet_rect(sheet: Sheet, page_bbox: tuple[float, float, float, float]) -> tuple:
    x0, y0, x1, y1 = page_bbox
    return (
        round(x0, 1),
        round(sheet.height_pt - y1, 1),
        round(x1, 1),
        round(sheet.height_pt - y0, 1),
    )


def _m(sheet: Sheet, mm: float | None) -> float:
    """Millimetres on paper -> ground metres at the sheet's scale."""
    return (mm or 0.0) * sheet.rule.scale / 1000.0


def _order_key(geom: BaseGeometry) -> tuple[float, float]:
    """Reading order: top to bottom, then left to right (local y points north)."""
    p = geom.representative_point()
    return (-round(p.y, 1), round(p.x, 1))


def _is_closed(p: SheetPath) -> bool:
    if p.close or any(it[0] in ("re", "qu") for it in p.items):
        return True
    subs = subpaths(p.items)
    return bool(subs) and all(s[0] == s[-1] or len(s) >= 3 and _near(s[0], s[-1]) for s in subs)


def _near(a: tuple[float, float], b: tuple[float, float], tol: float = 0.05) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


# --- labels ---------------------------------------------------------------------------------------


def read_labels(
    sheet: Sheet, rule: LabelRule, overrides: dict[str, str]
) -> tuple[list[Label], list[dict]]:
    """The labels of one sheet matching the rule's pattern; the second list reports glyph labels
    read below ``min_score`` and texts an override dropped."""
    pattern = re.compile(rule.pattern)
    raw: list[tuple[str, tuple, float | None, tuple[str, ...]]] = []
    if rule.source == "glyphs":
        for gl in read_glyph_labels(sheet, rule):
            raw.append((gl.text, gl.bbox, gl.score, gl.path_ids))
    else:
        for t in sheet.select_texts(rule.select):
            raw.append((t.text, t.bbox, None, ()))
    labels: list[Label] = []
    notes: list[dict] = []
    for text, bbox, score, ids in raw:
        fixed = overrides.get(f"{sheet.rule.id}:{text}", overrides.get(text, text))
        sheet_bbox = _sheet_rect(sheet, bbox)
        if fixed == "":
            notes.append(
                {
                    "kind": "dropped_by_override",
                    "text": text,
                    "sheet": sheet.rule.id,
                    "bbox": sheet_bbox,
                }
            )
            continue
        m = pattern.search(fixed)
        if not m:
            continue
        centre = Point((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
        label = Label(
            value=m.group(1),
            text=m.group(0).strip(),
            point=_local(sheet, [centre])[0],
            sheet=sheet.rule.id,
            bbox=sheet_bbox,
            score=score,
            path_ids=ids,
        )
        if score is not None and score < rule.min_score and fixed == text:
            notes.append(
                {
                    "kind": "low_score",
                    "text": text,
                    "score": score,
                    "sheet": sheet.rule.id,
                    "bbox": sheet_bbox,
                }
            )
        labels.append(label)
    labels.sort(key=lambda lab: (lab.value, lab.bbox))
    return labels, notes


def assign_labels(
    faces: list[BaseGeometry], labels: list[Label], max_distance_m: float
) -> tuple[dict[int, list[Label]], dict[int, set[str]], list[Label]]:
    """Each label to the face containing its centre (the deepest one when faces overlap), else
    to the nearest face within ``max_distance_m``. Returns labels per face, per-face flags and
    the labels no face took."""
    per_face: dict[int, list[Label]] = defaultdict(list)
    flags: dict[int, set[str]] = defaultdict(set)
    unassigned: list[Label] = []
    if not faces:
        return per_face, flags, list(labels)
    tree = shapely.STRtree(np.asarray(faces, dtype=object))
    for lab in labels:
        inside = tree.query(lab.point, predicate="within").tolist()
        if inside:
            best = max(inside, key=lambda i: (faces[i].boundary.distance(lab.point), -i))
            per_face[best].append(lab)
            continue
        if max_distance_m > 0:
            idx, dist = tree.query_nearest(
                lab.point, max_distance=max_distance_m, return_distance=True
            )
            if len(idx):
                best = int(sorted(zip(dist.tolist(), idx.tolist(), strict=True))[0][1])
                per_face[best].append(lab)
                flags[best].add("label_nearest")
                continue
        unassigned.append(lab)
    return per_face, flags, unassigned


def _values(labels: Iterable[Label]) -> list[str]:
    return sorted({lab.value for lab in labels})


# --- per-layer builders -----------------------------------------------------------------------


@dataclass
class _Candidate:
    geom: BaseGeometry
    sheet: Sheet
    paths: list[str]
    labels: list[Label] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Context:
    rules: DocumentRules
    sheets: dict[str, Sheet]
    layers: dict[str, list[Feature]] = field(default_factory=dict)
    qa: dict[str, dict[str, Any]] = field(default_factory=dict)
    boundary: BaseGeometry | None = None
    parcel_blocks: dict[str, str] = field(default_factory=dict)

    def sheets_for(self, layer: str) -> list[Sheet]:
        return [s for s in self.sheets.values() if layer in s.rule.layers]

    def counter(self, layer: str) -> dict[str, Any]:
        return self.qa.setdefault(layer, defaultdict(int))


def _linework(sheet: Sheet, paths: list[SheetPath], rule: LayerRule) -> tuple[list, list[str]]:
    """Paths as linework in the local frame: strokes as lines, filled shapes as their outlines,
    small closed pieces (dots, dashes) as their centre point or axis."""
    geoms: list[BaseGeometry] = []
    ids: list[str] = []
    for p in paths:
        if p.size_mm <= rule.dot_max_mm and (p.fill is not None or _is_closed(p)):
            poly = path_polygon(p)
            if poly is None:
                continue
            geoms.append(g.reduce_piece(poly))
            ids.append(p.id)
        elif p.fill is not None:
            poly = path_polygon(p)
            if poly is None:
                continue
            geoms.append(poly.boundary)
            ids.append(p.id)
        else:
            for ln in path_lines(p):
                geoms.append(ln)
                ids.append(p.id)
    return _local(sheet, geoms), ids


def _closing_lines(ctx: _Context, rule: LayerRule) -> list[BaseGeometry]:
    out = []
    for name in rule.closing:
        for f in ctx.layers.get(name, []):
            out.extend(p.boundary for p in g.polygon_parts(f.geom))
    return out


def _faces(
    ctx: _Context, sheet: Sheet, rule: LayerRule, selectors: list[Selector]
) -> tuple[list, list, list[str]]:
    """Candidate faces of one sheet plus the linework and its path ids (for provenance)."""
    paths = sheet.select(selectors)
    lines, ids = _linework(sheet, paths, rule)
    gap = _m(sheet, rule.gap_mm)
    closing = _closing_lines(ctx, rule)
    # the closing layers take part in the bridging: a dotted line stopping short of the plan
    # boundary is joined to it
    connectors = g.bridge(lines + closing, gap) if gap > 0 else []
    work = [x for x in lines if not isinstance(x, Point)] + connectors + closing
    snap = max(_m(sheet, rule.snap_mm), 1e-4)
    if rule.extend_mm > 0:
        work += g.extend_dangles(work, _m(sheet, rule.extend_mm), snap)
    faces = g.polygonize_lines(work, snap)
    return faces, lines, ids


def _hole_faces(ctx: _Context, sheet: Sheet, rule: LayerRule) -> tuple[list, list, list[str]]:
    paths = sheet.select(rule.select)
    pieces: list[BaseGeometry] = []
    ids: list[str] = []
    for p in paths:
        if p.fill is not None:
            poly = path_polygon(p)
            if poly is not None:
                pieces.append(poly)
                ids.append(p.id)
        else:
            for ln in path_lines(p):
                pieces.append(ln)
                ids.append(p.id)
    local = _local(sheet, pieces)
    widths = sorted(g.ribbon_width(q) for x in local for q in g.polygon_parts(x))
    thickness = widths[len(widths) // 2] if widths else 0.0
    closing = _closing_lines(ctx, rule)
    faces = g.hole_faces(local + closing, max(_m(sheet, rule.gap_mm), 1e-3), thickness)
    return faces, local, ids


def _provenance(
    faces: list[BaseGeometry], linework: list[BaseGeometry], ids: list[str], tol: float
) -> list[list[str]]:
    """The raw path ids drawing each face's boundary."""
    if not linework:
        return [[] for _ in faces]
    tree = shapely.STRtree(np.asarray(linework, dtype=object))
    out = []
    for face in faces:
        hits = tree.query(face.boundary.buffer(tol), predicate="intersects").tolist()
        out.append(sorted({ids[i] for i in hits}, key=_path_order))
    return out


def _clean_faces(
    ctx: _Context, layer: str, rule: LayerRule, faces: list[BaseGeometry], count: bool = True
) -> list[tuple[BaseGeometry, set[str]]]:
    """Clip to the plan boundary, repair, drop small and sliver parts (a face that is nothing
    but sliver is dropped whole), drop faces above ``max_area_m2``."""
    qa = ctx.counter(layer) if count else defaultdict(int)
    out = []
    for face in faces:
        geom = face
        if rule.clip and ctx.boundary is not None and layer != "plan_boundary":
            if not geom.intersects(ctx.boundary):
                qa["outside_dropped"] += 1
                continue
            geom = geom.intersection(ctx.boundary)
        cleaned, flags, counts = g.clean_polygon(geom, rule.min_area_m2)
        qa["invalid_fixed"] += counts["invalid_fixed"]
        if cleaned is None:
            qa["slivers_removed" if counts["slivers_removed"] else "small_removed"] += 1
            continue
        qa["slivers_removed"] += counts["slivers_removed"]
        qa["small_removed"] += counts["small_removed"]
        if rule.max_area_m2 is not None and cleaned.area > rule.max_area_m2:
            qa["too_large_dropped"] += 1
            continue
        out.append((cleaned, flags))
    return out


def _labelled_candidates(
    ctx: _Context,
    layer: str,
    rule: LayerRule,
    sheet: Sheet,
    faces: list[tuple[BaseGeometry, set[str]]],
    provenance: list[list[str]],
    fallback: Callable[[], tuple[list, list[list[str]]]] | None,
) -> list[_Candidate]:
    qa = ctx.counter(layer)
    cands = [
        _Candidate(geom, sheet, paths, flags=set(flags))
        for (geom, flags), paths in zip(faces, provenance, strict=True)
    ]
    if rule.labels is None:
        return cands
    labels, notes = read_labels(sheet, rule.labels, ctx.rules.label_overrides)
    qa["labels_read"] += len(labels)
    qa.setdefault("label_notes", []).extend(notes)
    if rule.clip and ctx.boundary is not None:
        inside = [
            lab for lab in labels if ctx.boundary.intersects(lab.point.buffer(_m(sheet, 1.0)))
        ]
        qa["labels_outside"] += len(labels) - len(inside)
        labels = inside
    nearest = _m(sheet, rule.labels.max_distance_mm)
    # with a fallback, a label outside every face first looks for a fallback face, and only
    # then for the nearest face
    per_face, flags, unassigned = assign_labels(
        [c.geom for c in cands], labels, 0.0 if fallback is not None else nearest
    )
    for i, labs in per_face.items():
        cands[i].labels = labs
        cands[i].flags |= flags.get(i, set())
    if fallback is not None:
        cands, unassigned = _resolve_with_fallback(cands, unassigned, fallback, sheet)
        if unassigned and nearest > 0:
            per_face, flags, unassigned = assign_labels(
                [c.geom for c in cands], unassigned, nearest
            )
            for i, labs in per_face.items():
                cands[i].labels = cands[i].labels + labs
                cands[i].flags |= flags.get(i, set())
    qa.setdefault("labels_unassigned", []).extend(
        {"text": lab.text, "sheet": lab.sheet, "bbox": list(lab.bbox)} for lab in unassigned
    )
    for c in cands:
        if any(lab.score is not None and lab.score < rule.labels.min_score for lab in c.labels):
            c.flags.add("label_low_score")
    return cands


def _resolve_with_fallback(
    cands: list[_Candidate], unassigned: list[Label], fallback: Callable, sheet: Sheet
) -> tuple[list[_Candidate], list[Label]]:
    """Faces holding labels of several parcels (their separating edges are drawn only on the
    fallback linework, e.g. the cadastral base) are replaced by the fallback faces inside them;
    fallback pieces without a label join the labelled neighbour they share the longest edge
    with. Labels no primary face took look for a fallback face too."""
    multi = [i for i, c in enumerate(cands) if len(_values(c.labels)) > 1]
    if not multi and not unassigned:
        return cands, unassigned
    fb_faces, fb_paths = fallback()
    if not fb_faces:
        return cands, unassigned
    fb_geoms = [f for f, _ in fb_faces]
    tree = shapely.STRtree(np.asarray(fb_geoms, dtype=object))
    replaced: set[int] = set()
    new: list[_Candidate] = []
    for i in multi:
        primary = cands[i]
        inside = [
            j
            for j in tree.query(primary.geom, predicate="intersects").tolist()
            if primary.geom.contains(fb_geoms[j].representative_point())
        ]
        if not inside:
            continue
        pieces = [fb_geoms[j] for j in inside]
        per_piece, flags, left = assign_labels(pieces, primary.labels, 0.0)
        owner: dict[int, int] = {k: k for k in per_piece}  # piece -> piece that owns it
        _absorb(pieces, owner)
        groups: dict[int, list[int]] = defaultdict(list)
        for k, o in owner.items():
            groups[o].append(k)
        if len(groups) < 2:
            continue  # the fallback does not separate them either: keep the primary face
        replaced.add(i)
        for o, members in sorted(groups.items()):
            geom = shapely.union_all([pieces[k] for k in members])
            paths = sorted({p for k in members for p in fb_paths[inside[k]]}, key=_path_order)
            c = _Candidate(
                geom,
                sheet,
                paths,
                labels=per_piece[o],
                flags={"fallback_face"} | flags.get(o, set()),
            )
            new.append(c)
        if left:
            unassigned.extend(left)
    still: list[Label] = []
    taken = [c.geom for k, c in enumerate(cands) if k not in replaced] + [c.geom for c in new]
    for lab in unassigned:
        hit = tree.query(lab.point, predicate="within").tolist()
        hit = [j for j in hit if not any(t.contains(lab.point) for t in taken)]
        if not hit:
            still.append(lab)
            continue
        j = min(hit, key=lambda k: fb_geoms[k].area)
        geom = fb_geoms[j]
        for t in taken:
            if geom.intersects(t):
                geom = geom.difference(t)
        if geom.is_empty or geom.area <= 0:
            still.append(lab)
            continue
        c = _Candidate(geom, sheet, list(fb_paths[j]), labels=[lab], flags={"fallback_face"})
        new.append(c)
        taken.append(geom)
    kept = [c for k, c in enumerate(cands) if k not in replaced]
    return kept + new, still


def _absorb(pieces: list[BaseGeometry], owner: dict[int, int]) -> None:
    """Give every unowned piece to the owned neighbour it shares the longest edge with,
    repeatedly, so chains of unlabelled pieces follow their parcel (deterministic order)."""
    if not owner:
        return
    changed = True
    while changed:
        changed = False
        for k in range(len(pieces)):
            if k in owner:
                continue
            best: tuple[float, int] | None = None
            for n, o in sorted(owner.items()):
                if not pieces[k].touches(pieces[n]) and not pieces[k].intersects(pieces[n]):
                    continue
                shared = pieces[k].boundary.intersection(pieces[n].boundary).length
                if shared > 0 and (
                    best is None or shared > best[0] or (shared == best[0] and o < best[1])
                ):
                    best = (shared, o)
            if best is not None:
                owner[k] = best[1]
                changed = True


def _fallback_for(
    ctx: _Context, layer: str, rule: LayerRule, sheet: Sheet, tol: float
) -> Callable[[], tuple[list, list[list[str]]]]:
    """Faces of the primary and the fallback linework together, built only when needed."""

    def build() -> tuple[list, list[list[str]]]:
        faces, lines, ids = _faces(ctx, sheet, rule, [*rule.select, *rule.fallback])
        cleaned = _clean_faces(ctx, layer, rule, faces, count=False)
        return cleaned, _provenance([c for c, _ in cleaned], lines, ids, tol)

    return build


def _build_polygons(ctx: _Context, layer: str, rule: LayerRule) -> list[_Candidate]:
    cands: list[_Candidate] = []
    for sheet in ctx.sheets_for(layer):
        if rule.method == "holes":
            faces, linework, ids = _hole_faces(ctx, sheet, rule)
        else:
            faces, linework, ids = _faces(ctx, sheet, rule, rule.select)
        ctx.counter(layer)["faces_built"] += len(faces)
        cleaned = _clean_faces(ctx, layer, rule, faces)
        tol = max(_m(sheet, rule.snap_mm), 1e-3) * 2
        prov = _provenance([c for c, _ in cleaned], linework, ids, tol)
        fallback = None
        if rule.fallback and rule.method == "polygonize":
            fallback = _fallback_for(ctx, layer, rule, sheet, tol)
        cands.extend(_labelled_candidates(ctx, layer, rule, sheet, cleaned, prov, fallback))
    return cands


SAMPLE_POINTS = 400  # coverage samples per classified polygon


@dataclass
class _CategoryIndex:
    """One category's drawing on the sheets, indexed for point queries."""

    lines: shapely.STRtree | None
    fills: shapely.STRtree | None


def _category_index(sheets: list[Sheet], cat: Category) -> _CategoryIndex:
    polys: list[BaseGeometry] = []
    lines: list[BaseGeometry] = []
    for sheet in sheets:
        fills, strokes = [], []
        for p in sheet.select(cat.select):
            if p.fill is not None:
                poly = path_polygon(p)
                if poly is not None:
                    fills.append(poly)
            else:
                strokes.extend(path_lines(p))
        polys.extend(_local(sheet, fills))
        lines.extend(_local(sheet, strokes))
    return _CategoryIndex(
        lines=shapely.STRtree(np.asarray(lines, dtype=object)) if lines else None,
        fills=shapely.STRtree(np.asarray(polys, dtype=object)) if polys else None,
    )


def _sample_points(geom: BaseGeometry, n: int = SAMPLE_POINTS) -> np.ndarray:
    """A regular grid of about ``n`` points inside the polygon (deterministic)."""
    x0, y0, x1, y1 = geom.bounds
    step = max((geom.area / n) ** 0.5, 1e-3)
    xs = np.arange(x0 + step / 2, x1, step)
    ys = np.arange(y0 + step / 2, y1, step)
    gx, gy = np.meshgrid(xs, ys)
    gx, gy = gx.ravel(), gy.ravel()
    inside = shapely.contains_xy(geom, gx, gy)
    pts = shapely.points(np.column_stack((gx[inside], gy[inside])))
    return pts if len(pts) else shapely.points([geom.representative_point().coords[0]])


def _shares(
    geom: BaseGeometry, index: dict[str, _CategoryIndex], hatch_m: float
) -> list[tuple[float, str]]:
    """Share of the polygon each category covers: sample points inside its fills or within
    half the hatch spacing of its hatch lines. Sorted, largest first."""
    pts = _sample_points(geom)
    out = []
    for code, idx in index.items():
        hit = np.zeros(len(pts), dtype=bool)
        if idx.fills is not None:
            src, _ = idx.fills.query(pts, predicate="intersects")
            hit[src] = True
        if idx.lines is not None and hatch_m > 0:
            src, _ = idx.lines.query(pts, predicate="dwithin", distance=hatch_m / 2)
            hit[src] = True
        share = round(float(hit.mean()), 3)
        if share > 0:
            out.append((share, code))
    return sorted(out, key=lambda sc: (-sc[0], sc[1]))


def _build_fills(ctx: _Context, layer: str, rule: LayerRule) -> list[_Candidate]:
    cands: list[_Candidate] = []
    categories = rule.categories or [Category(select=rule.select, code="")]
    for sheet in ctx.sheets_for(layer):
        for cat in categories:
            polys = []
            ids = []
            for p in sheet.select(cat.select):
                if p.fill is None:
                    continue
                poly = path_polygon(p)
                if poly is not None:
                    polys.append(poly)
                    ids.append(p.id)
            local = _local(sheet, polys)
            parts = g.union_fills(local)
            ctx.counter(layer)["faces_built"] += len(parts)
            cleaned = _clean_faces(ctx, layer, rule, parts)
            prov = _provenance([c for c, _ in cleaned], local, ids, 1e-3)
            attrs = {"code": cat.code or None, "name": cat.name}
            batch = [
                _Candidate(geom, sheet, paths, flags=set(flags), attrs=dict(attrs))
                for (geom, flags), paths in zip(cleaned, prov, strict=True)
            ]
            if rule.labels is not None:
                labels, notes = read_labels(sheet, rule.labels, ctx.rules.label_overrides)
                ctx.counter(layer).setdefault("label_notes", []).extend(notes)
                per_face, flags, _ = assign_labels([c.geom for c in batch], labels, 0.0)
                for i, labs in per_face.items():
                    batch[i].labels = labs
                    batch[i].flags |= flags.get(i, set())
            cands.extend(batch)
    return cands


def _build_classify(ctx: _Context, layer: str, rule: LayerRule) -> list[_Candidate]:
    """Polygons of another layer, each coded by the category covering most of it (a code label
    inside the polygon wins)."""
    source = ctx.layers.get(rule.classify_from or "", [])
    sheets = ctx.sheets_for(layer) or [ctx.sheets[f.sheet] for f in source[:1]]
    index = {cat.code: _category_index(sheets, cat) for cat in rule.categories}
    hatch_m = _m(sheets[0], rule.gap_mm) if sheets else 0.0
    names = {cat.code: cat.name for cat in rule.categories}
    labels: list[Label] = []
    if rule.labels is not None:
        for sheet in sheets:
            labs, notes = read_labels(sheet, rule.labels, ctx.rules.label_overrides)
            labels.extend(labs)
            ctx.counter(layer).setdefault("label_notes", []).extend(notes)
    per_face, _, _ = assign_labels([f.geom for f in source], labels, 0.0)
    qa = ctx.counter(layer)
    cands = []
    for i, f in enumerate(source):
        shares = _shares(f.geom, index, hatch_m)
        labs = per_face.get(i, [])
        code = None
        flags: set[str] = set()
        if labs:
            vals = _values(labs)
            code = vals[0]
            if len(vals) > 1:
                flags.add("multi_label")
        elif shares and shares[0][0] >= rule.min_share:
            code = shares[0][1]
        if code is None:
            qa["unclassified"] += 1
            flags.add("unlabelled")
        attrs = {
            "code": code,
            "name": names.get(code) if code else None,
            "share": shares[0][0] if shares else 0.0,
        }
        if f.attrs.get("urban_parcel_number"):
            attrs["urban_parcel_number"] = f.attrs["urban_parcel_number"]
        cands.append(
            _Candidate(
                f.geom,
                ctx.sheets[f.sheet],
                list(f.source_paths),
                labels=labs,
                flags=flags,
                attrs=attrs,
            )
        )
    return cands


def _build_derived(ctx: _Context, layer: str, rule: LayerRule) -> list[_Candidate]:
    key = rule.derive_by or ""
    groups: dict[str, list[Feature]] = defaultdict(list)
    missing = 0
    for f in ctx.layers.get(rule.derive_from or "", []):
        value = f.attrs.get(key)
        if value:
            groups[str(value)].append(f)
        else:
            missing += 1
    ctx.counter(layer)["source_without_key"] += missing
    cands = []
    for value, feats in sorted(groups.items()):
        geom = shapely.union_all(np.asarray([f.geom for f in feats], dtype=object))
        if rule.close_m > 0:
            geom = geom.buffer(rule.close_m, join_style="mitre").buffer(
                -rule.close_m, join_style="mitre"
            )
        if rule.clip and ctx.boundary is not None:
            geom = geom.intersection(ctx.boundary)
        cleaned, flags, counts = g.clean_polygon(geom, rule.min_area_m2)
        ctx.counter(layer)["invalid_fixed"] += counts["invalid_fixed"]
        ctx.counter(layer)["slivers_removed"] += counts["slivers_removed"]
        if cleaned is None:
            continue
        sheet_ids = sorted(
            {f.sheet for f in feats}, key=lambda s: (-sum(f.sheet == s for f in feats), s)
        )
        paths = sorted({p for f in feats for p in f.source_paths}, key=_path_order)
        attrs = {ATTRIBUTES.get(layer, key): value}
        cands.append(
            _Candidate(cleaned, ctx.sheets[sheet_ids[0]], paths, flags=set(flags), attrs=attrs)
        )
    return cands


def _build_lines(ctx: _Context, layer: str, rule: LayerRule) -> list[_Candidate]:
    cands = []
    categories = rule.categories or [Category(select=rule.select, code=rule.road_class or "")]
    for sheet in ctx.sheets_for(layer):
        for cat in categories:
            paths = sheet.select(cat.select)
            lines: list[BaseGeometry] = []
            ids: list[str] = []
            for p in paths:
                for ln in path_lines(p):
                    lines.append(ln)
                    ids.append(p.id)
            local = _local(sheet, lines)
            merged = g.merge_lines(local, _m(sheet, rule.gap_mm))
            tree = shapely.STRtree(np.asarray(local, dtype=object)) if local else None
            for ln in merged:
                geom: BaseGeometry | None = ln
                if rule.clip and ctx.boundary is not None:
                    geom = ln.intersection(ctx.boundary)
                geom = g.clean_line(geom, rule.min_length_m) if geom is not None else None
                if geom is None:
                    ctx.counter(layer)["short_removed"] += 1
                    continue
                src = (
                    sorted(
                        {
                            ids[i]
                            for i in tree.query(geom.buffer(0.05), predicate="intersects").tolist()
                        },
                        key=_path_order,
                    )
                    if tree
                    else []
                )
                cands.append(
                    _Candidate(
                        geom, sheet, src, attrs={"road_class": cat.code or None, "name": cat.name}
                    )
                )
    return cands


BUILDERS: dict[str, Callable[[_Context, str, LayerRule], list[_Candidate]]] = {
    "polygonize": _build_polygons,
    "holes": _build_polygons,
    "fills": _build_fills,
    "classify": _build_classify,
    "derive": _build_derived,
    "lines": _build_lines,
}


# --- merging, keys, QA -------------------------------------------------------------------------


def _attribute_of(layer: str, cand: _Candidate, ctx: _Context) -> None:
    """The label value becomes the layer's attribute; parcels also get their block."""
    attr = ATTRIBUTES.get(layer)
    vals = _values(cand.labels)
    if attr and vals and attr not in cand.attrs:
        # several distinct values: the deepest label wins, the feature is flagged
        if len(vals) > 1:
            cand.flags.add("multi_label")
            deepest = max(
                cand.labels, key=lambda lab: (cand.geom.boundary.distance(lab.point), lab.value)
            )
            cand.attrs[attr] = deepest.value
        else:
            cand.attrs[attr] = vals[0]
    elif attr and not vals and attr not in cand.attrs and layer != "planned_traffic":
        cand.flags.add("unlabelled")
    if layer == "urban_parcels" and cand.attrs.get("urban_parcel_number"):
        block = ctx.parcel_blocks.get(cand.attrs["urban_parcel_number"])
        if block is None and ctx.rules.parcel_blocks and ctx.rules.parcel_blocks.from_number:
            m = re.search(ctx.rules.parcel_blocks.from_number, cand.attrs["urban_parcel_number"])
            block = m.group(1) if m else None
        if block:
            cand.attrs["block_ref"] = block


def _merge(layer: str, cands: list[_Candidate], qa: dict[str, Any]) -> list[_Candidate]:
    """One feature per attribute value: pieces of the same value that touch or overlap (the
    same parcel on two sheets, or split by stray linework) are unioned; disjoint pieces stay
    separate and are flagged ``duplicate_label``. Unlabelled faces overlapping a kept face by
    90 % are duplicates (the overlap of two sheets) and dropped."""
    attr = ATTRIBUTES.get(layer)
    if layer in ("planned_land_use", "planned_traffic"):
        return _dedupe_copies(layer, cands, qa)
    if attr is None:
        return cands
    by_value: dict[str, list[_Candidate]] = defaultdict(list)
    rest: list[_Candidate] = []
    for c in cands:
        v = c.attrs.get(attr)
        (by_value[str(v)] if v else rest).append(c)
    out: list[_Candidate] = []
    for value in sorted(by_value):
        group = sorted(by_value[value], key=lambda c: (-c.geom.area, c.sheet.rule.id))
        clusters: list[list[_Candidate]] = []
        for c in group:
            for cl in clusters:
                if any(c.geom.intersects(o.geom) for o in cl):
                    cl.append(c)
                    break
            else:
                clusters.append([c])
        for cl in clusters:
            head = cl[0]
            if len(cl) > 1:
                merged = shapely.union_all(np.asarray([c.geom for c in cl], dtype=object))
                areas = sum(c.geom.area for c in cl)
                if (
                    merged.area < 0.9 * areas
                ):  # copies of one face (sheet overlap): keep the largest
                    qa["duplicates_removed"] += len(cl) - 1
                    if merged.area > head.geom.area * 1.02:
                        head.geom = merged
                        head.flags.add("merged_faces")
                else:
                    head.geom = merged
                    head.flags.add("merged_faces")
                for c in cl[1:]:
                    head.paths = sorted(set(head.paths) | set(c.paths), key=_path_order)
                    head.labels = head.labels + [lab for lab in c.labels if lab not in head.labels]
                    head.flags |= c.flags - {"unlabelled"}
            out.append(head)
        if len(clusters) > 1:
            for cl in clusters:
                cl[0].flags.add("duplicate_label")
    kept_geoms = [c.geom for c in out]
    for c in sorted(rest, key=lambda c: (-c.geom.area, _order_key(c.geom))):
        if g.duplicate_of(c.geom, kept_geoms) is not None:
            qa["duplicates_removed"] += 1
            continue
        out.append(c)
        kept_geoms.append(c.geom)
    return out


def _dedupe_copies(layer: str, cands: list[_Candidate], qa: dict[str, Any]) -> list[_Candidate]:
    """The same area or line drawn on two sheets of one drawing is kept once: a polygon of the
    same code overlapping a kept one of an earlier sheet by 90 %, a line lying within 0.5 m of
    kept lines of earlier sheets for 90 % of its length. Sheets are taken in id order."""
    lines = layer == "planned_traffic"
    by_sheet: dict[str, list[_Candidate]] = defaultdict(list)
    for c in cands:
        by_sheet[c.sheet.rule.id].append(c)
    kept: list[_Candidate] = []
    for sid in sorted(by_sheet):
        tree = shapely.STRtree(np.asarray([k.geom for k in kept], dtype=object)) if kept else None
        new = []
        for c in by_sheet[sid]:
            others: list[_Candidate] = []
            if tree is not None:
                probe = c.geom.buffer(0.5) if lines else c.geom
                others = [kept[i] for i in tree.query(probe, predicate="intersects").tolist()]
            if lines and others:
                near = shapely.union_all(np.asarray([o.geom for o in others], dtype=object))
                duplicate = c.geom.difference(near.buffer(0.5)).length < 0.1 * c.geom.length
            else:
                same = [o.geom for o in others if o.attrs.get("code") == c.attrs.get("code")]
                duplicate = bool(same) and g.duplicate_of(c.geom, same) is not None
            if duplicate:
                qa["duplicates_removed"] += 1
            else:
                new.append(c)
        kept.extend(new)
    return kept


def _as_multi(geom: BaseGeometry, lines: bool) -> BaseGeometry:
    if lines:
        parts = g.line_parts(geom)
        return MultiLineString(parts)
    return MultiPolygon(g.polygon_parts(geom))


def _finish(ctx: _Context, layer: str, rule: LayerRule, cands: list[_Candidate]) -> list[Feature]:
    qa = ctx.counter(layer)
    for c in cands:
        _attribute_of(layer, c, ctx)
    cands = _merge(layer, cands, qa)
    if rule.keep == "labelled":
        dropped = [c for c in cands if "unlabelled" in c.flags]
        qa["unlabelled_dropped"] += len(dropped)
        cands = [c for c in cands if "unlabelled" not in c.flags]
    lines = layer in LINE_LAYERS
    attr = ATTRIBUTES.get(layer)
    feats: list[Feature] = []
    cands.sort(key=lambda c: (_natural(str(c.attrs.get(attr) or "")), _order_key(c.geom)))
    seen: dict[str, int] = defaultdict(int)
    unnamed = 0
    for c in cands:
        geom = _as_multi(c.geom, lines)
        if layer == "plan_boundary":
            key = "coverage"
        elif attr and c.attrs.get(attr) and layer in ("urban_parcels", "urban_blocks"):
            base = str(c.attrs[attr])
            seen[base] += 1
            key = base if seen[base] == 1 else f"{base}#{seen[base]}"
        elif layer == "planned_land_use" and c.attrs.get("urban_parcel_number"):
            key = f"UP {c.attrs['urban_parcel_number']}"
        else:
            unnamed += 1
            prefix = {
                "urban_parcels": "face",
                "urban_blocks": "block-face",
                "planned_land_use": "area",
                "planned_traffic": "line",
            }[layer]
            code = c.attrs.get("code") or c.attrs.get("road_class")
            key = f"{prefix}-{code}-{unnamed:04d}" if code else f"{prefix}-{unnamed:04d}"
        f = Feature(
            layer=layer,
            geom=geom,
            sheet=c.sheet.rule.id,
            page=c.sheet.rule.page,
            source_paths=list(c.paths),
            attrs={k: v for k, v in c.attrs.items() if k != "share" or layer == "planned_land_use"},
            labels=sorted(c.labels, key=lambda lab: (lab.value, lab.bbox)),
            qa_flags=set(c.flags),
            key=key,
            source_bbox=c.sheet.sheet_bbox(geom.bounds),
        )
        feats.append(f)
    return feats


def _blocks_by_containment(ctx: _Context) -> None:
    """Parcels whose block the rules do not give take the block they overlap most."""
    parcels = ctx.layers.get("urban_parcels") or []
    blocks = [b for b in ctx.layers.get("urban_blocks") or [] if b.attrs.get("block_ref")]
    if not parcels or not blocks:
        return
    tree = shapely.STRtree(np.asarray([b.geom for b in blocks], dtype=object))
    for f in parcels:
        if f.attrs.get("block_ref"):
            continue
        hits = tree.query(f.geom, predicate="intersects").tolist()
        overlaps = [(f.geom.intersection(blocks[i].geom).area, -i) for i in hits]
        overlaps = [o for o in overlaps if o[0] > 0.5 * f.geom.area]
        if overlaps:
            f.attrs["block_ref"] = blocks[-max(overlaps)[1]].attrs["block_ref"]


def _natural(s: str) -> tuple:
    return tuple((0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.findall(r"\d+|\D+", s))


def _layer_qa(
    layer: str, rule: LayerRule, feats: list[Feature], counter: dict[str, Any]
) -> dict[str, Any]:
    lines = layer in LINE_LAYERS
    flags = defaultdict(int)
    for f in feats:
        for fl in f.qa_flags:
            flags[fl] += 1
    invalid = sum(not f.geom.is_valid for f in feats)
    slivers = 0 if lines else sum(g.is_sliver(f.geom, rule.min_area_m2) for f in feats)
    out: dict[str, Any] = {
        "staged_as": STAGED_AS[layer],
        "method": rule.method,
        "features": len(feats),
        "labelled": sum(bool(f.labels) for f in feats),
        "unlabelled": flags["unlabelled"],
        "multi_label": flags["multi_label"],
        "flags": {k: flags[k] for k in QA_FLAGS if flags[k]},
        "invalid_fixed": int(counter.get("invalid_fixed", 0)),
        "slivers_removed": int(counter.get("slivers_removed", 0)),
        "small_removed": int(counter.get("small_removed", 0)),
        "duplicates_removed": int(counter.get("duplicates_removed", 0)),
        "invalid_after_cleanup": invalid,
        "slivers_after_cleanup": slivers,
    }
    if lines:
        out["length_m"] = round(sum(f.geom.length for f in feats), 1)
    else:
        out["area_m2"] = round(sum(f.geom.area for f in feats), 1)
    for k in (
        "faces_built",
        "outside_dropped",
        "too_large_dropped",
        "unlabelled_dropped",
        "labels_read",
        "labels_outside",
        "unclassified",
        "source_without_key",
        "short_removed",
    ):
        if counter.get(k):
            out[k] = int(counter[k])
    if counter.get("labels_unassigned"):
        out["labels_unassigned"] = counter["labels_unassigned"]
    if counter.get("label_notes"):
        out["label_notes"] = counter["label_notes"]
    return out


def _table_values(data: bytes, pattern: str) -> list[tuple[str, ...]]:
    doc = pymupdf.open(stream=data, filetype="pdf")
    text = "\n".join(page.get_text() for page in doc)
    rx = re.compile(pattern)
    out = []
    for m in rx.finditer(text):
        out.append(tuple(x for x in m.groups()))
    return out


def _digest(layers: dict[str, list[Feature]], rules: DocumentRules) -> str:
    h = hashlib.sha256()
    for layer in TARGET_LAYERS:
        for f in layers.get(layer, []):
            props = f.properties(None, rules.document.id)
            h.update(layer.encode())
            h.update(shapely.to_wkb(f.geom, byte_order=1, output_dimension=2))
            h.update(json.dumps(props, sort_keys=True, default=str).encode())
    return h.hexdigest()


# --- entry point --------------------------------------------------------------------------------


def _selectors(rules: DocumentRules, sheet_rule: SheetRule) -> list[Selector]:
    out: list[Selector] = []
    names = set(sheet_rule.layers)
    for name, rule in rules.layers.items():
        if name not in names and not (rule.method == "classify" and name in names):
            continue
        out += rule.select + rule.fallback
        if rule.labels is not None:
            out += rule.labels.select
        for cat in rule.categories:
            out += cat.select
    return out


def extract_document(
    rules: DocumentRules,
    load_pdf: SheetLoader,
    *,
    document_id: int | None = None,
    load_file: FileLoader | None = None,
) -> Extraction:
    """Run the rules on the document's sheets (see the module docstring)."""
    sheets: dict[str, Sheet] = {}
    for s in rules.sheets:
        sheets[s.id] = load_sheet(load_pdf(s), s, keep=_selectors(rules, s))
    ctx = _Context(rules=rules, sheets=sheets)
    tables: dict[str, Any] = {}
    if rules.parcel_blocks and rules.parcel_blocks.table and load_file is not None:
        t = rules.parcel_blocks.table
        data = load_file(t.pdf, t.sha256)
        if data is not None:
            pairs = _table_values(data, t.pattern)
            ctx.parcel_blocks = {p[1]: p[0] for p in pairs if len(p) >= 2}
            tables["parcel_blocks"] = len(ctx.parcel_blocks)
    for layer in TARGET_LAYERS:
        rule = rules.layers.get(layer)
        if rule is None:
            continue
        cands = BUILDERS[rule.method](ctx, layer, rule)
        if layer == "plan_boundary":
            if cands:
                geom = shapely.union_all(np.asarray([c.geom for c in cands], dtype=object))
                geom, _, _ = g.clean_polygon(geom, rule.min_area_m2)
                if geom is not None:
                    head = cands[0]
                    paths = sorted({p for c in cands for p in c.paths}, key=_path_order)
                    cands = [_Candidate(geom, head.sheet, paths)]
                    ctx.boundary = shapely.make_valid(geom)
                else:
                    cands = []
        ctx.layers[layer] = _finish(ctx, layer, rule, cands)
    _blocks_by_containment(ctx)
    qa: dict[str, Any] = {
        "extractor_version": EXTRACTOR_VERSION,
        "document": rules.document.model_dump(),
        "document_id": document_id,
        "sheets": {},
        "layers": {},
    }
    for layer in TARGET_LAYERS:
        if layer in ctx.layers:
            qa["layers"][layer] = _layer_qa(
                layer, rules.layers[layer], ctx.layers[layer], ctx.counter(layer)
            )
    for sid, sheet in sheets.items():
        feats = [f for fs in ctx.layers.values() for f in fs if f.sheet == sid]
        rule_of = {f.layer: rules.layers[f.layer] for f in feats}
        qa["sheets"][sid] = {
            "file": sheet.rule.file,
            "page": sheet.rule.page,
            "scale": sheet.rule.scale,
            "offset_m": list(sheet.rule.offset_m),
            "features": {
                layer: sum(f.layer == layer for f in feats)
                for layer in TARGET_LAYERS
                if any(f.layer == layer for f in feats)
            },
            "invalid_after_cleanup": sum(not f.geom.is_valid for f in feats),
            "slivers_after_cleanup": sum(
                f.layer not in LINE_LAYERS and g.is_sliver(f.geom, rule_of[f.layer].min_area_m2)
                for f in feats
            ),
        }
    parcels = ctx.layers.get("urban_parcels")
    if parcels is not None and rules.expected_parcels and load_file is not None:
        t = rules.expected_parcels
        data = load_file(t.pdf, t.sha256)
        if data is not None:
            expected = {v[0] for v in _table_values(data, t.pattern) if v and v[0]}
            got = {
                str(f.attrs.get("urban_parcel_number"))
                for f in parcels
                if f.attrs.get("urban_parcel_number")
            }
            qa["expected_parcels"] = {
                "expected": len(expected),
                "found": len(expected & got),
                "missing": sorted(expected - got, key=_natural),
                "unexpected": sorted(got - expected, key=_natural),
            }
    if tables:
        qa["tables"] = tables
    qa["totals"] = {
        "features": sum(len(fs) for fs in ctx.layers.values()),
        "invalid_after_cleanup": sum(q["invalid_after_cleanup"] for q in qa["layers"].values()),
        "slivers_after_cleanup": sum(q["slivers_after_cleanup"] for q in qa["layers"].values()),
    }
    qa["digest"] = _digest(ctx.layers, rules)
    return Extraction(
        rules=rules, document_id=document_id, layers=ctx.layers, qa=_plain(qa), sheets=sheets
    )


def _plain(obj: Any) -> Any:
    """defaultdicts and tuples as plain JSON types."""
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_plain(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 3)
    return obj


__all__ = [
    "ATTRIBUTES",
    "EXTRACTOR_VERSION",
    "QA_FLAGS",
    "Extraction",
    "Feature",
    "Label",
    "assign_labels",
    "extract_document",
    "read_labels",
]
