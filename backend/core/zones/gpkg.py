"""A minimal GeoPackage reader and writer (OGC 12-128r18, sqlite3 + shapely), enough for the zone
working file: feature tables, attribute tables, spatial reference systems and QGIS default styles.

No GDAL: the zone tooling runs where only the backend's Python is installed. What QGIS needs is
here: the ``GPKG`` application id, ``gpkg_spatial_ref_sys`` / ``gpkg_contents`` /
``gpkg_geometry_columns``, an ``INTEGER PRIMARY KEY`` ``fid``, and geometry blobs in the
GeoPackage binary format (``GP`` header, srs id, envelope, ISO WKB). A table named
``layer_styles`` holds QGIS default styles (QML), which QGIS applies when a layer is added. No
R-tree index is written (QGIS offers to build one); files QGIS saved are read the same way.
"""

from __future__ import annotations

import sqlite3
import struct
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import shapely
from shapely.geometry.base import BaseGeometry

GPKG_APPLICATION_ID = 0x47504B47  # "GPKG"
GPKG_USER_VERSION = 10300  # 1.3.0


@dataclass(frozen=True, slots=True)
class Srs:
    srs_id: int
    name: str
    definition: str  # OGC WKT
    description: str = ""
    organization: str = "EPSG"
    proj4: str = ""

    @property
    def authid(self) -> str:
        return f"{self.organization}:{self.srs_id}"


# From PostGIS's spatial_ref_sys (srtext / proj4text), so a file can be written without a database.
SRS_DEFINITIONS: dict[int, Srs] = {
    4326: Srs(
        4326,
        "WGS 84",
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,'
        'AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,'
        'AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
        'AUTHORITY["EPSG","4326"]]',
        "longitude / latitude, WGS 84",
        proj4="+proj=longlat +datum=WGS84 +no_defs",
    ),
    25834: Srs(
        25834,
        "ETRS89 / UTM zone 34N",
        'PROJCS["ETRS89 / UTM zone 34N",GEOGCS["ETRS89",DATUM["European_Terrestrial_Reference_'
        'System_1989",SPHEROID["GRS 1980",6378137,298.257222101,AUTHORITY["EPSG","7019"]],'
        'TOWGS84[0,0,0,0,0,0,0],AUTHORITY["EPSG","6258"]],PRIMEM["Greenwich",0,'
        'AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
        'AUTHORITY["EPSG","4258"]],PROJECTION["Transverse_Mercator"],'
        'PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",21],'
        'PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],'
        'PARAMETER["false_northing",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
        'AXIS["Easting",EAST],AXIS["Northing",NORTH],AUTHORITY["EPSG","25834"]]',
        "ETRS89 / UTM zone 34N (metres)",
        proj4="+proj=utm +zone=34 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
    ),
    3857: Srs(
        3857,
        "WGS 84 / Pseudo-Mercator",
        'PROJCS["WGS 84 / Pseudo-Mercator",GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",'
        '6378137,298.257223563,AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],'
        'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,'
        'AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]],PROJECTION["Mercator_1SP"],'
        'PARAMETER["central_meridian",0],PARAMETER["scale_factor",1],'
        'PARAMETER["false_easting",0],PARAMETER["false_northing",0],'
        'UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["X",EAST],AXIS["Y",NORTH],'
        'AUTHORITY["EPSG","3857"]]',
        "web map tiles (the base map)",
        proj4="+proj=merc +a=6378137 +b=6378137 +lat_ts=0.0 +lon_0=0.0 +x_0=0.0 +y_0=0 +k=1.0 "
        "+units=m +nadgrids=@null +wktext +no_defs",
    ),
    3908: Srs(
        3908,
        "MGI 1901 / Balkans zone 6",
        'PROJCS["MGI 1901 / Balkans zone 6",GEOGCS["MGI 1901",DATUM["MGI_1901",'
        'SPHEROID["Bessel 1841",6377397.155,299.1528128,AUTHORITY["EPSG","7004"]],'
        'TOWGS84[682,-203,480,0,0,0,0],AUTHORITY["EPSG","1031"]],PRIMEM["Greenwich",0,'
        'AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
        'AUTHORITY["EPSG","3906"]],PROJECTION["Transverse_Mercator"],'
        'PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",18],'
        'PARAMETER["scale_factor",0.9999],PARAMETER["false_easting",6500000],'
        'PARAMETER["false_northing",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
        'AUTHORITY["EPSG","3908"]]',
        "MGI 1901 / Balkans zone 6 (the Podgorica plans' state system)",
        proj4="+proj=tmerc +lat_0=0 +lon_0=18 +k=0.9999 +x_0=6500000 +y_0=0 +ellps=bessel "
        "+towgs84=682,-203,480,0,0,0,0 +units=m +no_defs",
    ),
}

_SQL_TYPES = {
    "text": "TEXT",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "real": "REAL",
    "datetime": "DATETIME",
}
_ENVELOPE_BYTES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def _field(spec: Any) -> tuple[str, str]:
    if isinstance(spec, tuple):
        return spec[0], spec[1]
    return spec.name, spec.type


