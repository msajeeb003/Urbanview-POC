"""Command line of the georeferencing (run from ``backend/``). RULES is a document's extraction
rules file (``municipalities/<id>/extraction/<doc>.yaml``); its control points live next to it in
``<doc>.points.csv`` and the fitted transform in ``<doc>.transform.json``.

  python -m core.gis.georef points RULES
      the control points, with their residuals when a transform is stored
  python -m core.gis.georef add RULES --sheet ID --x-pt X --y-pt Y --easting E --northing N
      [--id ID] [--source grid|label|table|cadastre|corner|manual] [--note TEXT]
      add (or replace) one control point: a coordinate label, a vertex-table point, a cadastral
      corner, a sheet corner of known coordinates
  python -m core.gis.georef disable|enable RULES ID [ID ...]
      leave points out of the fit (never deleted)
  python -m core.gis.georef grid RULES --source DIR --sheet ID --seed X_PT Y_PT E N
      [--interval 100] [--layer REGEX] [--write]
      control points from the sheet's grid crosses and one seed coordinate (within half an
      interval of the truth); --write adds them to the CSV
  python -m core.gis.georef fit RULES [--method helmert|affine] [--max-rmse M] [--crs EPSG:N]
      fit the transform, report every residual and the RMSE per sheet; stored only when the RMSE
      is under the threshold (exit 1 otherwise)
  python -m core.gis.georef apply RULES --gpkg EXTRACTED.gpkg --out DIR [--frame local|sheet:ID]
      [--transform FILE] [--ct OP] [--document-id N] [--stage] [--redrawn] [--label L]
      [--store] [--by NAME]
      the stored transform on every layer, GDAL to EPSG:4326: DIR/<doc>.georef.gpkg and a report;
      --stage snaps to the cadastral base, validates and stages the dataset for the publish job
  python -m core.gis.georef datasets [--document-id N]
  python -m core.gis.georef show VERSION [--transform-out FILE]
      a dataset's transform, residuals, snapping and validation (and its transform as a file,
      to re-apply it with ``apply --transform``)

Exit codes: 0 done, 1 fit above the threshold or dataset refused by the validation, 2 input,
configuration or GDAL error.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from core.cadastre.ogr import OgrError
from core.config import get_settings
from core.gis.extract.rules import DocumentRules, load_rules
from core.gis.georef.fit import FitError, FitResult, fit, read_transform, write_transform
from core.gis.georef.points import (
    SOURCES,
    ControlPoint,
    add_points,
    points_hash,
    points_path,
    read_points,
    set_enabled,
    transform_path,
    write_points,
)


def _engine():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(get_settings().database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _municipality(args: argparse.Namespace) -> str:
    return args.municipality or get_settings().municipality_id


def _rules(args: argparse.Namespace) -> tuple[Path, DocumentRules, Path]:
    path = Path(args.rules)
    rules = load_rules(path)
    return path, rules, points_path(path, rules.georef.points)


def _crs(rules: DocumentRules, municipality_id: str, override: str | None = None) -> str:
    if override or rules.georef.crs:
        return str(override or rules.georef.crs)
    from core.municipality import load_profile

    epsg = load_profile(municipality_id).source_crs_epsg
    if not epsg:
        raise ValueError("no CRS: set georef.crs in the rules or source_crs_epsg in the profile")
    return f"EPSG:{epsg}"


def _print_fit(result: FitResult) -> None:
    t = result.transform
    print(
        f"{t.method} fit to {t.crs}: {result.points_used} points, RMSE {result.rmse_m:.3f} m, "
        f"max residual {result.max_residual_m:.3f} m, scale {t.scale:.6f}, "
        f"rotation {t.rotation_deg:+.4f} deg (threshold {result.max_rmse_m} m)"
    )
    for s in result.sheets:
        rmse = "-" if s["rmse_m"] is None else f"{s['rmse_m']:.3f} m"
        print(f"  sheet {s['sheet']:14} page {s['page']:>2}  {s['points']:>3} points  RMSE {rmse}")
    for r in result.residuals:
        mark = "  <- outlier" if r.outlier else ""
        print(
            f"  {r.id:16} {r.sheet:14} dx {r.dx_m:+8.3f}  dy {r.dy_m:+8.3f}  r {r.r_m:7.3f}{mark}"
        )
    for w in result.warnings:
        print(f"  ! {w}")


# --- points -----------------------------------------------------------------------------------


def cmd_points(args: argparse.Namespace) -> int:
    rules_path, _, path = _rules(args)
    points = read_points(path)
    residuals: dict[str, dict[str, Any]] = {}
    stored = transform_path(rules_path)
    if stored.is_file():
        _, data = read_transform(stored)
        residuals = {r["id"]: r for r in data.get("residuals", [])}
        state = "current" if data.get("points_sha256") == points_hash(points) else "STALE"
        print(f"transform {stored.name}: RMSE {data['rmse_m']} m ({state})")
    print(f"{path} ({len(points)} points, {sum(p.enabled for p in points)} enabled)")
    for p in points:
        r = residuals.get(p.id)
        res = f"  r {r['r_m']:.3f} m" if r else ""
        off = "" if p.enabled else "  [disabled]"
        print(
            f"  {p.id:16} {p.sheet:14} x {p.x_pt:10.2f} y {p.y_pt:10.2f}  "
            f"E {p.easting:14.3f} N {p.northing:14.3f}  {p.source:8}{res}{off}  {p.note}"
        )
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    _, rules, path = _rules(args)
    if args.sheet not in {s.id for s in rules.sheets}:
        raise ValueError(f"no sheet {args.sheet!r} in the rules")
    points = read_points(path)
    pid = args.id or f"{args.sheet}-{args.source[0]}{len(points) + 1:03d}"
    point = ControlPoint(
        id=pid,
        sheet=args.sheet,
        x_pt=args.x_pt,
        y_pt=args.y_pt,
        easting=args.easting,
        northing=args.northing,
        source=args.source,
        note=args.note or "",
    )
    write_points(path, add_points(points, [point]))
    print(f"{pid} added to {path}")
    return 0


def cmd_enable(args: argparse.Namespace, enabled: bool) -> int:
    _, _, path = _rules(args)
    write_points(path, set_enabled(read_points(path), set(args.ids), enabled))
    print(f"{'enabled' if enabled else 'disabled'} {', '.join(args.ids)} in {path}")
    return 0


def cmd_grid(args: argparse.Namespace) -> int:
    from core.gis.extract.rules import Selector
    from core.gis.extract.sheet import load_sheet
    from core.gis.georef.grid import suggest_grid_points

    _, rules, path = _rules(args)
    rule = next((s for s in rules.sheets if s.id == args.sheet), None)
    if rule is None:
        raise ValueError(f"no sheet {args.sheet!r} in the rules")
    layer = args.layer or rules.georef.grid_layer_regex
    sheet = load_sheet(
        (Path(args.source) / rule.file).read_bytes(), rule, keep=[Selector(layer_regex=layer)]
    )
    x, y, e, n = args.seed
    suggestion = suggest_grid_points(
        sheet, rule, seed=(x, y, e, n), interval_m=args.interval, layer_regex=layer
    )
    print(
        f"{suggestion.crosses} crosses on {rule.id}, lattice angle {suggestion.angle_deg:+.3f} deg,"
        f" seed {suggestion.seed_offset_m[0]:+.2f} / {suggestion.seed_offset_m[1]:+.2f} m from "
        f"its grid node, {len(suggestion.points)} control points"
    )
    for w in suggestion.warnings:
        print(f"  ! {w}")
    for p in suggestion.points[:10]:
        print(f"  {p.id} x {p.x_pt:.2f} y {p.y_pt:.2f} -> E {p.easting:.0f} N {p.northing:.0f}")
    if len(suggestion.points) > 10:
        print(f"  ... {len(suggestion.points) - 10} more")
    if args.write and suggestion.points:
        write_points(path, add_points(read_points(path), suggestion.points))
        print(f"written to {path}")
    return 0 if suggestion.points else 1


def cmd_fit(args: argparse.Namespace) -> int:
    rules_path, rules, path = _rules(args)
    points = read_points(path)
    if not points:
        raise FitError(f"no control points in {path}: add some first (add / grid)")
    result = fit(
        points,
        rules,
        crs=_crs(rules, _municipality(args), args.crs),
        method=args.method,
        max_rmse_m=args.max_rmse,
    )
    _print_fit(result)
    if not result.ok:
        print("rejected: the transform was not stored")
        return 1
    target = transform_path(rules_path)
    write_transform(target, result, rules.document.id)
    print(f"stored {target}")
    return 0


# --- apply ------------------------------------------------------------------------------------


def _store(path: Path, m: str, label: str) -> str:
    from core.storage import ObjectStorage

    key = ObjectStorage.object_key(m, "georef", f"{label}/{path.name}")
    return ObjectStorage(get_settings()).put_file(key, path)


def _load_transform(args: argparse.Namespace, rules_path: Path, rules: DocumentRules, points):
    stored = Path(args.transform) if args.transform else transform_path(rules_path)
    transform, data = read_transform(stored)
    if data.get("points_sha256") != points_hash(points):
        if not args.transform:
            raise FitError(
                "the control points changed since the stored fit: run `fit` again, or apply a "
                "stored transform explicitly with --transform"
            )
        print(f"note: {stored.name} was fitted on other control points than the CSV holds now")
    if float(data["rmse_m"]) > rules.georef.max_rmse_m:
        raise FitError(
            f"the stored transform's RMSE {data['rmse_m']} m is above the document's threshold "
            f"{rules.georef.max_rmse_m} m"
        )
    return transform, data


async def _stage(
    args: argparse.Namespace,
    rules: DocumentRules,
    transform,
    data: dict[str, Any],
    out: Path,
    work: Path,
    ct: str | None,
) -> int:
    from core.cadastre.config import load_cadastre_profile
    from core.cadastre.ogr import Ogr
    from core.gis.georef.apply import digest, georeference
    from core.gis.georef.stage import next_label, stage_document
    from core.municipality import load_profile

    m = _municipality(args)
    profile = load_profile(m)
    doc = rules.document.id
    engine, factory = _engine()
    try:
        async with factory() as session:
            label = args.label or await next_label(session, m, args.document_id, date.today())
            target = out / f"{doc}.georef.gpkg"
            features = georeference(
                Path(args.gpkg),
                rules,
                transform,
                target,
                work,
                frame=args.frame,
                ct=ct,
                ogr=Ogr(),
                attributes={"document_id": args.document_id, "dataset_version": label},
            )
            sha = digest(features)
            gpkg_key = await asyncio.to_thread(_store, target, m, label) if args.store else None
            outcome = await stage_document(
                session,
                municipality_id=m,
                document_id=args.document_id,
                label=label,
                features=features,
                transform=data,
                source="manual_redraw" if args.redrawn else "extraction",
                bounds=profile.bounds,
                snap_tolerance_m=rules.georef.snap_tolerance_m,
                metric_srid=load_cadastre_profile(m).area_crs_epsg,
                parcel_prefix=profile.terminology.urban_parcel.abbreviation,
                output_sha256=sha,
                gpkg_key=gpkg_key,
                imported_by=args.by or getpass.getuser(),
                log_csv=out / f"{doc}.snap-log.csv",
            )
            await session.commit()
    finally:
        await engine.dispose()
    report = {
        "document": doc,
        "document_id": args.document_id,
        "dataset_version": outcome.dataset_version,
        "status": outcome.status,
        "gpkg": str(target),
        "gpkg_key": gpkg_key,
        "output_sha256": sha,
        "transform": {k: data.get(k) for k in ("crs", "method", "rmse_m", "max_residual_m")},
        "sheets": [
            {k: s.get(k) for k in ("sheet", "page", "points", "rmse_m")}
            for s in data.get("sheets", [])
        ],
        "snap": outcome.snap,
        "overlap": outcome.overlap,
        "errors": [f.as_json() for f in outcome.errors],
        "warnings": [f.as_json() for f in outcome.warnings],
        "features": outcome.counts,
        "batches": outcome.batches,
        "superseded": outcome.superseded,
    }
    (out / f"{doc}.georef.json").write_bytes(
        (json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    )
    s = outcome.snap
    print(
        f"{outcome.dataset_version}: {outcome.status}; snapped {s['snapped_vertices']} of "
        f"{s['vertices']} vertices ({s['snapped_ratio']:.1%}), {s['near_misses']} near misses "
        f"(log {out / f'{doc}.snap-log.csv'})"
    )
    if s.get("offset_samples"):
        dx, dy = s["offset_vector_m"]
        print(
            f"mean offset to the cadastre {s['systematic_offset_m']} m ({dx:+} E, {dy:+} N) over "
            f"{s['offset_samples']} vertices near it"
        )
    o = outcome.overlap
    print(
        f"planned parcels {o['planned_parcels']}, cadastral parcels around them "
        f"{o['cadastral_parcels_in_extent']}, overlap {o['overlap_m2']} m2"
    )
    for f in outcome.errors:
        print(f"  error {f.code}: {f.message} ({f.count})")
    for f in outcome.warnings:
        print(f"  warning {f.code}: {f.message} ({f.count})")
    if outcome.batches:
        print(f"staged batches {outcome.batches}: run the publish job to serve them")
    return 0 if outcome.status == "staged" else 1


def cmd_apply(args: argparse.Namespace) -> int:
    from core.cadastre.ogr import Ogr
    from core.gis.georef.apply import digest, georeference

    rules_path, rules, path = _rules(args)
    transform, data = _load_transform(args, rules_path, rules, read_points(path))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="georef-"))
    ct = args.ct or rules.georef.transform
    if args.stage:
        if args.document_id is None:
            raise ValueError("--stage needs --document-id (the registered planning document)")
        return asyncio.run(_stage(args, rules, transform, data, out, work, ct))
    target = out / f"{rules.document.id}.georef.gpkg"
    attributes = {"document_id": args.document_id} if args.document_id is not None else None
    features = georeference(
        Path(args.gpkg),
        rules,
        transform,
        target,
        work,
        frame=args.frame,
        ct=ct,
        ogr=Ogr(),
        attributes=attributes,
    )
    counts = {layer: len(rows) for layer, rows in features.items()}
    print(f"{target} (EPSG:4326) {counts}  digest {digest(features)[:16]}")
    return 0


# --- datasets / show --------------------------------------------------------------------------


async def _datasets(args: argparse.Namespace) -> int:
    from sqlalchemy import text

    sql = (
        "SELECT g.dataset_version, g.status, g.document_id, d.name, g.crs, g.method, g.rmse_m, "
        "g.points_used, g.snap, g.created_at FROM georef_datasets g "
        "JOIN planning_documents d ON d.id = g.document_id WHERE g.municipality_id = :m"
    )
    params: dict[str, Any] = {"m": _municipality(args)}
    if args.document_id is not None:
        sql += " AND g.document_id = :d"
        params["d"] = args.document_id
    engine, factory = _engine()
    try:
        async with factory() as session:
            rows = (await session.execute(text(sql + " ORDER BY g.id"), params)).mappings().all()
    finally:
        await engine.dispose()
    if not rows:
        print("no georeferencing dataset yet")
    for r in rows:
        snap = r["snap"] or {}
        print(
            f"{r['dataset_version']:24} {r['status']:10} doc {r['document_id']:>4} "
            f"{r['crs']:10} {r['method']:7} RMSE {r['rmse_m']:.3f} m  {r['points_used']:>3} pts  "
            f"snapped {snap.get('snapped_ratio', 0):.1%}  {r['created_at']:%Y-%m-%d}  {r['name']}"
        )
    return 0


async def _show(args: argparse.Namespace) -> int:
    from sqlalchemy import text

    engine, factory = _engine()
    try:
        async with factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM georef_datasets "
                            "WHERE municipality_id = :m AND dataset_version = :v"
                        ),
                        {"m": _municipality(args), "v": args.version},
                    )
                )
                .mappings()
                .first()
            )
    finally:
        await engine.dispose()
    if row is None:
        print(f"no georeferencing dataset {args.version}")
        return 1
    t = row["transform"]
    print(
        f"{row['dataset_version']} ({row['status']}, {row['source']}): document "
        f"{row['document_id']}, {row['method']} to {row['crs']}, RMSE {row['rmse_m']:.3f} m, "
        f"max residual {row['max_residual_m']:.3f} m, {row['points_used']} points"
    )
    for s in row["sheets"] or []:
        rmse = "-" if s.get("rmse_m") is None else f"{s['rmse_m']:.3f} m"
        print(f"  sheet {s['sheet']:14} page {s['page']:>2}  {s['points']:>3} points  RMSE {rmse}")
    for r in t.get("residuals", []):
        flag = "  outlier" if r["outlier"] else ""
        print(f"  {r['id']:16} {r['sheet']:14} r {r['r_m']:7.3f}{flag}")
    print(f"snap: {json.dumps(row['snap'], ensure_ascii=False, default=str)}")
    print(f"validation: {json.dumps(row['validation'], ensure_ascii=False, default=str)[:3000]}")
    print(f"batches: {row['batches']}  output sha256 {row['output_sha256']}")
    if args.transform_out:
        target = Path(args.transform_out)
        target.write_bytes((json.dumps(t, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        print(f"transform written to {target}: `apply --transform {target}` re-applies it")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.gis.georef", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--municipality")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("points", help="list the control points")
    p.add_argument("rules")
    p.set_defaults(fn=cmd_points)
    p = sub.add_parser("add", help="add or replace a control point")
    p.add_argument("rules")
    p.add_argument("--sheet", required=True)
    p.add_argument("--x-pt", type=float, required=True)
    p.add_argument("--y-pt", type=float, required=True)
    p.add_argument("--easting", type=float, required=True)
    p.add_argument("--northing", type=float, required=True)
    p.add_argument("--id")
    p.add_argument("--source", choices=SOURCES, default="manual")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_add)
    for name, enabled in (("disable", False), ("enable", True)):
        p = sub.add_parser(name, help=f"{name} control points")
        p.add_argument("rules")
        p.add_argument("ids", nargs="+")
        p.set_defaults(fn=lambda a, e=enabled: cmd_enable(a, e))
    p = sub.add_parser("grid", help="control points from the grid crosses and one seed")
    p.add_argument("rules")
    p.add_argument("--source", required=True, help="the folder the rules' sheet paths start in")
    p.add_argument("--sheet", required=True)
    p.add_argument("--seed", nargs=4, type=float, required=True, metavar=("X_PT", "Y_PT", "E", "N"))
    p.add_argument("--interval", type=float, default=100.0)
    p.add_argument("--layer", help="regex of the grid layer (default: georef.grid_layer_regex)")
    p.add_argument("--write", action="store_true")
    p.set_defaults(fn=cmd_grid)
    p = sub.add_parser("fit", help="fit and store the transform")
    p.add_argument("rules")
    p.add_argument("--method", choices=("helmert", "affine"))
    p.add_argument("--max-rmse", type=float)
    p.add_argument("--crs")
    p.set_defaults(fn=cmd_fit)
    p = sub.add_parser("apply", help="georeference the extracted layers (and stage them)")
    p.add_argument("rules")
    p.add_argument("--gpkg", required=True, help="the extraction's or the redrawn GeoPackage")
    p.add_argument("--out", required=True)
    p.add_argument("--frame", default="local", help="local (extraction) or sheet:<id> (redrawn)")
    p.add_argument("--transform", help="a stored transform file (default <doc>.transform.json)")
    p.add_argument("--ct", help="ogr2ogr -ct operation plan CRS -> EPSG:4326")
    p.add_argument("--document-id", type=int)
    p.add_argument("--stage", action="store_true", help="snap, validate and stage in PostGIS")
    p.add_argument("--redrawn", action="store_true", help="the layers were redrawn by hand")
    p.add_argument("--label")
    p.add_argument("--work")
    p.add_argument("--store", action="store_true", help="keep the GeoPackage in the bucket")
    p.add_argument("--by")
    p.set_defaults(fn=cmd_apply)
    p = sub.add_parser("datasets", help="the georeferencing datasets")
    p.add_argument("--document-id", type=int)
    p.set_defaults(fn=lambda a: asyncio.run(_datasets(a)))
    p = sub.add_parser("show", help="a dataset's transform, residuals and validation")
    p.add_argument("version")
    p.add_argument("--transform-out")
    p.set_defaults(fn=lambda a: asyncio.run(_show(a)))
    args = parser.parse_args(argv)
    try:
        return int(args.fn(args))
    except (FitError, OgrError, ValueError, LookupError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
