"""The bodies of the ``geo`` queue's jobs: ``process_geometry`` (a document's drawing file into
STAGING) and ``import_zones`` (a zone GeoPackage from QGIS into STAGING). Nothing here touches the
serving tables: the publish job applies what is staged.

``process_geometry`` (``POST /v1/admin/files/{id}/jobs/geo``) by the file's kind:

- **GIS drawing** (``gis``: GeoPackage, GeoJSON, zipped Shapefile; a QGIS redraw of a scanned
  sheet or the plan's official GIS, pilot origins ``manual_qgis`` / ``official_gis``): the
  file's layers named as the GIS ingestion contract names them (``plan_boundary``,
  ``urban_parcels`` with ``urban_parcel_number`` and ``block_ref``, ``urban_blocks`` with
  ``block_ref``, ``planned_land_use`` with ``code`` / ``name``; the staged names
  ``document_coverage`` and ``land_use`` are accepted too; a single-layer file is named by its
  file name) are reprojected from the file's own CRS (else the profile's ``source_crs_epsg``) to
  EPSG:4326 with GDAL, then snapped, validated and staged for every current document version
  the file is a drawing of, exactly as georeferencing stages a document
  (``core.gis.georef.stage.stage_document``: source ``gis_file``, method ``native``, no fit). A
  refused dataset (outside the municipality, no overlap with the cadastre …) is recorded
  ``invalid`` and the job fails with its reasons.
- **PDF drawing** (``planning_document``): the PDF stage runs (its manifest records the pages the
  week-1 assessment's rule calls scanned: the Data sources list flags them "needs QGIS redraw"),
  then the job ends with what the sheet needs: a QGIS redraw of the scanned pages, or the
  control-point georeferencing of a vector sheet (``python -m core.gis.georef``), which stays a
  staff task outside the job.

``import_zones`` (``POST /v1/admin/zones/import``): the GeoPackage's ``zones`` layer and
``zone_documents`` table read and validated by ``core.zones`` (the CLI's checks and the
municipality's ``data/zones/<m>/zones.toml`` when the server has it, else its defaults), then
staged as a zone dataset with its report (``core.zones.staging.stage_dataset``). A dry run
validates only. Errors refuse the import (the job fails, naming them).

The module imports only the standard library at load time (the API imports the task module).
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger("urbanview.jobs.geometry")

# the GIS ingestion contract's layer names (the extraction's GeoPackage and the QGIS redraw
# template write these), and the staged names, onto the georeferencing stage's layers
CONTRACT_LAYERS = {
    "plan_boundary": "plan_boundary",
    "document_coverage": "plan_boundary",
    "urban_parcels": "urban_parcels",
    "urban_blocks": "urban_blocks",
    "planned_land_use": "planned_land_use",
    "land_use": "planned_land_use",
}
# attribute names the stage reads, matched without regard to case
CONTRACT_FIELDS = ("urban_parcel_number", "block_ref", "code", "name", "feature_key")
READABLE = (".gpkg", ".geojson", ".json", ".shp")
MAX_REASONS = 6


class GeometryError(Exception):
    """What the job could not do, in words the Data sources list shows (a final failure)."""


@dataclass(frozen=True, slots=True)
class FileRow:
    id: int
    kind: str
    object_key: str
    sha256: str
    original_filename: str


FILE_SQL = """
    SELECT id, kind, object_key, sha256, original_filename
    FROM stored_files WHERE id = :id AND municipality_id = :m
"""
DRAWING_DOCUMENTS_SQL = """
    SELECT d.id, d.name
    FROM planning_document_files pf
    JOIN planning_documents d ON d.id = pf.document_id
    WHERE pf.file_id = :id AND d.municipality_id = :m AND d.is_current_version
      AND pf.role IN ('drawing', 'both')
    ORDER BY d.id