# --- geometry blobs -------------------------------------------------------------------------------


def encode_geometry(geom: BaseGeometry | None, srs_id: int) -> bytes | None:
    """GeoPackage binary: magic, version 0, flags (little endian, envelope [minx maxx miny maxy]
    or the empty flag), srs id, envelope, ISO WKB (2D)."""
    if geom is None:
        return None
    if geom.is_empty:
        flags = 0b0001_0001  # empty, little endian, no envelope
        head = struct.pack("<2sBBi", b"GP", 0, flags, srs_id)
        return head + shapely.to_wkb(geom, output_dimension=2, byte_order=1, flavor="iso")
    minx, miny, maxx, maxy = geom.bounds
    flags = 0b0000_0011  # envelope indicator 1, little endian
    head = struct.pack("<2sBBi4d", b"GP", 0, flags, srs_id, minx, maxx, miny, maxy)
    return head + shapely.to_wkb(geom, output_dimension=2, byte_order=1, flavor="iso")


def decode_geometry(blob: bytes | None) -> tuple[BaseGeometry | None, int | None]:
    if blob is None:
        return None, None
    blob = bytes(blob)
    if blob[:2] != b"GP":
        return shapely.from_wkb(blob), None  # plain WKB (some writers)
    flags = blob[3]
    little = flags & 0b1
    srs_id = struct.unpack("<i" if little else ">i", blob[4:8])[0]
    envelope = _ENVELOPE_BYTES.get((flags >> 1) & 0b111, 0)
    wkb = blob[8 + envelope :]
    geom = shapely.from_wkb(wkb)
    if (flags >> 4) & 0b1 and geom is not None and not geom.is_empty:
        geom = geom  # flagged empty but carries coordinates: trust the WKB
    return geom, srs_id


# --- writing --------------------------------------------------------------------------------------


def create(path: Path, *, overwrite: bool = False) -> sqlite3.Connection:
    """A new, empty GeoPackage with the mandatory tables and the default SRS rows."""
    if path.exists():
        if not overwrite:
            raise FileExistsError(f"{path} exists (pass overwrite=True to replace it)")
        path.unlink()
        for suffix in ("-wal", "-shm", "-journal"):
            side = path.with_name(path.name + suffix)
            if side.exists():
                side.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA application_id = {GPKG_APPLICATION_ID}")
    conn.execute(f"PRAGMA user_version = {GPKG_USER_VERSION}")
    conn.executescript(
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
            table_name TEXT NOT NULL, column_name TEXT NOT NULL,
            geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL,
            z TINYINT NOT NULL, m TINYINT NOT NULL,
            CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
            CONSTRAINT uk_gc_table_name UNIQUE (table_name),
            CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
            CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys (srs_id));
        """
    )
    conn.executemany(
        "INSERT INTO gpkg_spatial_ref_sys VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", "undefined cartesian"),
            ("Undefined geographic SRS", 0, "NONE", 0, "undefined", "undefined geographic"),
        ],
    )
    ensure_srs(conn, SRS_DEFINITIONS[4326])
    return conn


def srs_for(srs_id: int, definitions: Mapping[int, Srs] | None = None) -> Srs:
    table = {**SRS_DEFINITIONS, **(definitions or {})}
    if srs_id not in table:
        raise KeyError(f"no definition for EPSG:{srs_id}: add it to gpkg.SRS_DEFINITIONS")
    return table[srs_id]


def ensure_srs(conn: sqlite3.Connection, srs: Srs) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO gpkg_spatial_ref_sys VALUES (?, ?, ?, ?, ?, ?)",
        (srs.name, srs.srs_id, srs.organization, srs.srs_id, srs.definition, srs.description),
    )


def _columns(fields: Sequence[Any]) -> str:
    parts = []
    for spec in fields:
        name, kind = _field(spec)
        parts.append(f'"{name}" {_SQL_TYPES[kind]}')
    return ", ".join(parts)


def add_feature_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    fields: Sequence[Any],
    srs: Srs,
    geometry_type: str = "MULTIPOLYGON",
    geometry_column: str = "geom",
    identifier: str | None = None,
    description: str = "",
) -> None:
    ensure_srs(conn, srs)
    extra = _columns(fields)
    conn.execute(
        f'CREATE TABLE "{table}" (fid INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, '
        f'"{geometry_column}" {geometry_type}{", " + extra if extra else ""})'
    )
    conn.execute(
        "INSERT INTO gpkg_contents (table_name, data_type, identifier, description, last_change,"
        " srs_id) VALUES (?, 'features', ?, ?, ?, ?)",
        (table, identifier or table, description, _now(), srs.srs_id),
    )
    conn.execute(
        "INSERT INTO gpkg_geometry_columns VALUES (?, ?, ?, ?, 0, 0)",
        (table, geometry_column, geometry_type, srs.srs_id),
    )


def add_attribute_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    fields: Sequence[Any],
    identifier: str | None = None,
    description: str = "",
) -> None:
    conn.execute(
        f'CREATE TABLE "{table}" (fid INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, '
        f"{_columns(fields)})"
    )
    conn.execute(
        "INSERT INTO gpkg_contents (table_name, data_type, identifier, description, last_change)"
        " VALUES (?, 'attributes', ?, ?, ?)",
        (table, identifier or table, description, _now()),
    )


def _db_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    return value


def insert_features(
    conn: sqlite3.Connection,
    table: str,
    rows: Iterable[tuple[BaseGeometry | None, Mapping[str, Any]]],
    *,
    srs_id: int,
    geometry_column: str = "geom",
) -> int:
    count = 0
    bounds: list[float] | None = None
    for geom, props in rows:
        names = [geometry_column, *props.keys()]
        values = [encode_geometry(geom, srs_id), *(_db_value(v) for v in props.values())]
        marks = ", ".join("?" for _ in names)
        cols = ", ".join(f'"{n}"' for n in names)
        conn.execute(f'INSERT INTO "{table}" ({cols}) VALUES ({marks})', values)
        count += 1
        if geom is not None and not geom.is_empty:
            b = geom.bounds
            bounds = (
                list(b)
                if bounds is None
                else [
                    min(bounds[0], b[0]),
                    min(bounds[1], b[1]),
                    max(bounds[2], b[2]),
                    max(bounds[3], b[3]),
                ]
            )
    if bounds is not None:
        conn.execute(
            "UPDATE gpkg_contents SET min_x = ?, min_y = ?, max_x = ?, max_y = ?, last_change = ?"
            " WHERE table_name = ?",
            (*bounds, _now(), table),
        )
    return count


def insert_rows(conn: sqlite3.Connection, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    for props in rows:
        cols = ", ".join(f'"{n}"' for n in props)
        marks = ", ".join("?" for _ in props)
        conn.execute(
            f'INSERT INTO "{table}" ({cols}) VALUES ({marks})',
            [_db_value(v) for v in props.values()],
        )
        count += 1
    return count


def set_default_style(
    conn: sqlite3.Connection,
    table: str,
    qml: str,
    *,
    geometry_column: str = "",
    name: str = "default",
    description: str = "",
) -> None:
    """Store a QML as the layer's default style (QGIS's ``layer_styles`` convention)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS layer_styles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, f_table_catalog TEXT(256),
            f_table_schema TEXT(256), f_table_name TEXT(256), f_geometry_column TEXT(256),
            styleName TEXT(30), styleQML TEXT, styleSLD TEXT, useAsDefault BOOLEAN,
            description TEXT, owner TEXT(30), ui TEXT(30),
            update_time DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier, last_change)"
        " VALUES ('layer_styles', 'attributes', 'layer_styles', ?)",
        (_now(),),
    )
    conn.execute("DELETE FROM layer_styles WHERE f_table_name = ? AND styleName = ?", (table, name))
    conn.execute(
        "INSERT INTO layer_styles (f_table_catalog, f_table_schema, f_table_name,"
        " f_geometry_column, styleName, styleQML, styleSLD, useAsDefault, description, owner)"
        " VALUES ('', '', ?, ?, ?, ?, '', 1, ?, 'urbanview')",
        (table, geometry_column, name, qml, description),
    )


