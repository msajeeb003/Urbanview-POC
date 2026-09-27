"""Apply a stored transform to a document's layers and bring them to EPSG:4326 with GDAL.

Input: the extraction's GeoPackage (``python -m core.gis.extract run``: the document's local
frame) or a sheet redrawn by hand in QGIS with the same layers (``frame = "sheet:<id>"``: the
sheet's PDF points). Every geometry goes local -> the plan's projected CRS through the stored
transform (exact, deterministic), then ogr2ogr reprojects each layer to EPSG:4326 (the profile's
or document's coordinate operation with ``-ct`` when a datum shift matters) into one GeoPackage.
The same inputs and the same stored transform always give the same file (``digest``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import shapely
from shapely import affinity
from shapely.geometry.base import BaseGeometry

from core.cadastre.ogr import Ogr
from core.gis.extract.gpkg import read_gpkg
from core.gis.extract.rules import TARGET_LAYERS, DocumentRules
from core.gis.georef.fit import Transform, sheet_k

Features = dict[str, list[tuple[BaseGeometry, dict[str, Any]]]]


def _flat(key: str, value: Any) -> Any:
    """GeoPackage columns are scalars: flags comma-separated (the extraction's convention), other
    lists as JSON text (read back by ``read_gpkg``)."""
    if isinstance(value, list | tuple):
        return ",".join(map(str, value)) if key == "qa_flags" else json.dumps(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def to_local(features: Features, rules: DocumentRules, frame: str) -> Features:
    """A redrawn sheet's PDF points -> the document's local frame (``frame = "sheet:<id>"``)."""
    if frame == "local":
        return features
    if not frame.startswith("sheet:"):
        raise ValueError(f"frame {frame!r}: use local or sheet:<id>")
    sheet_id = frame.split(":", 1)[1]
    sheet = next((s for s in rules.sheets if s.id == sheet_id), None)
    if sheet is None:
        raise ValueError(f"no sheet {sheet_id!r} in the rules")
    k = sheet_k(sheet)
    matrix = [k, 0.0, 0.0, k, sheet.offset_m[0], sheet.offset_m[1]]
    return {
        layer: [(affinity.affine_transform(g, matrix), props) for g, props in rows]
        for layer, rows in features.items()
    }


def project(features: Features, transform: Transform) -> Features:
    matrix = transform.shapely_matrix()
    return {
        layer: [(affinity.affine_transform(g, matrix), props) for g, props in rows]
        for layer, rows in features.items()
    }


def _write_geojsonseq(path: Path, rows: list[tuple[BaseGeometry, dict[str, Any]]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for geom, props in rows:
            feature = {
                "type": "Feature",
                "properties": {k: _flat(k, v) for k, v in props.items() if k != "fid"},
                "geometry": json.loads(shapely.to_geojson(geom)),
            }
            fh.write(json.dumps(feature, ensure_ascii=False, separators=(",", ":")) + "\n")


def reproject(
    projected: Features, crs: str, target: Path, work: Path, *, ct: str | None, ogr: Ogr
) -> None:
    """Every layer from the plan CRS to EPSG:4326, into one GeoPackage (GDAL)."""
    if target.exists():
        target.unlink()
    work.mkdir(parents=True, exist_ok=True)
    first = True
    for layer in TARGET_LAYERS:
        rows = projected.get(layer) or []
        if not rows:
            continue
        src = work / f"{layer}.projected.geojsonl"
        _write_geojsonseq(src, rows)
        cmd = [ogr.ogr2ogr, "-f", "GPKG"]
        if not first:
            cmd.append("-update")
        cmd += [str(target), str(src), "-nln", layer, "-s_srs", crs, "-t_srs", "EPSG:4326"]
        if ct:
            cmd += ["-ct", ct]
        cmd += ["-nlt", "PROMOTE_TO_MULTI"]
        ogr._run(cmd)
        first = False


VOLATILE = {"dataset_version", "fid"}  # differ between runs by design: not in the digest


def digest(features: Features) -> str:
    """Fingerprint of georeferenced features: re-running with the stored transform must
    reproduce it exactly (the dataset label aside)."""
    h = hashlib.sha256()
    for layer in sorted(features):
        for geom, props in sorted(
            features[layer], key=lambda r: str(r[1].get("feature_key") or "")
        ):
            h.update(layer.encode())
            h.update(shapely.to_wkb(geom, byte_order=1, output_dimension=2))
            stable = {k: v for k, v in props.items() if k not in VOLATILE}
            h.update(json.dumps(stable, sort_keys=True, default=str).encode())
    return h.hexdigest()


def georeference(
    source_gpkg: Path,
    rules: DocumentRules,
    transform: Transform,
    target_gpkg: Path,
    work: Path,
    *,
    frame: str = "local",
    ct: str | None = None,
    ogr: Ogr | None = None,
    attributes: dict[str, Any] | None = None,
) -> Features:
    """The whole chain; returns the EPSG:4326 features read back from the GeoPackage.
    ``attributes`` (``document_id``, ``dataset_version``) are set on every feature."""
    features = to_local(read_gpkg(source_gpkg), rules, frame)
    if attributes:
        features = {
            layer: [(g, {**props, **attributes}) for g, props in rows]
            for layer, rows in features.items()
        }
    reproject(
        project(features, transform), transform.crs, target_gpkg, work, ct=ct, ogr=ogr or Ogr()
    )
    out = read_gpkg(target_gpkg)
    for rows in out.values():
        for _, props in rows:
            props.pop("fid", None)
    return out