"""
REQUESTED_BY_SQL = "SELECT requested_by FROM pipeline_jobs WHERE id = :id"


def _pages(pages: list[int]) -> str:
    return ", ".join(str(p) for p in pages)


# --- reading a GIS drawing ------------------------------------------------------------------------


def unpack(path: Path, work: Path) -> list[Path]:
    """The datasets GDAL should open: the file itself, or what a zip holds (a zipped Shapefile
    keeps its sidecar files next to the ``.shp``)."""
    if path.suffix.lower() != ".zip":
        return [path]
    target = work / "unzipped"
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            name = Path(member.filename)
            if member.is_dir() or name.is_absolute() or ".." in name.parts:
                continue  # never write outside the scratch folder
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(member))
    return sorted(p for p in target.rglob("*") if p.suffix.lower() in READABLE)


def _contract_props(props: dict[str, Any]) -> dict[str, Any]:
    lowered = {str(k).lower(): v for k, v in props.items()}
    out = dict(props)
    for key in CONTRACT_FIELDS:
        if key not in out and key in lowered:
            out[key] = lowered[key]
    for key in ("urban_parcel_number", "block_ref", "code"):
        if out.get(key) is not None and not isinstance(out[key], str):
            value = out[key]
            out[key] = (
                str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
            )
    return out


def read_drawing(
    sources: list[Path], work: Path, *, ogr: Any, fallback_crs: str | None
) -> tuple[dict[str, list[tuple[Any, dict[str, Any]]]], list[str], set[str]]:
    """Every contract layer of the files in EPSG:4326 as (shapely geometry, properties), the
    layer names found, and the CRSs they were drawn in."""
    import shapely

    from core.cadastre.ogr import read_features

    features: dict[str, list[tuple[Any, dict[str, Any]]]] = {}
    found: list[str] = []
    crs_seen: set[str] = set()
    for index, source in enumerate(sources):
        for layer in ogr.layers(str(source)):
            found.append(layer.name)
            target = CONTRACT_LAYERS.get(layer.name.strip().lower())
            if target is None:
                continue
            crs = layer.crs or fallback_crs
            if crs is None:
                raise GeometryError(
                    f"layer {layer.name} has no coordinate system and the municipality profile "
                    "names none to assume: save it with its CRS"
                )
            crs_seen.add(crs)
            out = work / f"{index}-{target}.geojsonl"
            ogr.to_geojsonseq(
                str(source), out, layer=layer.name, source_crs=None if layer.crs else crs
            )
            rows = features.setdefault(target, [])
            for props, geometry in read_features(out):
                if geometry is None:
                    continue
                geom = shapely.make_valid(shapely.from_geojson(geometry))
                if geom.is_empty:
                    continue
                props = _contract_props(props)
                props.setdefault("feature_key", f"{target}:{len(rows) + 1}")
                rows.append((geom, props))
    return features, found, crs_seen


# --- the geometry job -----------------------------------------------------------------------------


class GeometryRunner:
    def __init__(
        self,
        session_factory: Any,
        storage: Any,
        *,
        municipality_id: str,
        ogr_factory: Any = None,
        preprocess: Any = None,
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.municipality_id = municipality_id
        self.ogr_factory = ogr_factory
        self.preprocess = preprocess  # async (file_id) -> the PDF stage's result

    async def _file(self, file_id: int) -> FileRow:
        from sqlalchemy import text

        async with self.session_factory() as session:
            row = (
                (await session.execute(text(FILE_SQL), {"id": file_id, "m": self.municipality_id}))
                .mappings()
                .first()
            )
        if row is None:
            raise GeometryError(f"no stored file with id {file_id}")
        return FileRow(**dict(row))

    async def run(self, file_id: int, *, job_id: int | None = None) -> dict[str, Any]:
        from sqlalchemy import text

        file = await self._file(file_id)
        if file.kind == "planning_document":
            return await self._pdf(file)
        if file.kind != "gis":
            raise GeometryError(f"a {file.kind} file has no geometry job")
        async with self.session_factory() as session:
            documents = [
                dict(r)
                for r in (
                    await session.execute(
                        text(DRAWING_DOCUMENTS_SQL), {"id": file_id, "m": self.municipality_id}
                    )
                ).mappings()
            ]
            requested_by = None
            if job_id is not None:
                requested_by = (
                    await session.execute(text(REQUESTED_BY_SQL), {"id": job_id})
                ).scalar()
        if not documents:
            raise GeometryError(
                "the file is not a drawing of any current planning document: add it to the "
                "document as a drawing first"
            )
        with tempfile.TemporaryDirectory(prefix="geo-") as scratch:
            work = Path(scratch)
            data = await asyncio.to_thread(self.storage.get_bytes, file.object_key)
            path = work / (Path(file.object_key).name or f"drawing-{file.id}")
            path.write_bytes(data)
            ogr = self.ogr_factory() if self.ogr_factory else _ogr()
            sources = unpack(path, work)
            if not sources:
                raise GeometryError("the zip holds no GeoPackage, GeoJSON or Shapefile")
            features, found, crs_seen = await asyncio.to_thread(
                read_drawing,
                sources,
                work,
                ogr=ogr,
                fallback_crs=_profile_crs(self.municipality_id),
            )
        if not features:
            raise GeometryError(
                f"no layer of the file is named as the GIS ingestion contract names them "
                f"(found: {', '.join(found) or 'none'}; expected: plan_boundary, urban_parcels, "
                "urban_blocks, planned_land_use)"
            )
        staged = [
            await self._stage(file, doc, features, crs_seen, found, requested_by)
            for doc in documents
        ]
        refused = [s for s in staged if s["status"] != "staged"]
        if refused:
            reasons = "; ".join(
                f"{s['dataset_version']}: " + ", ".join(s["errors"][:MAX_REASONS]) for s in refused
            )
            raise GeometryError(f"the geometry was refused ({reasons})")
        return {"file_id": file.id, "kind": file.kind, "layers_found": found, "documents": staged}

    async def _stage(
        self,
        file: FileRow,
        document: dict[str, Any],
        features: dict[str, list[tuple[Any, dict[str, Any]]]],
        crs_seen: set[str],
        found: list[str],
        requested_by: str | None,
    ) -> dict[str, Any]:
        from core.cadastre.config import load_cadastre_profile
        from core.gis.extract.rules import GeorefSettings
        from core.gis.georef.apply import digest
        from core.gis.georef.stage import next_label, stage_document
        from core.municipality import load_profile

        m = self.municipality_id
        profile = load_profile(m)
        attributes = {"document_id": document["id"]}
        rows = {
            layer: [(g, {**props, **attributes}) for g, props in items]
            for layer, items in features.items()
        }
        crs = ", ".join(sorted(crs_seen))
        async with self.session_factory() as session:
            label = await next_label(session, m, document["id"], date.today())
            rows = {
                layer: [(g, {**props, "dataset_version": label}) for g, props in items]
                for layer, items in rows.items()
            }
            outcome = await stage_document(
                session,
                municipality_id=m,
                document_id=document["id"],
                label=label,
                features=rows,
                transform={
                    "crs": crs,
                    "method": "native",
                    "rmse_m": None,
                    "max_residual_m": None,
                    "points_used": 0,
                    "sheets": [],
                    "file_id": file.id,
                    "file_sha256": file.sha256,
                    "file_name": file.original_filename,
                    "layers": found,
                },
                source="gis_file",
                bounds=profile.bounds,
                snap_tolerance_m=GeorefSettings().snap_tolerance_m,
                metric_srid=load_cadastre_profile(m).area_crs_epsg,
                parcel_prefix=profile.terminology.urban_parcel.abbreviation,
                output_sha256=digest(rows),
                gpkg_key=None,
                imported_by=requested_by or "worker:process_geometry",
            )
            await session.commit()
        log.info(
            "process_geometry staged",
            extra={"file_id": file.id, "document_id": document["id"], "status": outcome.status},
        )
        return {
            "document_id": document["id"],
            "document": document["name"],
            "dataset_version": outcome.dataset_version,
            "status": outcome.status,
            "crs": crs,
            "features": outcome.counts,
            "batches": outcome.batches,
            "errors": [f"{e.code}: {e.message}" for e in outcome.errors],
            "warnings": [w.code for w in outcome.warnings],
            "snapped_vertices": outcome.snap.get("snapped_vertices"),
            "superseded": outcome.superseded,
        }

    async def _pdf(self, file: FileRow) -> dict[str, Any]:
        if self.preprocess is None:
            raise GeometryError("the PDF stage is not configured for the geometry job")
        result = await self.preprocess(file.id)
        summary = result.get("summary") or {}
        redraw = summary.get("redraw_pages")
        if redraw is None:
            redraw = summary.get("scanned_pages") or []
        if redraw:
            raise GeometryError(
                f"pages {_pages(redraw)} are scanned sheets (the week-1 assessment's class C): "
                "redraw them in QGIS and add the GeoPackage to the document as a drawing"
            )
        raise GeometryError(
            "a vector PDF is georeferenced from control points: run python -m core.gis.georef "
            "(docs/gis/georeferencing.md), then publish"
        )


def _ogr() -> Any:
    from core.cadastre.ogr import Ogr, OgrError

    try:
        return Ogr()
    except OgrError as exc:
        raise GeometryError(str(exc)) from exc


def _profile_crs(municipality_id: str) -> str | None:
    from core.municipality import load_profile

    epsg = load_profile(municipality_id).source_crs_epsg
    return f"EPSG:{epsg}" if epsg else None


# --- the zone import ------------------------------------------------------------------------------


async def import_zones(
    session_factory: Any,
    storage: Any,
    *,
    municipality_id: str,
    file_id: int,
    dry_run: bool = False,
    job_id: int | None = None,
) -> dict[str, Any]:
    """Validate a QGIS zone GeoPackage and stage it (see the module docstring)."""
    import json

    from sqlalchemy import text

    from api.services.audit import write_audit
    from core.municipality import load_profile
    from core.zones.config import ValidationConfig, load_zone_config
    from core.zones.report import build_report
    from core.zones.staging import next_label, stage_dataset
    from core.zones.validate import read_dataset, read_extent, validate

    m = municipality_id
    async with session_factory() as session:
        row = (await session.execute(text(FILE_SQL), {"id": file_id, "m": m})).mappings().first()
        requested_by = None
        if job_id is not None:
            requested_by = (await session.execute(text(REQUESTED_BY_SQL), {"id": job_id})).scalar()
    if row is None:
        raise GeometryError(f"no stored file with id {file_id}")
    file = FileRow(**dict(row))
    if file.kind != "gis" or not file.object_key.lower().endswith(".gpkg"):
        raise GeometryError("zones are imported from the GeoPackage drawn in QGIS (.gpkg)")
    try:
        cfg = load_zone_config(m)
        config, prefix = cfg.validation, cfg.dataset_prefix
    except FileNotFoundError:
        config, prefix = ValidationConfig(), f"{m}-zones"
    with tempfile.TemporaryDirectory(prefix="zones-") as scratch:
        path = Path(scratch) / "zones.gpkg"
        path.write_bytes(await asyncio.to_thread(storage.get_bytes, file.object_key))
        try:
            dataset = read_dataset(path, path)
        except (ValueError, KeyError) as exc:
            raise GeometryError(
                f"the GeoPackage could not be read as a zone dataset: {exc}"
            ) from exc
        extent = None
        if config.extent is not None and config.extent.is_file():
            geometry, srs = read_extent(config.extent)
            extent = geometry if srs == dataset.srs_id else None
        report = validate(
            dataset,
            document_types=dict(load_profile(m).terminology.document_types),
            config=config,
            extent=extent,
        )
    problems = {
        "errors": [p.to_json() for p in report.errors],
        "warnings": [p.to_json() for p in report.warnings][:50],
        "stats": {k: v for k, v in report.stats.items() if not isinstance(v, dict)},
    }
    if not report.ok:
        named = "; ".join(
            f"{p.code}: {p.message}" + (f" ({p.zone_id})" if p.zone_id else "")
            for p in report.errors[:MAX_REASONS]
        )
        raise GeometryError(f"import refused, {len(report.errors)} error(s): {named}")
    if dry_run:
        return {"status": "valid", "dry_run": True, "file_id": file_id, **problems}
    source = {"file_id": file_id, "sha256": file.sha256, "path": file.original_filename}
    async with session_factory() as session:
        label = await next_label(session, m, prefix, date.today())
        staged = await stage_dataset(
            session,
            municipality_id=m,
            dataset=dataset,
            validation=report,
            label=label,
            imported_by=requested_by or "worker:import_zones",
            sources={"zones": source, "documents": source},
        )
        summary = await build_report(session, m, dataset_version=label)
        await session.execute(
            text("UPDATE zone_datasets SET report = CAST(:r AS jsonb) WHERE id = :id"),
            {
                "r": json.dumps(summary.to_json(), ensure_ascii=False, default=str),
                "id": staged.dataset_id,
            },
        )
        await write_audit(
            session,
            municipality_id=m,
            actor="worker:import_zones",
            action="zones.import",
            entity_type="zone_dataset",
            entity_id=staged.dataset_id,
            details={
                "dataset_version": label,
                "file_id": file_id,
                "job_id": job_id,
                "requested_by": requested_by,
                "zones": staged.zones,
                "documents": staged.documents,
                "superseded": staged.superseded,
            },
        )
        await session.commit()
    return {
        "status": "staged",
        "dry_run": False,
        "file_id": file_id,
        "dataset_version": label,
        "zones": staged.zones,
        "documents": staged.documents,
        "matches": staged.matches,
        "superseded": staged.superseded,
        **problems,
    }
