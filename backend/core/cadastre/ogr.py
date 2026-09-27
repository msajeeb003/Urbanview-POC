"""GDAL's ogr2ogr / ogrinfo for the cadastral exports.

ogr2ogr reads the export in whatever form it comes (Shapefile, zipped Shapefile, GeoPackage, GML,
DXF, CSV / XLSX for attribute exports, or a WFS layer), promotes polygons to multipolygons, drops
Z and reprojects to EPSG:4326 (with an explicit coordinate operation when the profile names one:
datum shifts matter at parcel scale), writing newline-delimited GeoJSON that the loader streams
into PostGIS. Loading through a file keeps the database credentials away from the GDAL command
line and works with any GDAL build (the portable bundle on Windows has no PostgreSQL driver).
Executables: ``$OGR2OGR`` / ``$OGRINFO``, the PATH, else the portable PostGIS bundle.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.gis.inspect_gis import _bundle_dirs, find_ogrinfo, gdal_env

TIMEOUT_SECONDS = 3600
TARGET_CRS = "EPSG:4326"


class OgrError(RuntimeError):
    """ogr2ogr / ogrinfo failed or is missing."""


def find_ogr2ogr(explicit: str | None = None) -> str | None:
    for candidate in (explicit, os.environ.get("OGR2OGR"), shutil.which("ogr2ogr")):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    for directory in _bundle_dirs():
        exe = directory / "ogr2ogr.exe"
        if exe.is_file():
            return str(exe)
    return None


def dataset_name(path: Path) -> str:
    """How GDAL opens a local export: a .zip through /vsizip/ (a zipped Shapefile, GML ...)."""
    text = str(path)
    if path.suffix.lower() == ".zip" and not text.lower().endswith(".shp.zip"):
        return "/vsizip/" + text.replace("\\", "/")
    return text


@dataclass(frozen=True, slots=True)
class OgrLayer:
    name: str
    feature_count: int | None
    geometry_type: str | None
    crs: str | None  # "EPSG:25834" when the CRS has an authority code, else its WKT name
    fields: tuple[str, ...] = field(default_factory=tuple)


def _crs_of(geometry_field: dict[str, Any]) -> str | None:
    system = geometry_field.get("coordinateSystem") or {}
    projjson = system.get("projjson") or {}
    ident = projjson.get("id") or {}
    if ident.get("authority") and ident.get("code"):
        return f"{ident['authority']}:{ident['code']}"
    if projjson.get("name"):
        return str(projjson["name"])
    wkt = system.get("wkt") or ""
    return wkt.split('"')[1] if wkt.count('"') >= 2 else None


class Ogr:
    """The two GDAL tools with the environment a portable bundle needs."""

    def __init__(self, ogr2ogr: str | None = None, ogrinfo: str | None = None) -> None:
        self.ogr2ogr = ogr2ogr or find_ogr2ogr()
        self.ogrinfo = ogrinfo or find_ogrinfo()
        if not self.ogr2ogr or not self.ogrinfo:
            raise OgrError(
                "GDAL's ogr2ogr / ogrinfo were not found: install gdal-bin (the worker image has "
                "it), put them on the PATH or set OGR2OGR / OGRINFO"
            )
        self.env = gdal_env(self.ogr2ogr)

    def _run(self, cmd: list[str], config: dict[str, str] | None = None) -> str:
        args = [cmd[0]]
        for key, value in (config or {}).items():
            args += ["--config", key, value]
        args += cmd[1:]
        proc = subprocess.run(  # noqa: S603 - fixed executable, arguments from the profile
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
            raise OgrError(f"{Path(cmd[0]).name} failed ({proc.returncode}): {tail}")
        return proc.stdout

    def layers(self, source: str) -> list[OgrLayer]:
        out = self._run([self.ogrinfo, "-json", "-so", "-ro", source])
        data = json.loads(out)
        layers = []
        for layer in data.get("layers", []):
            geoms = layer.get("geometryFields") or []
            first = geoms[0] if geoms else {}
            layers.append(
                OgrLayer(
                    name=layer.get("name", ""),
                    feature_count=layer.get("featureCount"),
                    geometry_type=first.get("type"),
                    crs=_crs_of(first) if first else None,
                    fields=tuple(f.get("name", "") for f in layer.get("fields", [])),
                )
            )
        return layers

    def to_geojsonseq(
        self,
        source: str,
        target: Path,
        *,
        layer: str | None = None,
        source_crs: str | None = None,
        transform: str | None = None,
        geometry: bool = True,
    ) -> list[str]:
        """Convert one layer to newline-delimited GeoJSON in EPSG:4326 (attributes only with
        ``geometry=False``). Returns the command (for the record, without the paths)."""
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        cmd = [self.ogr2ogr, "-f", "GeoJSONSeq", str(target), source]
        if layer:
            cmd.append(layer)
        options: list[str] = []
        if geometry:
            if source_crs:
                options += ["-s_srs", source_crs]
            if transform:
                options += ["-ct", transform]
            options += ["-t_srs", TARGET_CRS, "-nlt", "PROMOTE_TO_MULTI", "-dim", "XY"]
            options += ["-lco", "COORDINATE_PRECISION=9"]
        cmd += options
        self._run(cmd)
        return options

    def snapshot_wfs(self, url: str, typenames: list[str], target: Path, *, page_size: int) -> None:
        """Copy WFS layers into a local GeoPackage: the snapshot is what gets checksummed,
        stored and imported, so an import is reproducible."""
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        cmd = [self.ogr2ogr, "-f", "GPKG", str(target), f"WFS:{url}", *typenames]
        self._run(cmd, config={"OGR_WFS_PAGING_ALLOWED": "ON", "OGR_WFS_PAGE_SIZE": str(page_size)})


def read_features(path: Path) -> Iterator[tuple[dict[str, Any], str | None]]:
    """(properties, geometry as GeoJSON text or None) per feature of a GeoJSONSeq file."""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feature = json.loads(line)
            geometry = feature.get("geometry")
            yield (
                feature.get("properties") or {},
                json.dumps(geometry, separators=(",", ":")) if geometry else None,
            )
