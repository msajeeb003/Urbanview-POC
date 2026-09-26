"""The QGIS working file for a zone session: the GeoPackage and the project (``template``).

Layers, all in the municipality's editing CRS (EPSG:25834 for Podgorica):

- ``zones``: the zone list to draw. Rows come from the previous round's export
  (``zones.geojson``, EPSG:4326, reprojected in PostGIS) when there is one, else the zone list
  (``zones.csv``: attributes, no geometry, so each listed zone is a row the client gives a shape
  with *Add Part*), with geometry taken over from the client's existing boundaries
  (``template.initial_zones``) by ``zone_id`` or name when configured;
- ``zone_documents``: the document list (``zone_documents.csv``);
- read-only reference layers from PostGIS, each when named in ``template.reference_layers``:
  cadastral municipalities (the cadastral parcels dissolved by KO), cadastral parcels,
  georeferenced planning-document coverage, and the zones currently in the database.

A GeoPackage that already holds drawn zones is the client's work: it is never overwritten
without ``overwrite`` (the safe path is ``import``, which exports ``zones.geojson``, then
``template`` again). Without a database the reference layers and the reprojection are skipped
with a warning.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import shapely
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.municipality import load_profile
from core.zones import gpkg, qgis
from core.zones.config import ZoneSetConfig
from core.zones.schema import (
    DOCUMENT_FIELDS,
    DOCUMENTS_LAYER,
    ZONE_FIELDS,
    ZONES_LAYER,
    FieldSpec,
    name_key,
    parse_bool,
)
from core.zones.seed import read_csv_rows


@dataclass(slots=True)
class TemplateResult:
    geopackage: Path
    project: Path
    layers: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReferenceLayer:
    name: str  # table in the GeoPackage
    title: str  # layer name in QGIS
    fields: tuple[tuple[str, str], ...]
    sql: str  # :m municipality, :crs editing CRS; columns = fields + wkb


REFERENCE_LAYERS: dict[str, ReferenceLayer] = {
    layer.name: layer
    for layer in (
        ReferenceLayer(
            "cadastral_municipalities",
            "Cadastral municipalities (KO)",
            (("ko_name", "text"), ("parcels", "integer")),
            """
            SELECT ko_name, count(*) AS parcels,
                   ST_AsBinary(ST_Transform(ST_Multi(ST_CollectionExtract(
                       ST_MakeValid(ST_Union(geom)), 3)), CAST(:crs AS integer))) AS wkb
            FROM cadastral_parcels WHERE municipality_id = :m
            GROUP BY ko_name ORDER BY ko_name
            """,
        ),
        ReferenceLayer(
            "cadastral_parcels",
            "Cadastral parcels",
            (
                ("parcel_id", "integer"),
                ("ko_name", "text"),
                ("parcel_number", "text"),
                ("sub_number", "text"),
                ("area_m2", "real"),
            ),
            """
            SELECT id AS parcel_id, ko_name, parcel_number, sub_number, area_m2,
                   ST_AsBinary(ST_Transform(geom, CAST(:crs AS integer))) AS wkb
            FROM cadastral_parcels WHERE municipality_id = :m ORDER BY id
            """,
        ),
        ReferenceLayer(
            "document_coverage",
            "Planning-document coverage",
            (
                ("document_id", "integer"),
                ("name", "text"),
                ("type", "text"),
                ("status", "text"),
                ("coverage_live", "boolean"),
                ("zone_id", "integer"),
            ),
            """
            SELECT id AS document_id, name, type, CAST(status AS text) AS status, coverage_live,
                   zone_id, ST_AsBinary(ST_Transform(coverage_geom, CAST(:crs AS integer))) AS wkb
            FROM planning_documents
            WHERE municipality_id = :m AND coverage_geom IS NOT NULL AND is_current_version
            ORDER BY ST_Area(coverage_geom) DESC
            """,
        ),
        ReferenceLayer(
            "current_zones",
            "Zones in the database",
            (
                ("zone_db_id", "integer"),
                ("zone_key", "text"),
                ("name", "text"),
                ("zone_type", "text"),
            ),
            """
            SELECT id AS zone_db_id, zone_key, name, zone_type,
                   ST_AsBinary(ST_Transform(geom, CAST(:crs AS integer))) AS wkb
            FROM zones WHERE municipality_id = :m ORDER BY name
            """,
        ),
    )
}
TRANSFORM_SQL = text(
    "SELECT ST_AsBinary(ST_Transform(ST_SetSRID(ST_GeomFromWKB(CAST(:wkb AS bytea)),"
    " CAST(:src AS integer)), CAST(:dst AS integer)))"
)
BOUNDS_SQL = text(
    "SELECT ST_XMin(g), ST_YMin(g), ST_XMax(g), ST_YMax(g) FROM (SELECT ST_Transform("
    "ST_MakeEnvelope(:xmin, :ymin, :xmax, :ymax, 4326), CAST(:crs AS integer)) AS g) x"
)


# --- values ---------------------------------------------------------------------------------------


def multipolygon(geom: BaseGeometry | None) -> MultiPolygon | None:
    if geom is None or geom.is_empty:
        return None
    geom = shapely.force_2d(geom)
    if isinstance(geom, MultiPolygon):
        return geom
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    parts = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]
    parts += [p for g in getattr(geom, "geoms", []) if isinstance(g, MultiPolygon) for p in g.geoms]
    return MultiPolygon(parts) if parts else None


def typed(spec: FieldSpec, raw: Any) -> Any:
    """A CSV / GeoJSON value typed for its GeoPackage column (blank = NULL)."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    if spec.type == "boolean":
        try:
            return parse_bool(raw)
        except ValueError:
            return None
    if spec.type == "integer":
        try:
            return int(str(raw).strip())
        except ValueError:
            return None
    if spec.type == "date":
        text_value = str(raw).strip()
        try:
            return date.fromisoformat(text_value[:10]).isoformat()
        except ValueError:
            return text_value  # kept as written; validation reports it
    return str(raw).strip() if isinstance(raw, str) else raw


