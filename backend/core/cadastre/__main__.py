"""Command line of the cadastral base loader (run from ``backend/``).

  python -m core.cadastre sources
      the configured sources with their access status, basis and licence
  python -m core.cadastre check-access [--source ID ...]
      stops with the reason when bulk access to a source is not confirmed (exit 2)
  python -m core.cadastre import [--source ID] (--file EXPORT | [WFS per profile])
      [--layer NAME] [--ko-layer NAME] [--source-crs CRS] [--transform OP]
      [--ownership EXPORT] [--retrieved-on YYYY-MM-DD] [--label L] [--out DIR]
      [--accept-large-change] [--store] [--dry-run] [--by NAME]
      ogr2ogr the export, validate, stage a versioned dataset with its diff; the publish job
      applies it. Reports: report.md, report.json, diff.csv in --out
  python -m core.cadastre datasets
      the imported versions with status, source, counts and checksum
  python -m core.cadastre show VERSION [--out DIR]
      a dataset's validation and diff summary (and its report files with --out)

Exit codes: 0 staged (or a valid dry run), 1 refused by the validation, 2 access not confirmed,
configuration or GDAL error.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
import tempfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from core.cadastre.adapters import (
    AccessNotConfirmed,
    SourceError,
    SourceRecord,
    adapter_for,
)
from core.cadastre.config import CadastreProfile, SourceConfig, load_cadastre_profile
from core.cadastre.normalise import canonical_ko, clean, map_ownership, map_parcel
from core.cadastre.ogr import Ogr, OgrError, OgrLayer, dataset_name, read_features
from core.cadastre.report import write_report
from core.config import get_settings
from core.municipality import load_profile

REPO_ROOT = Path(__file__).resolve().parents[3]


def _engine():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(get_settings().database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _municipality(args: argparse.Namespace) -> str:
    return args.municipality or get_settings().municipality_id


# --- sources / check-access -------------------------------------------------------------------


def cmd_sources(args: argparse.Namespace) -> int:
    m = _municipality(args)
    profile = load_cadastre_profile(m)
    for sid, src in profile.sources.items():
        default = " (default)" if sid in (profile.source, profile.ownership_source) else ""
        print(f"{sid}{default}: {src.name} [{src.kind}] {src.url or ''}")
        print(
            f"  access: {src.access}; methods: {', '.join(src.methods)}; import uses {src.method}"
        )
        print(f"  basis: {src.access_basis or '-'}")
        print(f"  licence: {src.licence_note or '-'}")
    return 0


def cmd_check_access(args: argparse.Namespace) -> int:
    m = _municipality(args)
    profile = load_cadastre_profile(m)
    ids = args.source or [s for s in (profile.source, profile.ownership_source) if s]
    refused = 0
    for sid in ids:
        try:
            adapter_for(profile, sid, m).check_access()
            print(f"{sid}: bulk access confirmed ({profile.sources[sid].access_basis})")
        except AccessNotConfirmed as exc:
            print(f"{sid}: {exc}")
            refused += 1
        except SourceError as exc:
            print(f"{sid}: {exc}")
            refused += 1
    return 2 if refused else 0


# --- import -----------------------------------------------------------------------------------


def _pick_layer(layers: list[OgrLayer], wanted: str | None, what: str) -> OgrLayer:
    if wanted:
        for layer in layers:
            if layer.name == wanted:
                return layer
        names = ", ".join(layer.name for layer in layers)
        raise SourceError(f"the export has no {what} layer {wanted!r} (layers: {names})")
    polygons = [la for la in layers if la.geometry_type and "polygon" in la.geometry_type.lower()]
    if len(polygons) == 1:
        return polygons[0]
    if len(layers) == 1:
        return layers[0]
    names = ", ".join(f"{la.name} ({la.geometry_type})" for la in layers)
    raise SourceError(f"the export has several layers, name the {what} layer ({names})")


def _crs(
    layer: OgrLayer, forced: str | None, configured: str | None, warnings: list[dict[str, Any]]
) -> tuple[str | None, str | None]:
    """(CRS recorded, -s_srs to pass): the export's own CRS unless forced; the profile's when
    the export has none."""
    if forced:
        return forced, forced
    if layer.crs:
        if configured and configured.upper() != layer.crs.upper():
            warnings.append(
                {
                    "code": "crs_mismatch",
                    "message": f"the export declares {layer.crs}, the profile says {configured}: "
                    "the export's CRS was used (--source-crs forces another)",
                    "count": 1,
                }
            )
        return layer.crs, None
    if configured:
        return configured, configured
    raise SourceError(
        f"layer {layer.name!r} carries no coordinate reference system: confirm it with the "
        "source and set source_crs in the profile (or pass --source-crs)"
    )


def _parcel_rows(path: Path, source: SourceConfig, profile: CadastreProfile) -> Iterator[Any]:
    from core.cadastre.dataset import LoadRow

    assert source.fields is not None
    for row, (props, geometry) in enumerate(read_features(path), start=1):
        rec = map_parcel(props, source.fields, profile.ko_names, row)
        yield LoadRow(
            row=rec.row,
            source_fid=rec.source_fid,
            ko_code=rec.ko_code,
            ko_name=rec.ko_name,
            parcel_number=rec.parcel_number,
            sub_number=rec.sub_number,
            street_address=rec.street_address,
            geometry=geometry,
        )


def _ko_rows(path: Path, source: SourceConfig, profile: CadastreProfile) -> Iterator[dict]:
    fields = source.ko_fields
    if fields is None:
        raise SourceError(f"source {source.id}: a KO layer needs [ko_fields]")
    for props, geometry in read_features(path):
        name, code = canonical_ko(
            clean(props.get(fields.ko_name)) if fields.ko_name else None,
            clean(props.get(fields.ko_code)) if fields.ko_code else None,
            profile.ko_names,
        )
        yield {"ko_code": code, "ko_name": name, "geom": geometry}


def _flag_rows(path: Path, source: SourceConfig, profile: CadastreProfile) -> Iterator[dict]:
    assert source.ownership_fields is not None
    for props, _ in read_features(path):
        rec = map_ownership(props, source.ownership_fields, profile.ko_names)
        yield {
            "ko_name": rec.ko_name,
            "number": rec.parcel_number,
            "sub": rec.sub_number,
            "po": rec.public_ownership,
            "rb": rec.restitution_or_legal_burden,
        }


def _ownership_status(
    profile: CadastreProfile, m: str, record: SourceRecord | None
) -> dict[str, Any]:
    sid = profile.ownership_source
    if record is not None:
        return {
            "status": "loaded",
            "summary": f"loaded from {record.source_name} ({record.file_name})",
            **record.as_json(),
        }
    if sid is None:
        return {"status": "not_available", "summary": "no ownership source configured"}
    src = profile.sources[sid]
    if src.access != "confirmed":
        return {
            "status": "not_available",
            "reason": "bulk_access_not_confirmed",
            "summary": f"not loaded: bulk access to {src.name} is not confirmed (P0); the "
            "flags stay null and the Public ownership / Restitution layers are marked "
            "unavailable. They are never derived from other data.",
        }
    return {
        "status": "not_loaded",
        "summary": f"not loaded in this import (no --ownership export of {src.name}); the flags "
        "stay null",
    }


def _store(record: SourceRecord, m: str, label: str) -> str:
    from core.storage import ObjectStorage

    key = ObjectStorage.object_key(m, "cadastre", f"{label}/{record.file_name}")
    return ObjectStorage(get_settings()).put_file(key, record.path)


async def _import(args: argparse.Namespace) -> int:
    from core.cadastre.dataset import Provenance, import_dataset, next_label

    m = _municipality(args)
    profile = load_cadastre_profile(m)
    municipality = load_profile(m)
    source_id = args.source or profile.source
    adapter = adapter_for(profile, source_id, m)
    source = adapter.config
    if source.kind != "parcels":
        raise SourceError(f"{source_id} is not a parcel source")
    adapter.check_access()
    ownership_adapter = None
    if args.ownership:
        if profile.ownership_source is None:
            raise SourceError("no ownership source configured ([cadastre] ownership_source)")
        ownership_adapter = adapter_for(profile, profile.ownership_source, m)
        ownership_adapter.check_access()

    retrieved = (
        datetime.combine(date.fromisoformat(args.retrieved_on), datetime.min.time(), UTC)
        if args.retrieved_on
        else None
    )
    ogr = Ogr()
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="cadastre-"))
    record = adapter.acquire(
        work, path=Path(args.file) if args.file else None, retrieved_at=retrieved, ogr=ogr
    )
    print(f"export: {record.file_name} ({record.file_size} bytes, sha256 {record.file_sha256})")
    source_path = dataset_name(record.path)
    layers = ogr.layers(source_path)
    parcel_layer = _pick_layer(layers, args.layer or source.parcel_layer, "parcel")
    warnings: list[dict[str, Any]] = []
    source_crs, s_srs = _crs(parcel_layer, args.source_crs, source.source_crs, warnings)
    transform = args.transform or source.transform
    parcels_path = work / "parcels.geojsonl"
    ogr.to_geojsonseq(
        source_path,
        parcels_path,
        layer=parcel_layer.name,
        source_crs=s_srs,
        transform=transform,
    )
    ko_rows: list[dict[str, Any]] = []
    ko_layer_name = args.ko_layer or source.ko_layer
    if ko_layer_name:
        ko_layer = _pick_layer(layers, ko_layer_name, "KO")
        _, ko_s_srs = _crs(ko_layer, args.source_crs, source.source_crs, warnings)
        kos_path = work / "kos.geojsonl"
        ogr.to_geojsonseq(
            source_path, kos_path, layer=ko_layer.name, source_crs=ko_s_srs, transform=transform
        )
        ko_rows = list(_ko_rows(kos_path, source, profile))

    ownership_record = None
    flag_rows = None
    if ownership_adapter is not None:
        ownership_record = ownership_adapter.acquire(
            work, path=Path(args.ownership), retrieved_at=retrieved, ogr=ogr
        )
        flags_path = work / "ownership.geojsonl"
        ogr.to_geojsonseq(dataset_name(ownership_record.path), flags_path, geometry=False)
        flag_rows = list(_flag_rows(flags_path, ownership_adapter.config, profile))
    ownership = _ownership_status(profile, m, ownership_record)

    engine, factory = _engine()
    try:
        async with factory() as session:
            label = args.label or await next_label(session, m, profile.dataset_prefix, date.today())
            out = Path(args.out) if args.out else REPO_ROOT / "data" / "cadastre" / m / label
            file_key = None
            if args.store and not args.dry_run:
                file_key = await asyncio.to_thread(_store, record, m, label)
                print(f"stored the export at {file_key}")
            provenance = Provenance(
                source_id=record.source_id,
                source_name=record.source_name,
                source_url=record.source_url,
                method=record.method,
                retrieved_at=record.retrieved_at,
                access_basis=record.access_basis,
                licence_note=record.licence_note,
                file_name=record.file_name,
                file_sha256=record.file_sha256,
                file_size=record.file_size,
                file_key=file_key,
                source_crs=source_crs,
                transform=transform,
            )
            outcome = await import_dataset(
                session,
                municipality_id=m,
                label=label,
                provenance=provenance,
                rows=_parcel_rows(parcels_path, source, profile),
                kos=ko_rows,
                flags=flag_rows,
                ownership=ownership,
                bounds=tuple(municipality.bounds),  # type: ignore[arg-type]
                area_srid=profile.area_crs_epsg,
                on_duplicate=profile.on_duplicate,
                coverage_cell_m=profile.coverage_cell_m,
                min_coverage=profile.min_coverage,
                mass_change_threshold=profile.mass_change_threshold,
                accept_large_change=args.accept_large_change,
                expected_kos=municipality.cadastral_municipalities,
                imported_by=args.by or getpass.getuser(),
                diff_csv=out / "diff.csv",
                extra_warnings=warnings,
            )
            if args.dry_run:
                await session.rollback()
            else:
                await session.commit()
    finally:
        await engine.dispose()

    report = {
        "municipality_id": m,
        "dataset_version": outcome.dataset_version,
        "status": ("dry run: " if args.dry_run else "") + outcome.status,
        "provenance": {
            **record.as_json(),
            "file_key": file_key,
            "source_crs": source_crs,
            "transform": transform,
            "ogr_layer": parcel_layer.name,
        },
        "area_srid": profile.area_crs_epsg,
        "ownership": ownership,
        "validation": outcome.validation.as_json(),
        "diff": outcome.diff,
        "refused": outcome.refused,
        "superseded": outcome.superseded,
    }
    paths = write_report(report, out)
    _print_summary(report)
    print(f"report: {', '.join(str(p) for p in paths)}")
    if outcome.status == "staged" and not args.dry_run:
        print("next: publish (POST /v1/admin/publish) applies the dataset")
    return 0 if outcome.status == "staged" else 1


def _print_summary(report: dict[str, Any]) -> None:
    v = report["validation"]
    print(
        f"{report['dataset_version']}: {report['status']} ({v['stats'].get('records', 0)} records)"
    )
    for f in v["errors"]:
        print(f"  ERROR {f['code']}: {f['message']} ({f['count']})")
    for f in v["warnings"]:
        print(f"  warning {f['code']}: {f['message']} ({f['count']})")
    diff = report.get("diff")
    if diff:
        totals = ", ".join(f"{k} {n}" for k, n in diff["totals"].items() if n)
        print(f"  changes against {diff.get('previous_version') or 'nothing'}: {totals}")
    print(f"  ownership: {report['ownership'].get('summary')}")


def cmd_import(args: argparse.Namespace) -> int:
    return asyncio.run(_import(args))


# --- datasets / show --------------------------------------------------------------------------


async def _datasets(args: argparse.Namespace) -> int:
    from sqlalchemy import text

    engine, factory = _engine()
    try:
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT dataset_version, status, source_id, parcel_count, ko_count, "
                            "retrieved_at, file_sha256, created_at, published_at "
                            "FROM cadastral_datasets WHERE municipality_id = :m ORDER BY id"
                        ),
                        {"m": _municipality(args)},
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await engine.dispose()
    if not rows:
        print("no cadastral dataset imported yet")
    for r in rows:
        print(
            f"{r['dataset_version']:22} {r['status']:10} {r['source_id']:14} "
            f"{r['parcel_count']:>8} parcels {r['ko_count']:>3} KOs  retrieved "
            f"{r['retrieved_at']:%Y-%m-%d}  sha256 {(r['file_sha256'] or '')[:12]}"
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
                            "SELECT * FROM cadastral_datasets "
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
        print(f"no cadastral dataset {args.version}")
        return 1
    report = {
        "municipality_id": row["municipality_id"],
        "dataset_version": row["dataset_version"],
        "status": row["status"],
        "provenance": {
            k: row[k]
            for k in (
                "source_id",
                "source_name",
                "source_url",
                "method",
                "retrieved_at",
                "access_basis",
                "licence_note",
                "file_name",
                "file_sha256",
                "file_size",
                "file_key",
                "source_crs",
                "transform",
            )
        },
        "area_srid": load_cadastre_profile(row["municipality_id"]).area_crs_epsg,
        "ownership": row["ownership"] or {},
        "validation": row["validation"] or {"errors": [], "warnings": [], "stats": {}},
        "diff": row["diff"],
        "refused": None,
    }
    _print_summary(report)
    if args.out:
        paths = write_report(report, Path(args.out))
        print(f"report: {', '.join(str(p) for p in paths)}")
    else:
        print(json.dumps(report["diff"] or {}, ensure_ascii=False, default=str)[:2000])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.cadastre", description=__doc__)
    parser.add_argument("--municipality")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("sources", help="the configured sources and their access status")
    p.set_defaults(fn=cmd_sources)
    p = sub.add_parser("check-access", help="refuses sources without confirmed bulk access")
    p.add_argument("--source", action="append")
    p.set_defaults(fn=cmd_check_access)
    p = sub.add_parser("import", help="import an export as a new staged dataset")
    p.add_argument("--source")
    p.add_argument("--file", help="the export (Shapefile, zip, GeoPackage, GML, DXF ...)")
    p.add_argument("--layer")
    p.add_argument("--ko-layer")
    p.add_argument("--source-crs", help="force the export's CRS, e.g. EPSG:3908")
    p.add_argument("--transform", help="ogr2ogr -ct coordinate operation")
    p.add_argument("--ownership", help="the eKatastar attribute export (confirmed access only)")
    p.add_argument("--retrieved-on", help="date the source produced the export (YYYY-MM-DD)")
    p.add_argument("--label")
    p.add_argument("--out", help="report folder (default data/cadastre/<m>/<version>)")
    p.add_argument("--work", help="folder for the intermediate files (default a temp folder)")
    p.add_argument("--accept-large-change", action="store_true")
    p.add_argument("--store", action="store_true", help="keep a copy in the private bucket")
    p.add_argument("--dry-run", action="store_true", help="validate and diff, stage nothing")
    p.add_argument("--by")
    p.set_defaults(fn=cmd_import)
    p = sub.add_parser("datasets", help="the imported versions")
    p.set_defaults(fn=lambda a: asyncio.run(_datasets(a)))
    p = sub.add_parser("show", help="a dataset's validation and diff")
    p.add_argument("version")
    p.add_argument("--out")
    p.set_defaults(fn=lambda a: asyncio.run(_show(a)))
    args = parser.parse_args(argv)
    try:
        return int(args.fn(args))
    except AccessNotConfirmed as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (SourceError, OgrError, LookupError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
