"""Command line for the vector PDF extraction (run from ``backend/``).

python -m core.gis.extract styles PDF [--page N] [--layer REGEX] [--assessment layers.csv]
    the style clusters of a sheet (layer, colours, width, dash, counts, sizes, dot spacing,
    text samples, the assessment's layer type): what a rules file selects from
python -m core.gis.extract offset RULES --source DIR --base SHEET --sheet SHEET [--layer L]
    the translation that puts SHEET on BASE (vertex voting on common linework), for the
    sheet's ``offset_m`` in the rules file
python -m core.gis.extract run RULES --source DIR --out DIR [--document-id N] [--overlay]
    [--preview] [--dpi N]
    extract: ``<doc>.gpkg`` + ``<doc>.qa.json``; ``--overlay`` adds each sheet as PNG + world
    file in the same local frame (load both in QGIS), ``--preview`` the features drawn on it
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import shapely

from core.gis.extract.extract import extract_document
from core.gis.extract.geometry import estimate_offset
from core.gis.extract.gpkg import feature_rows, write_gpkg
from core.gis.extract.overlay import render_preview, render_sheet
from core.gis.extract.rules import Selector, SheetRule, load_rules
from core.gis.extract.sheet import Sheet, load_sheet, style_clusters, subpaths
from core.gis.inspect_pdf import MM_PER_PT, fold, layer_key

SMALL_PIECE_MM = 3.0


def _spacing_mm(sheet: Sheet) -> dict[tuple, float]:
    """Median distance between neighbouring small pieces of each style cluster (dots, dashes)."""
    centres: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
    for p in sheet.paths:
        if p.size_mm <= SMALL_PIECE_MM:
            x0, y0, x1, y1 = p.rect
            centres[p.style_key()].append(((x0 + x1) / 2, (y0 + y1) / 2))
    out = {}
    for key, pts in centres.items():
        if len(pts) < 10:
            continue
        geoms = shapely.points(np.array(pts))
        tree = shapely.STRtree(geoms)
        step = max(1, len(pts) // 2000)
        gaps = []
        for g in geoms[::step]:
            idx = tree.query_nearest(g, exclusive=True, all_matches=False)
            if len(idx):
                gaps.append(g.distance(geoms[int(idx[0])]))
        gaps.sort()
        if gaps:
            out[key] = round(gaps[len(gaps) // 2] * MM_PER_PT, 2)
    return out


def _assessed(assessment: Path | None, pdf: Path) -> dict[str, str]:
    if assessment is None or not assessment.exists():
        return {}
    types = {}
    with assessment.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if Path(row["file"]).name == pdf.name and row.get("layer_type"):
                types[row["layer"]] = row["layer_type"]
    return types


def cmd_styles(args: argparse.Namespace) -> int:
    pdf = Path(args.pdf)
    rule = SheetRule(id="sheet", file=pdf.name, page=args.page, scale=args.scale or 1000, layers=[])
    keep = [Selector(layer_regex=args.layer)] if args.layer else None
    sheet = load_sheet(pdf.read_bytes(), rule, keep=keep)
    rows = style_clusters(sheet)
    spacing = _spacing_mm(sheet)
    types = _assessed(Path(args.assessment) if args.assessment else None, pdf)
    rx = re.compile(args.layer, re.IGNORECASE) if args.layer else None
    print(
        f"{'layer':44} {'stroke':8} {'fill':8} {'width':>6} {'dash':6} {'paths':>6} "
        f"{'segs':>7} {'med mm':>7} {'max mm':>7} {'gap mm':>6}  assessed / texts"
    )
    for r in rows:
        if rx and not rx.search(fold(layer_key(r["layer"]))):
            continue
        if r["paths"] < args.min_paths and r["dash"] != "text":
            continue
        key = (r["layer"], r["stroke"], r["fill"], r["width"], r["dash"])
        gap = spacing.get(key)
        texts = " | ".join(r["texts"][:4])
        note = " ".join(x for x in (types.get(r["layer"], ""), texts) if x)
        print(
            f"{r['layer'][:44]:44} {r['stroke']:8} {r['fill']:8} {r['width']:>6} {r['dash']:6} "
            f"{r['paths']:>6} {r['segments']:>7} {r['median_size_mm']:>7} {r['max_size_mm']:>7} "
            f"{'' if gap is None else gap:>6}  {note[:90]}"
        )
    return 0


def _vertices(sheet: Sheet, selectors: list[Selector]) -> np.ndarray:
    pts: list[tuple[float, float]] = []
    for p in sheet.select(selectors):
        for sub in subpaths(p.items):
            pts.extend(sub)
    if not pts:
        return np.zeros((0, 2))
    a = np.array(pts, dtype=float)
    k = sheet.metres_per_pt
    ox, oy = sheet.rule.offset_m
    local = np.column_stack((a[:, 0] * k + ox, (sheet.height_pt - a[:, 1]) * k + oy))
    return np.unique(np.round(local, 3), axis=0)


def cmd_offset(args: argparse.Namespace) -> int:
    rules = load_rules(Path(args.rules))
    source = Path(args.source)
    by_id = {s.id: s for s in rules.sheets}
    base_rule, moving = by_id[args.base], by_id[args.sheet]
    moving = moving.model_copy(update={"offset_m": (0.0, 0.0)})
    if args.pdf_layer:
        selectors = [Selector(layer_regex=args.pdf_layer)]
    else:
        rule = rules.layers[args.layer]
        selectors = [*rule.select, *rule.fallback]
    a = _vertices(
        load_sheet((source / base_rule.file).read_bytes(), base_rule, keep=selectors), selectors
    )
    b = _vertices(
        load_sheet((source / moving.file).read_bytes(), moving, keep=selectors), selectors
    )
    dx, dy, votes, share = estimate_offset(a, b)
    print(f"vertices: {args.base} {len(a)}, {args.sheet} {len(b)}")
    print(f"offset_m for {args.sheet}: [{dx}, {dy}]  (votes {votes}, share {share})")
    return 0 if votes else 1


def cmd_run(args: argparse.Namespace) -> int:
    rules_path = Path(args.rules)
    rules = load_rules(rules_path)
    source = Path(args.source)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    def load_pdf(s: SheetRule) -> bytes:
        return (source / s.file).read_bytes()

    def load_file(path: str, sha256: str | None) -> bytes | None:
        f = source / path
        return f.read_bytes() if f.exists() else None

    ex = extract_document(rules, load_pdf, document_id=args.document_id, load_file=load_file)
    doc = rules.document.id
    counts = write_gpkg(out / f"{doc}.gpkg", feature_rows(ex), description=rules.document.name)
    (out / f"{doc}.qa.json").write_text(
        json.dumps(ex.qa, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    for layer, q in ex.qa["layers"].items():
        extra = (
            f"area {q['area_m2']:.0f} m2"
            if "area_m2" in q
            else f"length {q.get('length_m', 0):.0f} m"
        )
        print(
            f"{layer:18} {q['features']:>5} features  labelled {q['labelled']:>4}  "
            f"unlabelled {q['unlabelled']:>3}  multi {q['multi_label']:>2}  {extra}  "
            f"invalid {q['invalid_after_cleanup']}  slivers {q['slivers_after_cleanup']}"
        )
    if "expected_parcels" in ex.qa:
        e = ex.qa["expected_parcels"]
        print(
            f"expected parcels: {e['found']}/{e['expected']}  "
            f"missing {e['missing'][:20]}  unexpected {e['unexpected'][:20]}"
        )
    print(f"gpkg {out / f'{doc}.gpkg'} {counts}  digest {ex.qa['digest'][:16]}")
    if args.overlay or args.preview:
        boundary = ex.layers.get("plan_boundary") or []
        for sid, sheet in ex.sheets.items():
            feats = [
                f
                for fs in ex.layers.values()
                for f in fs
                if f.sheet == sid or f.layer == "plan_boundary"
            ]
            geoms = [f.geom for f in (boundary or feats)]
            bounds = shapely.union_all(geoms).bounds if geoms else None
            pdf = load_pdf(sheet.rule)
            if args.overlay:
                png = render_sheet(
                    pdf, sheet, out / f"{doc}-{sid}.png", dpi=args.dpi, local_bounds=bounds
                )
                print(f"overlay {png} (+ .pgw)")
            if args.preview:
                png = render_preview(
                    pdf,
                    sheet,
                    feats,
                    out / f"{doc}-{sid}-preview.png",
                    dpi=args.preview_dpi,
                    local_bounds=bounds,
                )
                print(f"preview {png}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.gis.extract", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("styles", help="style clusters of a sheet")
    p.add_argument("pdf")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--scale", type=float)
    p.add_argument("--layer", help="regex on the folded PDF layer name")
    p.add_argument("--assessment", help="docs/gis/assessment/layers.csv")
    p.add_argument("--min-paths", type=int, default=1)
    p.set_defaults(fn=cmd_styles)
    p = sub.add_parser("offset", help="estimate a sheet's offset against another sheet")
    p.add_argument("rules")
    p.add_argument("--source", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--sheet", required=True)
    p.add_argument("--layer", default="urban_parcels")
    p.add_argument(
        "--pdf-layer", help="use this PDF layer regex instead of the target layer's rules"
    )
    p.set_defaults(fn=cmd_offset)
    p = sub.add_parser("run", help="extract a document")
    p.add_argument("rules")
    p.add_argument("--source", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--document-id", type=int)
    p.add_argument("--overlay", action="store_true")
    p.add_argument("--preview", action="store_true")
    p.add_argument("--dpi", type=int, default=100)
    p.add_argument("--preview-dpi", type=int, default=72)
    p.set_defaults(fn=cmd_run)
    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