def _row(fields: Sequence[FieldSpec], values: dict[str, Any]) -> dict[str, Any]:
    return {spec.name: typed(spec, values.get(spec.name)) for spec in fields}


def _geojson_features(path: Path) -> tuple[list[tuple[BaseGeometry | None, dict]], int]:
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    name = ((doc.get("crs") or {}).get("properties") or {}).get("name", "EPSG:4326")
    srs = int(name.replace("::", ":").rsplit(":", 1)[-1])
    features = doc.get("features", [doc] if doc.get("type") == "Feature" else [])
    return [
        (shape(f["geometry"]) if f.get("geometry") else None, dict(f.get("properties") or {}))
        for f in features
    ], srs


async def _transform(session: AsyncSession, geom: BaseGeometry, src: int, dst: int) -> BaseGeometry:
    if src == dst:
        return geom
    wkb = (
        await session.execute(
            TRANSFORM_SQL, {"wkb": shapely.to_wkb(geom, output_dimension=2), "src": src, "dst": dst}
        )
    ).scalar_one()
    return shapely.from_wkb(bytes(wkb))


# --- zones ----------------------------------------------------------------------------------------


async def zone_rows(
    cfg: ZoneSetConfig, session: AsyncSession | None, warnings: list[str]
) -> list[tuple[MultiPolygon | None, dict[str, Any]]]:
    """The zones layer's rows: the previous export, else the zone list (+ initial boundaries)."""
    crs = cfg.editing_crs
    if cfg.zones_export.is_file():
        features, srs = _geojson_features(cfg.zones_export)
        if session is not None or srs == crs:
            rows = []
            for geom, props in features:
                if geom is not None and session is not None:
                    geom = await _transform(session, geom, srs, crs)
                rows.append((multipolygon(geom), _row(ZONE_FIELDS, props)))
            return rows
        warnings.append(
            f"{cfg.zones_export.name} is in EPSG:{srs} and reprojecting it needs the database: "
            "starting from the zone list instead"
        )
    rows = []
    if cfg.zones_seed.is_file():
        rows = [(None, _row(ZONE_FIELDS, r)) for r in read_csv_rows(cfg.zones_seed)]
    else:
        warnings.append(f"no zone list at {cfg.zones_seed.name}: run `seed` first")
    initial = cfg.template.initial_zones
    if initial is None:
        return rows
    if not initial.is_file():
        warnings.append(f"initial_zones {initial} does not exist")
        return rows
    if initial.suffix.lower() == ".gpkg":
        layer = gpkg.read_layer(initial, gpkg.list_layers(initial)[0][0])
        features = [(f.geometry, f.properties) for f in layer.features]
        srs = layer.srs_id or crs
    else:
        features, srs = _geojson_features(initial)
    if srs != crs and session is None:
        warnings.append(f"{initial.name} is in EPSG:{srs}: reprojecting needs the database")
        return rows
    by_id = {r["zone_id"]: i for i, (_, r) in enumerate(rows) if r.get("zone_id")}
    by_name = {name_key(r["name"]): i for i, (_, r) in enumerate(rows) if r.get("name")}
    matched = 0
    for geom, props in features:
        if geom is None:
            continue
        index = by_id.get(str(props.get("zone_id") or ""))
        if index is None:
            index = by_name.get(name_key(str(props.get("name") or props.get("NAME") or "")))
        if index is None:
            continue
        if session is not None:
            geom = await _transform(session, geom, srs, crs)
        rows[index] = (multipolygon(geom), rows[index][1])
        matched += 1
    warnings.append(f"initial boundaries: {matched} of {len(features)} matched to the zone list")
    return rows