# --- reading --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Feature:
    fid: int
    geometry: BaseGeometry | None
    properties: dict[str, Any]


@dataclass(slots=True)
class Layer:
    name: str
    data_type: str  # features | attributes
    srs_id: int | None
    geometry_column: str | None
    geometry_type: str | None
    fields: list[str]
    features: list[Feature] = field(default_factory=list)


def list_layers(path: Path) -> list[tuple[str, str, int | None]]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return [
            (row[0], row[1], row[2])
            for row in conn.execute(
                "SELECT table_name, data_type, srs_id FROM gpkg_contents ORDER BY table_name"
            )
        ]


def read_layer(path: Path, table: str) -> Layer:
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        content = conn.execute(
            "SELECT data_type, srs_id FROM gpkg_contents WHERE table_name = ?", (table,)
        ).fetchone()
        if content is None:
            raise KeyError(f"{path} has no layer {table!r}")
        geometry = conn.execute(
            "SELECT column_name, geometry_type_name, srs_id FROM gpkg_geometry_columns"
            " WHERE table_name = ?",
            (table,),
        ).fetchone()
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        pk = next((row[1] for row in info if row[5]), "fid")
        geom_col = geometry[0] if geometry else None
        names = [row[1] for row in info if row[1] not in (pk, geom_col)]
        layer = Layer(
            name=table,
            data_type=content[0],
            srs_id=geometry[2] if geometry else content[1],
            geometry_column=geom_col,
            geometry_type=geometry[1] if geometry else None,
            fields=names,
        )
        select = ", ".join(f'"{c}"' for c in [pk, *([geom_col] if geom_col else []), *names])
        for row in conn.execute(f'SELECT {select} FROM "{table}" ORDER BY "{pk}"'):
            offset = 2 if geom_col else 1
            geom = decode_geometry(row[1])[0] if geom_col else None
            layer.features.append(
                Feature(
                    fid=row[0],
                    geometry=geom,
                    properties=dict(zip(names, row[offset:], strict=True)),
                )
            )
        return layer
    finally:
        conn.close()
