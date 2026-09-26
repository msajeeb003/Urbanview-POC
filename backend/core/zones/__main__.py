"""``python -m core.zones``: the zone tooling for one municipality (``--municipality``, default the
settings' ``MUNICIPALITY_ID``), driven by ``data/zones/<municipality>/zones.toml``.

    seed      the zone list and the document list from the reference capture + the eRegistri
              snapshot (zones.csv, zone_documents.csv); --refresh-eregistri fetches the registry
              list once; an edited list with confirmed rows is kept unless --force
    template  the QGIS working file (GeoPackage in the editing CRS: zones, zone_documents and the
              reference layers from PostGIS) and the project (.qgz); --no-db without reference
              layers; a GeoPackage with drawn zones is kept unless --force
    validate  check the QGIS outputs without touching the database (exit 1 on errors)
    import    validate, then stage zones + documents with a dataset_version, export zones.geojson
              (EPSG:4326) and zone_documents.csv, and write the report; --dry-run validates only
    report    the zone / document / parcel-count report of a dataset (default the latest)
    datasets  the imported zone datasets

Errors in the data never stage anything; the publish job (the admin's one button) applies a staged
dataset. Run from backend/ (DATABASE_URL from backend/.env).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from core.config import get_settings
from core.municipality import load_profile
from core.zones.config import ZoneSetConfig, load_zone_config


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rel(cfg: ZoneSetConfig, path: Path) -> str:
    try:
        return path.resolve().relative_to(cfg.root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _engine():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(get_settings().database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _document_types(municipality_id: str) -> dict[str, str]:
    terminology = load_profile(municipality_id).terminology
    return dict(terminology.document_types)


# --- seed -----------------------------------------------------------------------------------------


def cmd_seed(cfg: ZoneSetConfig, args: argparse.Namespace) -> int:
    from core.zones.seed import build_seed, write_seed

    result = build_seed(cfg, refresh_eregistri=args.refresh_eregistri)
    try:
        paths = write_seed(cfg, result, overwrite=args.force)
    except FileExistsError as exc:
        print(f"refused: {exc}")
        return 1
    for key, value in result.stats.items():
        print(f"  {key}: {value}")
    for warning in result.warnings[:40]:
        print(f"  ! {warning}")
    if len(result.warnings) > 40:
        print(f"  ! ... {len(result.warnings) - 40} more")
    for path in paths:
        print(f"wrote {_rel(cfg, path)}")
    return 0


# --- template -------------------------------------------------------------------------------------


async def _template(cfg: ZoneSetConfig, args: argparse.Namespace) -> int:
    from core.zones.template import build_template

    if args.no_db:
        result = await build_template(cfg, session=None, overwrite=args.force)
    else:
        engine, factory = _engine()
        try:
            async with factory() as session:
                result = await build_template(cfg, session=session, overwrite=args.force)
        finally:
            await engine.dispose()
    for name, count in result.layers.items():
        print(f"  {name}: {count}")
    for warning in result.warnings:
        print(f"  ! {warning}")
    print(f"wrote {_rel(cfg, result.geopackage)} and {_rel(cfg, result.project)}")
    return 0


def cmd_template(cfg: ZoneSetConfig, args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_template(cfg, args))
    except FileExistsError as exc:
        print(f"refused: {exc}")
        return 1


# --- validate / import ----------------------------------------------------------------------------


def _inputs(cfg: ZoneSetConfig, args: argparse.Namespace) -> tuple[Path, Path]:
    zones = Path(args.zones) if args.zones else None
    documents = Path(args.documents) if args.documents else None
    if zones is None:
        zones = cfg.geopackage if cfg.geopackage.is_file() else cfg.zones_export
    if documents is None:
        documents = cfg.geopackage if zones.suffix.lower() == ".gpkg" else cfg.documents
    return zones, documents


def _load_extent(cfg: ZoneSetConfig, srs_id: int) -> Any:
    """The configured extent polygon, when it is in the dataset's CRS (no reprojection here)."""
    if cfg.validation.extent is None:
        return None
    from core.zones.validate import read_extent

    geometry, extent_srs = read_extent(cfg.validation.extent)
    if extent_srs != srs_id:
        name = cfg.validation.extent.name
        print(f"  ! extent {name} is in EPSG:{extent_srs}, the zones in EPSG:{srs_id}: skipped")
        return None
    return geometry