def document_rows(cfg: ZoneSetConfig, warnings: list[str]) -> list[dict[str, Any]]:
    if not cfg.documents.is_file():
        warnings.append(f"no document list at {cfg.documents.name}: run `seed` first")
        return []
    return [_row(DOCUMENT_FIELDS, r) for r in read_csv_rows(cfg.documents)]


def has_drawn_zones(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            count = conn.execute(
                f'SELECT count(*) FROM "{ZONES_LAYER}" WHERE geom IS NOT NULL'
            ).fetchone()
    except sqlite3.Error:
        return False
    return bool(count and count[0])


# --- the build ------------------------------------------------------------------------------------


async def _reference(
    session: AsyncSession, layer: ReferenceLayer, municipality_id: str, crs: int
) -> list[tuple[MultiPolygon | None, dict[str, Any]]]:
    rows = (await session.execute(text(layer.sql), {"m": municipality_id, "crs": crs})).mappings()
    out = []
    for row in rows:
        wkb = row["wkb"]
        geom = multipolygon(shapely.from_wkb(bytes(wkb))) if wkb is not None else None
        out.append((geom, {name: row[name] for name, _ in layer.fields}))
    return out


def _bounds(rows: Sequence[tuple[BaseGeometry | None, Any]]) -> tuple[float, ...] | None:
    geoms = [g for g, _ in rows if g is not None and not g.is_empty]
    if not geoms:
        return None
    return tuple(shapely.union_all(geoms).bounds)


def _pad(extent: tuple[float, ...] | None, share: float = 0.05) -> tuple[float, ...] | None:
    if extent is None:
        return None
    xmin, ymin, xmax, ymax = extent
    dx, dy = (xmax - xmin) * share or 100, (ymax - ymin) * share or 100
    return (xmin - dx, ymin - dy, xmax + dx, ymax + dy)


async def build_template(
    cfg: ZoneSetConfig, *, session: AsyncSession | None, overwrite: bool = False
) -> TemplateResult:
    if has_drawn_zones(cfg.geopackage) and not overwrite:
        raise FileExistsError(
            f"{cfg.geopackage.name} already holds drawn zones: run `import` (it exports them to "
            f"{cfg.zones_export.name}) and then `template` again, or pass --force to discard them"
        )
    result = TemplateResult(geopackage=cfg.geopackage, project=cfg.project)
    warnings = result.warnings
    crs = cfg.editing_crs
    srs = gpkg.srs_for(crs)
    terminology = load_profile(cfg.municipality_id).terminology
    types_en = dict(terminology.document_types_en or {})
    document_types = {code: types_en.get(code, "") for code in terminology.document_types}

    zones = await zone_rows(cfg, session, warnings)
    documents = document_rows(cfg, warnings)
    references: dict[str, list] = {}
    if session is None:
        if cfg.template.reference_layers:
            warnings.append("no database: reference layers skipped")
    else:
        for name in cfg.template.reference_layers:
            layer = REFERENCE_LAYERS.get(name)
            if layer is None:
                warnings.append(f"unknown reference layer {name!r} in zones.toml")
                continue
            references[name] = await _reference(session, layer, cfg.municipality_id, crs)

    tmp = cfg.geopackage.with_name(cfg.geopackage.name + ".tmp")
    conn = gpkg.create(tmp, overwrite=True)
    try:
        gpkg.add_feature_table(
            conn,
            ZONES_LAYER,
            fields=ZONE_FIELDS,
            srs=srs,
            description="UrbanView zones (draw here)",
        )
        gpkg.insert_features(conn, ZONES_LAYER, zones, srs_id=crs)
        gpkg.add_attribute_table(
            conn, DOCUMENTS_LAYER, fields=DOCUMENT_FIELDS, description="planning documents per zone"
        )
        gpkg.insert_rows(conn, DOCUMENTS_LAYER, documents)
        for name, rows in references.items():
            layer = REFERENCE_LAYERS[name]
            gpkg.add_feature_table(
                conn,
                name,
                fields=layer.fields,
                srs=srs,
                description=f"reference (read-only): {layer.title}",
            )
            gpkg.insert_features(conn, name, rows, srs_id=crs)
        gpkg.set_default_style(
            conn,
            ZONES_LAYER,
            qgis.qml(qgis.zone_style_elements(), labels=True),
            geometry_column="geom",
        )
        gpkg.set_default_style(
            conn, DOCUMENTS_LAYER, qgis.qml(qgis.documents_style_elements(document_types))
        )
        for name in references:
            gpkg.set_default_style(
                conn, name, qgis.qml(qgis.reference_style_elements(name)), geometry_column="geom"
            )
        conn.commit()
    finally:
        conn.close()
    tmp.replace(cfg.geopackage)
    result.layers = {
        ZONES_LAYER: len(zones),
        DOCUMENTS_LAYER: len(documents),
        **{name: len(rows) for name, rows in references.items()},
    }
    drawn = sum(1 for g, _ in zones if g is not None)
    if zones and not drawn:
        warnings.append(f"{len(zones)} zones listed, none drawn yet")

    extent = _bounds(zones) or next((_bounds(r) for r in references.values() if _bounds(r)), None)
    if extent is None and session is not None:
        bounds = load_profile(cfg.municipality_id).bounds
        row = (
            await session.execute(
                BOUNDS_SQL,
                {
                    "xmin": bounds[0],
                    "ymin": bounds[1],
                    "xmax": bounds[2],
                    "ymax": bounds[3],
                    "crs": crs,
                },
            )
        ).one()
        extent = tuple(float(v) for v in row)
    extent = _pad(extent)

    source = qgis.gpkg_source
    zones_layer = qgis.ProjectLayer(
        qgis.layer_id("zones"),
        "Zones",
        "vector",
        source(cfg.geopackage, ZONES_LAYER),
        "ogr",
        crs,
        "Polygon",
        labels=True,
        extent=_bounds(zones),
    )
    documents_layer = qgis.ProjectLayer(
        qgis.layer_id("zone_documents"),
        "Zone documents",
        "vector",
        source(cfg.geopackage, DOCUMENTS_LAYER),
        "ogr",
        crs,
        None,
    )
    zones_layer.style = qgis.zone_style_elements()
    documents_layer.style = qgis.documents_style_elements(document_types, zones_layer.ref)
    layers = [zones_layer, documents_layer]
    for name in (
        "document_coverage",
        "current_zones",
        "cadastral_parcels",
        "cadastral_municipalities",
    ):
        if name in references:
            layers.append(
                qgis.ProjectLayer(
                    qgis.layer_id(name),
                    REFERENCE_LAYERS[name].title,
                    "vector",
                    source(cfg.geopackage, name),
                    "ogr",
                    crs,
                    "Polygon",
                    style=qgis.reference_style_elements(name),
                    read_only=True,
                    labels=name in ("cadastral_municipalities", "current_zones"),
                    visible=name != "cadastral_parcels" or len(references[name]) < 50_000,
                    extent=_bounds(references[name]),
                )
            )
    layers.append(
        qgis.ProjectLayer(
            qgis.layer_id("basemap"),
            cfg.template.basemap_name,
            "raster",
            qgis.xyz_source(cfg.template.basemap_url, cfg.template.basemap_zmax),
            "wms",
            3857,
            None,
        )
    )
    xml = qgis.project_xml(
        title=cfg.title, layers=layers, srs_id=crs, extent=extent, paper=cfg.template.layout_paper
    )
    qgis.write_qgz(cfg.project, xml)
    return result
