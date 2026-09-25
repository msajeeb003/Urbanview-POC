"""GeoPackage output, written natively with sqlite3 (GDAL and QGIS read it; no GDAL needed here).

One GeoPackage per document with the layers ``plan_boundary``, ``urban_parcels``,
``urban_blocks``, ``planned_land_use`` and ``planned_traffic``: MULTIPOLYGON or MULTILINESTRING
in the document's local frame (ground metres, not georeferenced, so ``srs_id = -1``, the
GeoPackage's undefined Cartesian system). One row per feature with its key, provenance
(document, sheet, page, raw path ids, source bbox in the sheet's PDF points), label and
``qa_flags`` (comma-separated). The same layers and columns are the contract for sheets redrawn
by hand in QGIS (``read_gpkg`` imports them).
"""

from __future__ import annotations

import json
import sqlite3
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import shapely
from shapely.geometry.base import BaseGeometry

from core.gis.extract.extract import Extraction
from core.gis.extract.rules import LINE_LAYERS, TARGET_LAYERS

APPLICATION_ID = 0x47504B47  # "GPKG"
USER_VERSION = 10300  # GeoPackage 1.3.0
LOCAL_SRS_ID = -1

COMMON_COLUMNS: tuple[tuple[str, str], ...] = (
    ("feature_key", "TEXT NOT NULL"),
    ("document_id", "INTEGER"),
    ("document_ref", "TEXT"),
    ("sheet", "TEXT"),
    ("page", "INTEGER"),
    ("source_paths", "TEXT"),  # JSON list of raw path ids ("p{page}:{seqno}")
    ("source_bbox", "TEXT"),  # JSON [x0, y0, x1, y1], sheet PDF points, origin bottom-left
    ("label_text", "TEXT"),
    ("label_bbox", "TEXT"),
    ("qa_flags", "TEXT"),
)
LAYER_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "plan_boundary": (("area_m2", "REAL"),),
    "urban_parcels": (("urban_parcel_number", "TEXT"), ("block_ref", "TEXT"), ("area_m2", "REAL")),
    "urban_blocks": (("block_ref", "TEXT"), ("area_m2", "REAL")),
    "planned_land_use": (
        ("code", "TEXT"),
        ("name", "TEXT"),
        ("share", "REAL"),
        ("urban_parcel_number", "TEXT"),
        ("area_m2", "REAL"),
    ),
    "planned_traffic": (("road_class", "TEXT"), ("name", "TEXT"), ("length_m", "REAL")),
}
JSON_COLUMNS = {"source_paths", "source_bbox", "label_bbox"}

_SRS_ROWS = (
    (
        "Undefined cartesian SRS",
        -1,
        "NONE",
        -1,
        "undefined",
        "undefined cartesian coordinate reference system",
    ),
    (
        "Undefined geographic SRS",
        0,
        "NONE",
        0,
        "undefined",
        "undefined geographic coordinate reference system",
    ),
    (
        "WGS 84 geodetic",
        4326,
        "EPSG",
        4326,
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,'
        'AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,'
        'AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
        'AUTHORITY["EPSG","4326"]]',
        "longitude/latitude coordinates in decimal degrees on the WGS 84 spheroid",
    ),
)

_ENVELOPE_BYTES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


def gpkg_blob(geom: BaseGeometry, srs_id: int = LOCAL_SRS_ID) -> bytes:
    """GeoPackage binary: header (magic, version, flags, srs id, xy envelope) + WKB."""
    if geom.is_empty:
        return (
            b"GP"
            + bytes([0, 0b00010001])
            + struct.pack("<i", srs_id)
            + shapely.to_wkb(geom, byte_order=1)
        )
    minx, miny, maxx, maxy = geom.bounds
    flags = (1 << 1) | 1  # xy envelope, little endian
    header = (
        b"GP"
        + bytes([0, flags])
        + struct.pack("<i", srs_id)
        + struct.pack("<4d", minx, maxx, miny, maxy)
    )
    return header + shapely.to_wkb(geom, byte_order=1, output_dimension=2)


def parse_blob(blob: bytes) -> BaseGeometry:
    if blob[:2] != b"GP":
        raise ValueError("not a GeoPackage geometry")
    flags = blob[3]
    envelope = _ENVELOPE_BYTES.get((flags >> 1) & 0b111)
    if envelope is None:
        raise ValueError(f"bad envelope indicator in flags {flags:#x}")
    return shapely.from_wkb(blob[8 + envelope :])


def _timestamp(when: datetime | None) -> str:
    when = (when or datetime.now(UTC)).astimezone(UTC)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def feature_rows(extraction: Extraction) -> dict[str, list[tuple[BaseGeometry, dict[str, Any]]]]:
    doc_ref = extraction.rules.document.id
    return {
        layer: [(f.geom, f.properties(extraction.document_id, doc_ref)) for f in feats]
        for layer, feats in extraction.layers.items()
    }