def _validate(cfg: ZoneSetConfig, args: argparse.Namespace):
    from core.zones.validate import read_dataset, validate

    zones_path, documents_path = _inputs(cfg, args)
    print(f"zones:     {_rel(cfg, zones_path)}")
    print(f"documents: {_rel(cfg, documents_path)}")
    dataset = read_dataset(zones_path, documents_path)
    config = cfg.validation
    if args.require_confirmed:
        config = replace(config, require_confirmed=True)
    report = validate(
        dataset,
        document_types=_document_types(cfg.municipality_id),
        config=config,
        extent=_load_extent(cfg, dataset.srs_id),
    )
    for problem in report.problems:
        where = " ".join(x for x in (problem.zone_id, problem.document) if x)
        area = f" ({problem.area_m2:.1f} m²)" if problem.area_m2 else ""
        print(f"  {problem.severity:7} {problem.code}: {problem.message}{area} {where}".rstrip())
    print(
        f"{len(report.errors)} error(s), {len(report.warnings)} warning(s); "
        + ", ".join(f"{k}={v}" for k, v in report.stats.items() if not isinstance(v, dict))
    )
    cfg.reports.mkdir(parents=True, exist_ok=True)
    (cfg.reports / "validation.md").write_text(report.to_markdown(), encoding="utf-8", newline="\n")
    (cfg.reports / "validation.json").write_text(
        json.dumps(report.to_json(), ensure_ascii=False, indent=1, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return dataset, report, zones_path, documents_path


def cmd_validate(cfg: ZoneSetConfig, args: argparse.Namespace) -> int:
    _, report, _, _ = _validate(cfg, args)
    return 0 if report.ok else 1


async def _import(cfg: ZoneSetConfig, args: argparse.Namespace) -> int:
    from core.zones.report import build_report, write_report
    from core.zones.staging import (
        export_geojson,
        next_label,
        stage_dataset,
        write_documents_csv,
    )

    dataset, validation, zones_path, documents_path = _validate(cfg, args)
    if not validation.ok:
        print("import refused: fix the errors above (reports/validation.md) and run it again")
        return 1
    if args.dry_run:
        print("dry run: nothing staged")
        return 0
    sources = {
        "zones": {"path": _rel(cfg, zones_path), "sha256": _sha256(zones_path)},
        "documents": {"path": _rel(cfg, documents_path), "sha256": _sha256(documents_path)},
    }
    engine, factory = _engine()
    try:
        async with factory() as session:
            label = args.label or await next_label(
                session, cfg.municipality_id, cfg.dataset_prefix, date.today()
            )
            staged = await stage_dataset(
                session,
                municipality_id=cfg.municipality_id,
                dataset=dataset,
                validation=validation,
                label=label,
                imported_by=args.by,
                sources=sources,
            )
            await session.commit()
            print(
                f"staged {label}: {staged.zones} zones, {staged.documents} documents "
                f"(batch {staged.batch_id}; matched to registered documents: {staged.matches})"
            )
            if staged.superseded:
                print(f"  superseded the staged dataset(s): {', '.join(staged.superseded)}")
            if not args.no_export:
                n = await export_geojson(session, staged.batch_id, cfg.zones_export, name=cfg.title)
                write_documents_csv(dataset, cfg.documents)
                print(
                    f"exported {n} zones to {_rel(cfg, cfg.zones_export)} (EPSG:4326) and the "
                    f"documents to {_rel(cfg, cfg.documents)}: version both"
                )
            report = await build_report(session, cfg.municipality_id, dataset_version=label)
            from sqlalchemy import text

            await session.execute(
                text("UPDATE zone_datasets SET report = CAST(:r AS jsonb) WHERE id = :id"),
                {
                    "r": json.dumps(report.to_json(), ensure_ascii=False, default=str),
                    "id": staged.dataset_id,
                },
            )
            await session.commit()
    finally:
        await engine.dispose()
    paths = write_report(report, cfg.reports / label, title=cfg.title)
    _print_report(report)
    print(f"report: {', '.join(_rel(cfg, p) for p in paths)}")
    print("next: publish (POST /v1/admin/publish) applies the zones and the documents")
    return 0


def cmd_import(cfg: ZoneSetConfig, args: argparse.Namespace) -> int:
    return asyncio.run(_import(cfg, args))


# --- report / datasets ----------------------------------------------------------------------------


def _print_report(report: Any) -> None:
    print(f"{'zone':32} {'docs':>5} {'adopted':>7} {'in prog':>7} {'cadastral':>9} {'urban':>6}")
    for z in report.zones:
        print(
            f"{z.name[:32]:32} {z.documents:5} {z.adopted:7} {z.in_progress:7} "
            f"{z.cadastral_parcels:9} {z.urban_parcels:6}"
        )
    for warning in report.warnings:
        print(f"  ! {warning}")


async def _report(cfg: ZoneSetConfig, args: argparse.Namespace) -> int:
    from core.zones.report import build_report, write_report

    engine, factory = _engine()
    try:
        async with factory() as session:
            report = await build_report(session, cfg.municipality_id, dataset_version=args.dataset)
    finally:
        await engine.dispose()
    paths = write_report(report, cfg.reports / report.dataset_version, title=cfg.title)
    _print_report(report)
    print(f"report: {', '.join(_rel(cfg, p) for p in paths)}")
    return 0


def cmd_report(cfg: ZoneSetConfig, args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_report(cfg, args))
    except LookupError as exc:
        print(exc)
        return 1


async def _datasets(cfg: ZoneSetConfig) -> int:
    from sqlalchemy import text

    engine, factory = _engine()
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT dataset_version, status, zone_count, document_count, imported_by,"
                        " created_at, published_at FROM zone_datasets WHERE municipality_id = :m"
                        " ORDER BY id DESC"
                    ),
                    {"m": cfg.municipality_id},
                )
            ).all()
    finally:
        await engine.dispose()
    for r in rows:
        published = f", published {r.published_at:%Y-%m-%d}" if r.published_at else ""
        print(
            f"{r.dataset_version:36} {r.status:10} {r.zone_count:3} zones {r.document_count:4} "
            f"documents  by {r.imported_by} {r.created_at:%Y-%m-%d}{published}"
        )
    if not rows:
        print("no zone datasets yet")
    return 0


