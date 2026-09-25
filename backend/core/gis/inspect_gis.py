"""Inspect GIS / CAD files for the geometry assessment with GDAL's ``ogrinfo``.

Per file: the layers GDAL sees, geometry type, feature count, coordinate system (EPSG code when
GDAL identifies one) and extent. A DXF exposes a single ``entities`` layer; the CAD layers
inside it are counted with a GROUP BY on its ``Layer`` field. DWG goes through GDAL's CAD driver,
which reads AutoCAD R2000 files only: anything newer needs a DXF export (or the ODA File
Converter) first, and the report says so instead of failing.

``ogrinfo`` is looked up as: the explicit path, ``$OGRINFO``, ``PATH``, then the portable
PostGIS bundle of ``database/scripts/dev_postgis.py`` (Windows machines without Docker), whose
GDAL data and PROJ database are wired in when found.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GIS_SUFFIXES = {
    ".shp",
    ".gpkg",
    ".geojson",
    ".json",
    ".dxf",
    ".dwg",
    ".gml",
    ".kml",
    ".tab",
    ".mif",
    ".dgn",
    ".gdb",
    ".sqlite",
}
TIMEOUT_S = 600


@dataclass
class GisLayer:
    name: str
    geometry_type: str | None
    features: int | None
    epsg: int | None
    crs_name: str | None
    extent: list[float] | None
    fields: list[str] = field(default_factory=list)
    cad_layers: dict[str, int] | None = None  # DXF: CAD layer -> entities


@dataclass
class GisReport:
    path: str
    sha256: str | None
    size_bytes: int
    driver: str | None
    layers: list[GisLayer] = field(default_factory=list)
    error: str | None = None


def _bundle_dirs() -> list[Path]:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return []
    return [Path(base) / "urbanview-dev" / "pg16" / "pgsql" / "bin"]


def find_ogrinfo(explicit: str | None = None) -> str | None:
    for candidate in (explicit, os.environ.get("OGRINFO"), shutil.which("ogrinfo")):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    for directory in _bundle_dirs():
        exe = directory / "ogrinfo.exe"
        if exe.is_file():
            return str(exe)
    return None


def gdal_env(ogrinfo: str) -> dict[str, str]:
    """Environment for ``ogrinfo``: GDAL_DATA / PROJ_LIB of a portable bundle when unset."""
    env = dict(os.environ)
    root = Path(ogrinfo).resolve().parent.parent  # .../pgsql
    gdal_data = root / "gdal-data"
    if "GDAL_DATA" not in env and gdal_data.is_dir():
        env["GDAL_DATA"] = str(gdal_data)
    if "PROJ_LIB" not in env and "PROJ_DATA" not in env:
        for proj_db in sorted(root.glob("share/contrib/postgis-*/proj/proj.db")):
            env["PROJ_LIB"] = env["PROJ_DATA"] = str(proj_db.parent)
    return env


def gdal_version(ogrinfo: str) -> str | None:
    try:
        out = subprocess.run(
            [ogrinfo, "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            env=gdal_env(ogrinfo),
        )
    except OSError:
        return None
    return out.stdout.strip() or None


def _epsg(crs: dict[str, Any] | None) -> tuple[int | None, str | None]:
    if not crs:
        return None, None
    projjson = crs.get("projjson") or {}
    name = projjson.get("name")
    ident = projjson.get("id") or {}
    if ident.get("authority") == "EPSG":
        return int(ident["code"]), name
    ids = re.findall(r'ID\["EPSG",(\d+)\]', crs.get("wkt") or "")
    return (int(ids[-1]) if ids else None), name


def _run(ogrinfo: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ogrinfo, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_S,
        env=gdal_env(ogrinfo),
    )


def _dxf_layers(ogrinfo: str, path: Path, layer: str) -> dict[str, int]:
    sql = f'SELECT "Layer" AS cad_layer, COUNT(*) AS n FROM "{layer}" GROUP BY "Layer"'
    out = _run(ogrinfo, ["-ro", "-q", "-dialect", "SQLite", "-sql", sql, str(path)])
    counts: dict[str, int] = {}
    current: str | None = None
    for line in out.stdout.splitlines():
        m = re.match(r"\s*cad_layer \(String\) = (.*)$", line)
        if m:
            current = m.group(1).strip()
            continue
        m = re.match(r"\s*n \((?:Integer|Integer64)\) = (\d+)", line)
        if m and current is not None:
            counts[current] = int(m.group(1))
            current = None
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def inspect_gis(path: Path, ogrinfo: str | None) -> GisReport:
    data_hash = None
    size = 0
    if path.is_file():
        blob = path.read_bytes()
        data_hash = hashlib.sha256(blob).hexdigest()
        size = len(blob)
    report = GisReport(path=str(path), sha256=data_hash, size_bytes=size, driver=None)
    if ogrinfo is None:
        report.error = "GDAL ogrinfo not found (install GDAL or set OGRINFO)"
        return report
    out = _run(ogrinfo, ["-json", "-so", "-al", "-ro", str(path)])
    if out.returncode != 0:
        message = (out.stderr or out.stdout).strip().splitlines()
        report.error = message[-1] if message else f"ogrinfo exited {out.returncode}"
        if path.suffix.lower() == ".dwg":
            report.error += (
                " — GDAL's CAD driver reads DWG R2000 only; ask for a DXF export or convert with "
                "the ODA File Converter"
            )
        return report
    info = json.loads(out.stdout)
    report.driver = info.get("driverShortName")
    for layer in info.get("layers", []):
        geom = (layer.get("geometryFields") or [{}])[0]
        epsg, crs_name = _epsg(geom.get("coordinateSystem"))
        gl = GisLayer(
            name=layer.get("name", ""),
            geometry_type=geom.get("type"),
            features=layer.get("featureCount"),
            epsg=epsg,
            crs_name=crs_name,
            extent=geom.get("extent"),
            fields=[f.get("name", "") for f in layer.get("fields", [])],
        )
        if report.driver == "DXF" and "Layer" in gl.fields:
            gl.cad_layers = _dxf_layers(ogrinfo, path, gl.name)
        report.layers.append(gl)
    return report