def write_gpkg(
    path: Path,
    layers: dict[str, list[tuple[BaseGeometry, dict[str, Any]]]],
    *,
    description: str = "",
    last_change: datetime | None = None,
) -> dict[str, int]:
    """Write the layers (every target layer, empty ones too, so the file is a complete
    template for a manual redraw). Returns the row count per layer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    stamp = _timestamp(last_change)
    con = sqlite3.connect(path)
    try:
        con.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        con.execute(f"PRAGMA user_version = {USER_VERSION}")
        con.executescript(
            """
            CREATE TABLE gpkg_spatial_ref_sys (
              srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
              organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL,
              definition TEXT NOT NULL, description TEXT);
            CREATE TABLE gpkg_contents (
              table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL,
              identifier TEXT UNIQUE, description TEXT DEFAULT '',
              last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
              min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER,
              CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id)
                REFERENCES gpkg_spatial_ref_sys(srs_id));
            CREATE TABLE gpkg_geometry_columns (
              table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL,
              srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL,
              CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
              CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
              CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys (srs_id));
            """
        )
        con.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?, ?, ?, ?, ?, ?)", _SRS_ROWS)
        counts: dict[str, int] = {}
        for layer in TARGET_LAYERS:
            rows = layers.get(layer, [])
            gtype = "MULTILINESTRING" if layer in LINE_LAYERS else "MULTIPOLYGON"
            columns = COMMON_COLUMNS + LAYER_COLUMNS[layer]
            ddl = ", ".join(f'"{name}" {kind}' for name, kind in columns)
            con.execute(
                f'CREATE TABLE "{layer}" '
                f"(fid INTEGER PRIMARY KEY AUTOINCREMENT, geom {gtype}, {ddl})"
            )
            names = [name for name, _ in columns]
            quoted = ", ".join(f'"{n}"' for n in names)
            marks = ", ".join("?" for _ in names)
            sql = f'INSERT INTO "{layer}" (geom, {quoted}) VALUES (?, {marks})'
            bounds = None
            for geom, props in rows:
                values = []
                for n in names:
                    v = props.get(n)
                    if n in JSON_COLUMNS and v is not None:
                        v = json.dumps(v)
                    elif n == "qa_flags":
                        v = ",".join(v or [])
                    values.append(v)
                con.execute(sql, [gpkg_blob(geom), *values])
                b = geom.bounds
                if bounds is None:
                    bounds = b
                else:
                    bounds = (
                        min(bounds[0], b[0]),
                        min(bounds[1], b[1]),
                        max(bounds[2], b[2]),
                        max(bounds[3], b[3]),
                    )
            con.execute(
                "INSERT INTO gpkg_contents (table_name, data_type, identifier, description, "
                "last_change, min_x, min_y, max_x, max_y, srs_id) "
                "VALUES (?, 'features', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    layer,
                    layer,
                    description,
                    stamp,
                    *(bounds or (None, None, None, None)),
                    LOCAL_SRS_ID,
                ),
            )
            con.execute(
                "INSERT INTO gpkg_geometry_columns VALUES (?, 'geom', ?, ?, 0, 0)",
                (layer, gtype, LOCAL_SRS_ID),
            )
            counts[layer] = len(rows)
        con.commit()
    finally:
        con.close()
    return counts


def read_gpkg(path: Path) -> dict[str, list[tuple[BaseGeometry, dict[str, Any]]]]:
    """Every feature table of a GeoPackage (ours, or one edited / redrawn in QGIS)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = con.execute(
            "SELECT c.table_name, g.column_name FROM gpkg_contents c "
            "JOIN gpkg_geometry_columns g ON g.table_name = c.table_name "
            "WHERE c.data_type = 'features' ORDER BY c.table_name"
        ).fetchall()
        out: dict[str, list[tuple[BaseGeometry, dict[str, Any]]]] = {}
        for table, geom_col in tables:
            cur = con.execute(f'SELECT * FROM "{table}" ORDER BY 1')
            names = [d[0] for d in cur.description]
            rows = []
            for rec in cur:
                props = dict(zip(names, rec, strict=True))
                blob = props.pop(geom_col)
                if blob is None:
                    continue
                for n in JSON_COLUMNS:
                    if isinstance(props.get(n), str):
                        props[n] = json.loads(props[n])
                if isinstance(props.get("qa_flags"), str):
                    props["qa_flags"] = [f for f in props["qa_flags"].split(",") if f]
                rows.append((parse_blob(blob), props))
            out[table] = rows
        return out
    finally:
        con.close()