# --- entry ----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.zones", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--municipality", default=None, help="default: MUNICIPALITY_ID")
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed", help="the zone and document lists from the reference")
    seed.add_argument("--refresh-eregistri", action="store_true")
    seed.add_argument("--force", action="store_true")
    template = commands.add_parser("template", help="the QGIS GeoPackage and project")
    template.add_argument("--no-db", action="store_true")
    template.add_argument("--force", action="store_true")
    for name in ("validate", "import"):
        sub = commands.add_parser(name, help=f"{name} the QGIS outputs")
        sub.add_argument("--zones", help="GeoPackage or GeoJSON (default: the working GeoPackage)")
        sub.add_argument("--documents", help="GeoPackage or CSV (default: next to the zones)")
        sub.add_argument("--require-confirmed", action="store_true")
        if name == "import":
            sub.add_argument("--label", help="dataset_version (default <prefix>-<yyyymmdd>-<n>)")
            sub.add_argument("--by", default=getpass.getuser(), help="who imports")
            sub.add_argument("--dry-run", action="store_true")
            sub.add_argument("--no-export", action="store_true")
    report = commands.add_parser("report", help="the report of a dataset")
    report.add_argument("--dataset", help="dataset_version (default: the latest)")
    commands.add_parser("datasets", help="the imported zone datasets")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_zone_config(args.municipality or get_settings().municipality_id)
    if args.command == "seed":
        return cmd_seed(cfg, args)
    if args.command == "template":
        return cmd_template(cfg, args)
    if args.command == "validate":
        return cmd_validate(cfg, args)
    if args.command == "import":
        return cmd_import(cfg, args)
    if args.command == "report":
        return cmd_report(cfg, args)
    return asyncio.run(_datasets(cfg))


if __name__ == "__main__":
    sys.exit(main())
