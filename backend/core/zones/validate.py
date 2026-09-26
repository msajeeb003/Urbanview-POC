"""Reading and checking the QGIS outputs before anything is imported: the ``zones`` polygons and
the ``zone_documents`` list (``core.zones.schema``).

The import refuses a dataset with errors, so every rule the product relies on is checked here:

- zones: a polygonal, valid, non-empty geometry; a stable slug id and a name, both unique; a zone
  type from the map palette (missing = drawn neutral, a warning);
- the partition: no two zones overlap and there are no holes inside the zones' union (plus, when
  the configuration names an extent such as the POC area, nothing of it left uncovered). Slivers up
  to the configured tolerance (m²) are digitising noise: warnings, not errors;
- documents: every row in a known zone, with a name, a document type of the municipality profile
  and a status; each document in exactly one row (``schema.document_identity``: the eRegistri id,
  else the folded name plus the listed year), dates that parse and are not in the future;
- zones and documents together: every zone has at least one adopted document unless it is
  explicitly marked ``no_adopted_plan`` (the acceptance rule "each zone has >= 1 document listed,
  or is explicitly marked no adopted plan").

Reading never raises for a bad cell: a value that does not parse becomes a :class:`Problem` on its
record and the value is left empty, so the client's session gets the whole list at once. Only a
file that cannot be read as a dataset (wrong format, missing layer, missing required columns)
raises ``ValueError``.

Areas are square metres. A projected CRS (the editing CRS, e.g. EPSG:25834 or EPSG:3908) is taken
as metric and its planar areas are used as they are. A geographic CRS (:data:`GEOGRAPHIC_SRS`) is
scaled at each geometry's centroid latitude by ``111 320 * cos(lat)`` m per degree of longitude
and ``110 574`` m per degree of latitude: an equirectangular approximation, good to well under a
percent over a city, which is all a sliver tolerance needs. No pyproj: this runs where only the
backend's Python is installed.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import shapely
from shapely.errors import ShapelyError
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry

from core.zones import gpkg, schema
from core.zones.config import ValidationConfig

Severity = Literal["error", "warning"]

# Geographic CRSs (degrees) the tolerances must be scaled for: WGS 84, ETRS89, GRS 1980 and the
# GeoPackage "undefined geographic" id 0. Anything else is treated as metric: the zone tooling
# only ever sees WGS 84 (GeoJSON) or the municipality's projected editing / state CRS.
GEOGRAPHIC_SRS = frozenset({0, 4019, 4258, 4326})
# GeoPackage's "undefined cartesian" (-1) and "undefined geographic" (0): a layer QGIS saved
# without a CRS. The import cannot reproject it, so it is an error.
UNDEFINED_SRS = frozenset({-1, 0})

M_PER_DEG_LON_AT_EQUATOR = 111_320.0
M_PER_DEG_LAT = 110_574.0

# Below this an overlap or hole is floating-point noise from the union / intersection (shared
# vertices that differ in the last digits), not something drawn: it is not reported at all. The
# configured tolerances decide between warning and error above it.
NEGLIGIBLE_M2 = 1e-4

# How the national registry (eRegistri) marks a plan it no longer considers valid, in its
# free-text note. The CLI passes the configured markers (zones.toml [eregistri] invalid_markers);
# these are the defaults for Montenegro's registry.
DEFAULT_INVALID_MARKERS: tuple[str, ...] = ("NEVAŽEĆI", "NIJE VAŽEĆI")

# Read-time geometry problems that already explain a missing geometry.
_GEOMETRY_READ_CODES = frozenset({"zone_not_polygon", "zone_invalid_geometry"})
_VALIDITY_POINT = re.compile(r"\[\s*([-+\d.eE]+)\s+([-+\d.eE]+)\s*\]")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?$")
_DOTTED_DATE = re.compile(r"^(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\.?$")
_EPSG = re.compile(r"EPSG:{1,2}(?:[\d.]*:)?(\d+)$", re.IGNORECASE)
_SQLITE_MAGIC = b"SQLite format 3\x00"


# --- records --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Problem:
    severity: Severity
    code: str  # part of the API: the checks below name them
    message: str
    zone_id: str | None = None
    document: str | None = None  # the document's label, e.g. "row 12: DUP Momišići C"
    area_m2: float | None = None
    location: tuple[float, float] | None = None  # a point on it, in the dataset CRS
    # Every zone involved when there are several (an overlapping pair, the zones around a gap,
    # duplicated ids); ``zone_id`` is then the first of them.
    zone_ids: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "zone_id": self.zone_id,
            "zone_ids": list(self.zone_ids),
            "document": self.document,
            "area_m2": self.area_m2,
            "location": list(self.location) if self.location else None,
        }


@dataclass(slots=True)
class ZoneRecord:
    fid: int
    zone_id: str
    name: str
    zone_type: str  # the QGIS value ("residential" ...), lower-cased; "" when not set
    general_planning_summary: str
    notes: str
    no_adopted_plan: bool
    # Polygon or MultiPolygon as drawn (2D), or None when missing or not polygonal (the reason is
    # in ``problems``). Validity is not repaired here: ``validate`` reports it.
    geometry: BaseGeometry | None
    problems: list[Problem] = field(default_factory=list)  # found while reading

    @property
    def label(self) -> str:
        return self.zone_id or f"fid {self.fid}"


@dataclass(slots=True)
class DocumentRecord:
    row: int  # 1-based position among the file's non-blank data rows
    # Every schema.DOCUMENT_FIELDS name, typed: text stripped ("" when empty; ``status`` lower-
    # cased), booleans (``confirmed``, ``poc_coverage``; empty = False), ``listed_year`` int | None,
    # ``adoption_date`` date | None. A cell that did not parse is empty and has a problem.
    values: dict[str, Any]
    fid: int | None = None  # the GeoPackage row id, when read from one
    problems: list[Problem] = field(default_factory=list)  # found while reading

    @property
    def zone_id(self) -> str:
        return self.values["zone_id"]

    @property
    def status(self) -> str:
        return self.values["status"]

    @property
    def label(self) -> str:
        return f"row {self.row}: {self.values['document_name'] or '(no name)'}"


@dataclass(slots=True)
class ZoneDataset:
    zones: list[ZoneRecord]
    documents: list[DocumentRecord]
    srs_id: int  # of the zones layer (4326 for GeoJSON without a crs member)
    zones_source: Path
    documents_source: Path


@dataclass(slots=True)
class ValidationReport:
    problems: list[Problem]
    stats: dict[str, Any]

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.severity == "error"]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if p.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self, severity: Severity | None = None) -> Counter[str]:
        return Counter(p.code for p in self.problems if severity in (None, p.severity))

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "stats": self.stats,
            "problems": [p.to_json() for p in self.problems],
        }

    def to_markdown(self) -> str:
        s = self.stats
        head = (
            f"**Validation: {len(self.errors)} error(s), {len(self.warnings)} warning(s)** "
            f"({s.get('zones', 0)} zones, {s.get('documents', 0)} documents, "
            f"EPSG:{s.get('srs_id')})"
        )
        if not self.problems:
            return head + "\n\nNo problems found.\n"
        lines = [
            head,
            "",
            "| Severity | Check | Zone | Document | Detail |",
            "|---|---|---|---|---|",
        ]
        for p in self.problems:
            zones = " / ".join(p.zone_ids) if p.zone_ids else (p.zone_id or "")
            detail = p.message
            if p.location:
                detail += f" (at {p.location[0]}, {p.location[1]})"
            cells = [p.severity, f"`{p.code}`", zones, p.document or "", detail]
            lines.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
        return "\n".join(lines) + "\n"


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


# --- reading --------------------------------------------------------------------------------------


def read_dataset(
    zones_path: Path,
    documents_path: Path,
    *,
    zones_layer: str = schema.ZONES_LAYER,
    documents_layer: str = schema.DOCUMENTS_LAYER,
) -> ZoneDataset:
    """The QGIS outputs as records: zones from a GeoPackage layer or a GeoJSON FeatureCollection,
    documents from a GeoPackage table or a CSV. Both may be the same GeoPackage."""
    zones_path, documents_path = Path(zones_path), Path(documents_path)
    if _is_geopackage(zones_path):
        zones, srs_id = _read_zones_gpkg(zones_path, zones_layer)
    elif zones_path.suffix.lower() in (".geojson", ".json"):
        zones, srs_id = _read_zones_geojson(zones_path)
    else:
        raise ValueError(f"{zones_path}: zones must be a GeoPackage (.gpkg) or GeoJSON file")
    if _is_geopackage(documents_path):
        documents = _read_documents_gpkg(documents_path, documents_layer)
    elif documents_path.suffix.lower() in (".csv", ".txt"):
        documents = _read_documents_csv(documents_path)
    else:
        raise ValueError(f"{documents_path}: documents must be a GeoPackage table or a CSV file")
    return ZoneDataset(zones, documents, srs_id, zones_path, documents_path)


def read_extent(path: Path, *, layer: str | None = None) -> tuple[BaseGeometry, int]:
    """An extent polygon (e.g. the POC area) and its srs id, from GeoJSON (a FeatureCollection, a
    Feature or a bare geometry) or a GeoPackage feature layer (``layer``, else the first one). All
    polygonal parts are merged. ``validate`` needs it in the dataset's CRS."""
    path = Path(path)
    if _is_geopackage(path):
        if layer is None:
            names = [n for n, kind, _ in gpkg.list_layers(path) if kind == "features"]
            if not names:
                raise ValueError(f"{path}: no feature layer")
            layer = names[0]
        data = gpkg.read_layer(path, layer)
        geoms = [f.geometry for f in data.features if f.geometry is not None]
        srs_id = data.srs_id if data.srs_id is not None else -1
    else:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        srs_id = _geojson_srs(doc, path)
        if doc.get("type") == "FeatureCollection":
            geoms = [shape(f["geometry"]) for f in doc.get("features", []) if f.get("geometry")]
        elif doc.get("type") == "Feature":
            geoms = [shape(doc["geometry"])] if doc.get("geometry") else []
        else:
            geoms = [shape(doc)]
    parts = [p for g in geoms for p in _polygon_parts(shapely.make_valid(shapely.force_2d(g)))]
    if not parts:
        raise ValueError(f"{path}: no polygon in the extent")
    return shapely.union_all(parts), srs_id


def _is_geopackage(path: Path) -> bool:
    if path.suffix.lower() == ".gpkg":
        return True
    try:
        with path.open("rb") as fh:
            return fh.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def _missing_columns(path: Path, columns: Iterable[str], specs: Iterable[schema.FieldSpec]) -> None:
    present = set(columns)
    missing = [s.name for s in specs if s.required and s.name not in present]
    if missing:
        raise ValueError(f"{path}: missing required column(s): {', '.join(missing)}")


def _read_gpkg_layer(path: Path, table: str) -> gpkg.Layer:
    try:
        return gpkg.read_layer(path, table)
    except KeyError:
        layers = ", ".join(name for name, _, _ in gpkg.list_layers(path)) or "none"
        raise ValueError(f"{path}: no layer {table!r} (layers: {layers})") from None


def _read_zones_gpkg(path: Path, table: str) -> tuple[list[ZoneRecord], int]:
    layer = _read_gpkg_layer(path, table)
    if layer.geometry_column is None:
        raise ValueError(f"{path}: layer {table!r} has no geometry column")
    _missing_columns(path, layer.fields, schema.ZONE_FIELDS)
    zones = [_zone_record(f.fid, f.geometry, f.properties) for f in layer.features]
    return zones, layer.srs_id if layer.srs_id is not None else -1


def _geojson_srs(doc: Mapping[str, Any], path: Path) -> int:
    """GeoJSON is WGS 84 (RFC 7946); the pre-RFC ``crs`` member QGIS still writes for other CRSs
    ("EPSG:25834", "urn:ogc:def:crs:EPSG::25834", CRS84) is honoured."""
    crs = doc.get("crs")
    if not crs:
        return 4326
    name = str((crs.get("properties") or {}).get("name") or "").strip()
    if name.upper().endswith("CRS84"):
        return 4326
    match = _EPSG.search(name)
    if not match:
        raise ValueError(f"{path}: unsupported crs {name!r} (expected an EPSG code)")
    return int(match.group(1))


def _read_zones_geojson(path: Path) -> tuple[list[ZoneRecord], int]:
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    if doc.get("type") != "FeatureCollection":
        raise ValueError(f"{path}: expected a GeoJSON FeatureCollection")
    srs_id = _geojson_srs(doc, path)
    features = doc.get("features") or []
    columns = {key for f in features for key in (f.get("properties") or {})}
    if features:
        _missing_columns(path, columns, schema.ZONE_FIELDS)
    zones = []
    for index, feature in enumerate(features):
        fid = feature.get("id")
        fid = fid if isinstance(fid, int) and not isinstance(fid, bool) else index + 1
        geometry, error = None, None
        if feature.get("geometry"):
            try:
                geometry = shape(feature["geometry"])
            except (ValueError, TypeError, KeyError, ShapelyError) as exc:
                error = str(exc)
        zones.append(
            _zone_record(fid, geometry, feature.get("properties") or {}, geometry_error=error)
        )
    return zones, srs_id


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # a numeric cell ("12.0") read back as text
    return str(value).strip()


def _zone_record(
    fid: int,
    geometry: BaseGeometry | None,
    props: Mapping[str, Any],
    *,
    geometry_error: str | None = None,
) -> ZoneRecord:
    zone = ZoneRecord(
        fid=fid,
        zone_id=_text(props.get("zone_id")),
        name=_text(props.get("name")),
        zone_type=_text(props.get("zone_type")).lower(),
        general_planning_summary=_text(props.get("general_planning_summary")),
        notes=_text(props.get("notes")),
        no_adopted_plan=False,
        geometry=None,
    )
    try:
        zone.no_adopted_plan = bool(schema.parse_bool(props.get("no_adopted_plan")))
    except ValueError as exc:
        zone.problems.append(
            Problem("error", "zone_field_invalid", f"no_adopted_plan: {exc}", zone.label)
        )
    if geometry_error is not None:
        zone.problems.append(
            Problem(
                "error",
                "zone_invalid_geometry",
                f"zone {zone.label}: geometry cannot be read ({geometry_error})",
                zone.label,
            )
        )
        return zone
    polygonal, other_type = _as_polygonal(geometry)
    if other_type is not None:
        zone.problems.append(
            Problem(
                "error",
                "zone_not_polygon",
                f"zone {zone.label} is a {other_type}; zones must be polygons",
                zone.label,
            )
        )
    zone.geometry = polygonal
    return zone


def _read_documents_gpkg(path: Path, table: str) -> list[DocumentRecord]:
    layer = _read_gpkg_layer(path, table)
    _missing_columns(path, layer.fields, schema.DOCUMENT_FIELDS)
    rows = [f for f in layer.features if not _blank(f.properties)]
    return [_document_record(i, f.properties, f.fid) for i, f in enumerate(rows, start=1)]


def _read_documents_csv(path: Path) -> list[DocumentRecord]:
    text = path.read_text(encoding="utf-8-sig")
    header = text.split("\n", 1)[0]
    # A spreadsheet saved under a comma-decimal locale (Montenegro's) writes semicolons.
    delimiter = max((",", ";", "\t"), key=header.count)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    reader.fieldnames = [(name or "").strip() for name in (reader.fieldnames or [])]
    _missing_columns(path, reader.fieldnames, schema.DOCUMENT_FIELDS)
    records = []
    for props in reader:
        props.pop(None, None)  # cells beyond the header
        if _blank(props):
            continue  # the empty lines a spreadsheet leaves at the end
        records.append(_document_record(len(records) + 1, props, None))
    return records


def _blank(props: Mapping[str, Any]) -> bool:
    return all(_text(v) == "" for v in props.values())


def _document_record(row: int, props: Mapping[str, Any], fid: int | None) -> DocumentRecord:
    record = DocumentRecord(row=row, values={}, fid=fid)
    values = record.values
    for spec in schema.DOCUMENT_FIELDS:
        if spec.type == "text":
            values[spec.name] = _text(props.get(spec.name))
    values["status"] = values["status"].lower()
    for spec in schema.DOCUMENT_FIELDS:
        raw = props.get(spec.name)
        try:
            if spec.type == "boolean":
                values[spec.name] = bool(schema.parse_bool(raw))
            elif spec.type == "integer":
                values[spec.name] = _parse_int(raw)
            elif spec.type == "date":
                values[spec.name] = _parse_date(raw)
        except ValueError as exc:
            values[spec.name] = False if spec.type == "boolean" else None
            code = (
                f"document_{spec.name}_invalid" if spec.type == "date" else "document_field_invalid"
            )
            record.problems.append(
                Problem(
                    "error",
                    code,
                    f"{spec.name}: {exc}",
                    values["zone_id"] or None,
                    record.label,
                )
            )
    return record


def _parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"not a whole number: {value!r}")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        number = float(text)  # raises ValueError for text
        if not number.is_integer():
            raise ValueError(f"not a whole number: {value!r}") from None
        return int(number)


def _parse_date(value: Any) -> date | None:
    """YYYY-MM-DD (what QGIS stores; a time part is ignored), d.m.yyyy and d.m.yyyy. (the local
    spelling, trailing dot included)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    if match := _ISO_DATE.match(text):
        year, month, day = (int(g) for g in match.groups())
    elif match := _DOTTED_DATE.match(text):
        day, month, year = (int(g) for g in match.groups())
    else:
        raise ValueError(f"not a date (YYYY-MM-DD or d.m.yyyy): {value!r}")
    return date(year, month, day)  # ValueError for 31.2.


# --- geometry helpers -----------------------------------------------------------------------------


def is_geographic(srs_id: int) -> bool:
    return srs_id in GEOGRAPHIC_SRS


def area_m2(geom: BaseGeometry | None, *, geographic: bool) -> float:
    """Square metres: planar area in a metric CRS, the centroid-latitude approximation in degrees
    (module docstring)."""
    if geom is None or geom.is_empty:
        return 0.0
    if not geographic:
        return float(geom.area)
    lat = math.radians(geom.centroid.y)
    return float(geom.area) * M_PER_DEG_LON_AT_EQUATOR * math.cos(lat) * M_PER_DEG_LAT


def _polygon_parts(geom: BaseGeometry | None) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if hasattr(geom, "geoms"):
        return [p for g in geom.geoms for p in _polygon_parts(g)]
    return []  # points and lines of an intersection or a repair


def _polygonal(geom: BaseGeometry | None) -> BaseGeometry:
    parts = _polygon_parts(geom)
    if not parts:
        return Polygon()
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def _as_polygonal(geom: BaseGeometry | None) -> tuple[BaseGeometry | None, str | None]:
    """(the geometry if polygonal, else None; the offending type name). A collection whose parts
    are all polygons (some editors save one) becomes a MultiPolygon."""
    if geom is None:
        return None, None
    geom = shapely.force_2d(geom)
    if isinstance(geom, Polygon | MultiPolygon):
        return geom, None
    if isinstance(geom, GeometryCollection):
        leaves = list(_leaves(geom))
        if all(isinstance(g, Polygon) for g in leaves):
            return MultiPolygon(leaves), None
    return None, geom.geom_type


def _leaves(geom: BaseGeometry) -> Iterable[BaseGeometry]:
    if hasattr(geom, "geoms"):
        for part in geom.geoms:
            yield from _leaves(part)
    elif not geom.is_empty:
        yield geom


def _point(geom: BaseGeometry, geographic: bool) -> tuple[float, float] | None:
    if geom.is_empty:
        return None
    p = geom.representative_point()
    digits = 7 if geographic else 2
    return (round(p.x, digits), round(p.y, digits))


def _fmt_area(value: float) -> str:
    return f"{value:,.2f} m²" if value < 100 else f"{value:,.0f} m²"


# --- validation -----------------------------------------------------------------------------------


def validate(
    dataset: ZoneDataset,
    *,
    document_types: Collection[str],
    config: ValidationConfig,
    extent: BaseGeometry | None = None,
    extent_srs_id: int | None = None,
    invalid_markers: Collection[str] | None = None,
    today: date | None = None,
) -> ValidationReport:
    """Every check of the module docstring; the report lists errors before warnings.

    ``document_types`` are the municipality profile's type keys; ``extent`` (in the dataset's CRS;
    pass ``extent_srs_id`` to have that checked) must be covered completely; ``invalid_markers``
    default to :data:`DEFAULT_INVALID_MARKERS`; ``today`` is for tests."""
    if extent is not None and extent_srs_id is not None and extent_srs_id != dataset.srs_id:
        raise ValueError(
            f"the extent is in EPSG:{extent_srs_id}, the zones in EPSG:{dataset.srs_id}: "
            "reproject one of them first"
        )
    geographic = is_geographic(dataset.srs_id)
    problems: list[Problem] = []
    if dataset.srs_id in UNDEFINED_SRS:
        problems.append(
            Problem(
                "error",
                "zones_crs_undefined",
                "the zones layer has no coordinate reference system: set it in QGIS "
                "(Layer Properties > Source) before exporting",
            )
        )
    if not dataset.zones:
        problems.append(Problem("error", "zones_layer_empty", "the zones layer has no features"))
    for zone in dataset.zones:
        problems.extend(zone.problems)
    problems.extend(_check_zone_attributes(dataset.zones))
    shapes = _check_zone_geometries(dataset.zones, geographic, problems)
    topology = _check_topology(shapes, config, geographic, extent, problems)
    for document in dataset.documents:
        problems.extend(document.problems)
    markers = DEFAULT_INVALID_MARKERS if invalid_markers is None else tuple(invalid_markers)
    problems.extend(
        _check_documents(dataset, set(document_types), config, markers, today or date.today())
    )
    adopted = _check_zone_documents(dataset, problems)

    by_status = Counter(d.status for d in dataset.documents)
    documents_by_status = {s: by_status.pop(s, 0) for s in schema.DOCUMENT_STATUSES}
    if by_status:
        documents_by_status["other"] = sum(by_status.values())
    stats = {
        "srs_id": dataset.srs_id,
        "area_method": "geographic_approximation" if geographic else "planar",
        "zones": len(dataset.zones),
        "zones_with_geometry": len(shapes),
        "documents": len(dataset.documents),
        "documents_by_status": documents_by_status,
        "adopted_per_zone": adopted,
        "total_area_m2": round(topology["total_area_m2"], 1),
        "overlap_pairs": topology["overlap_pairs"],
        "gaps": topology["gaps"],
    }
    ordered = sorted(problems, key=lambda p: p.severity != "error")  # stable: errors first
    return ValidationReport(problems=ordered, stats=stats)


def _check_zone_attributes(zones: list[ZoneRecord]) -> list[Problem]:
    problems: list[Problem] = []
    for zone in zones:
        label = zone.label
        if not zone.zone_id:
            problems.append(
                Problem("error", "zone_id_missing", f"zone at fid {zone.fid} has no zone_id")
            )
        elif not schema.SLUG_RE.match(zone.zone_id):
            problems.append(
                Problem(
                    "error",
                    "zone_id_format",
                    f"zone_id {zone.zone_id!r} is not a slug (lower-case letters, digits and "
                    f"single hyphens, e.g. {schema.slugify(zone.zone_id) or 'nova-varos'!r})",
                    zone.zone_id,
                )
            )
        if not zone.name:
            problems.append(
                Problem("error", "zone_name_missing", f"zone {label} has no name", label)
            )
        if not zone.zone_type:
            problems.append(
                Problem(
                    "warning",
                    "zone_type_missing",
                    f"zone {label} has no zone_type: the map draws it neutral",
                    label,
                )
            )
        elif zone.zone_type not in schema.ZONE_TYPE_BY_VALUE:
            valid = " | ".join(t.value for t in schema.ZONE_TYPES)
            problems.append(
                Problem(
                    "error",
                    "zone_type_invalid",
                    f"zone {label}: zone_type {zone.zone_type!r} is not one of {valid}",
                    label,
                )
            )

    by_id: dict[str, list[ZoneRecord]] = defaultdict(list)
    by_name: dict[str, list[ZoneRecord]] = defaultdict(list)
    for zone in zones:
        if zone.zone_id:
            by_id[zone.zone_id].append(zone)
        if zone.name:
            by_name[schema.name_key(zone.name)].append(zone)
    for zone_id, group in by_id.items():
        if len(group) > 1:
            fids = ", ".join(str(z.fid) for z in group)
            problems.append(
                Problem(
                    "error",
                    "zone_id_duplicate",
                    f"zone_id {zone_id!r} is used by {len(group)} features (fid {fids}); "
                    "merge them into one (multi)polygon or give each its own id",
                    zone_id,
                    zone_ids=(zone_id,),
                )
            )
    for group in by_name.values():
        if len(group) > 1:
            labels = tuple(z.label for z in group)
            problems.append(
                Problem(
                    "error",
                    "zone_name_duplicate",
                    f"zone name {group[0].name!r} is used by {', '.join(labels)}",
                    labels[0],
                    zone_ids=labels,
                )
            )
    return problems


def _check_zone_geometries(
    zones: list[ZoneRecord], geographic: bool, problems: list[Problem]
) -> list[tuple[ZoneRecord, BaseGeometry]]:
    """Per-zone geometry checks; returns the zones usable for the topology checks, an invalid
    one repaired (``make_valid``) so its overlaps and gaps are still reported next to the
    validity error."""
    shapes: list[tuple[ZoneRecord, BaseGeometry]] = []
    for zone in zones:
        label, geom = zone.label, zone.geometry
        if geom is None:
            if not any(p.code in _GEOMETRY_READ_CODES for p in zone.problems):
                problems.append(
                    Problem(
                        "error", "zone_missing_geometry", f"zone {label} has no geometry", label
                    )
                )
            continue
        if not isinstance(geom, Polygon | MultiPolygon):
            problems.append(
                Problem(
                    "error",
                    "zone_not_polygon",
                    f"zone {label} is a {geom.geom_type}; zones must be polygons",
                    label,
                )
            )
            continue
        if geom.is_empty:
            problems.append(Problem("error", "zone_empty", f"zone {label} is empty", label))
            continue
        if not geom.is_valid:
            reason = shapely.is_valid_reason(geom)
            match = _VALIDITY_POINT.search(reason)
            location = None
            if match:
                digits = 7 if geographic else 2
                location = (round(float(match[1]), digits), round(float(match[2]), digits))
            problems.append(
                Problem(
                    "error",
                    "zone_invalid_geometry",
                    f"zone {label}: invalid geometry ({reason}); fix it with QGIS's "
                    "Check Validity / Fix Geometries",
                    label,
                    location=location,
                )
            )
            geom = _polygonal(shapely.make_valid(geom))
            if not geom.is_empty:
                shapes.append((zone, geom))
            continue
        if geom.area <= 0:
            problems.append(Problem("error", "zone_empty", f"zone {label} has no area", label))
            continue
        shapes.append((zone, geom))
    return shapes


def _check_topology(
    shapes: list[tuple[ZoneRecord, BaseGeometry]],
    config: ValidationConfig,
    geographic: bool,
    extent: BaseGeometry | None,
    problems: list[Problem],
) -> dict[str, Any]:
    result = {"total_area_m2": 0.0, "overlap_pairs": 0, "gaps": 0}
    if not shapes:
        return result
    geoms = [g for _, g in shapes]
    tree = shapely.STRtree(geoms)

    # Overlaps: every intersecting pair once, in input order.
    left, right = tree.query(geoms, predicate="intersects")
    for i, j in sorted({(int(a), int(b)) for a, b in zip(left, right, strict=True) if a < b}):
        overlap = _polygonal(geoms[i].intersection(geoms[j]))
        area = area_m2(overlap, geographic=geographic)
        if area <= NEGLIGIBLE_M2:
            continue  # touching along an edge or a vertex
        a, b = shapes[i][0].label, shapes[j][0].label
        above = area > config.overlap_tolerance_m2
        result["overlap_pairs"] += 1
        problems.append(
            Problem(
                "error" if above else "warning",
                "zones_overlap",
                f"zones {a} and {b} overlap by {_fmt_area(area)} "
                f"({'above' if above else 'within'} the {config.overlap_tolerance_m2:g} m² "
                "tolerance)",
                a,
                area_m2=round(area, 2),
                location=_point(overlap, geographic),
                zone_ids=(a, b),
            )
        )

    # Gaps: holes inside the zones' union (whatever is enclosed by zones but in none), then the
    # part of the extent outside every zone's outline.
    union = shapely.union_all(geoms)
    result["total_area_m2"] = sum(area_m2(p, geographic=geographic) for p in _polygon_parts(union))
    filled = shapely.union_all([Polygon(p.exterior) for p in _polygon_parts(union)])
    gaps: list[tuple[BaseGeometry, str]] = [
        (part, "hole inside the zones") for part in _polygon_parts(filled.difference(union))
    ]
    if extent is not None:
        outside = _polygonal(extent).difference(filled)
        gaps += [(part, "part of the extent in no zone") for part in _polygon_parts(outside)]
    for part, kind in gaps:
        area = area_m2(part, geographic=geographic)
        if area <= NEGLIGIBLE_M2:
            continue
        neighbours = tuple(
            shapes[int(k)][0].label for k in sorted(tree.query(part, predicate="intersects"))
        )
        above = area > config.gap_tolerance_m2
        between = f" next to {', '.join(neighbours[:6])}" if neighbours else ""
        result["gaps"] += 1
        problems.append(
            Problem(
                "error" if above else "warning",
                "zones_gap",
                f"{kind}: {_fmt_area(area)}{between} "
                f"({'above' if above else 'within'} the {config.gap_tolerance_m2:g} m² tolerance)",
                neighbours[0] if neighbours else None,
                area_m2=round(area, 2),
                location=_point(part, geographic),
                zone_ids=neighbours,
            )
        )
    return result


def _is_http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _marked_invalid(texts: Iterable[str], markers: tuple[str, ...]) -> str | None:
    """The marker found (accent- and case-insensitive, at a word start) in the texts, if any."""
    folded = " " + " ".join(schema.name_key(t) for t in texts if t) + " "
    for marker in markers:
        key = schema.name_key(marker)
        if key and f" {key}" in folded:
            return marker
    return None


def _check_documents(
    dataset: ZoneDataset,
    document_types: set[str],
    config: ValidationConfig,
    markers: tuple[str, ...],
    today: date,
) -> list[Problem]:
    problems: list[Problem] = []
    zone_ids = {z.zone_id for z in dataset.zones if z.zone_id}
    type_list = ", ".join(sorted(document_types))
    for doc in dataset.documents:
        v, label = doc.values, doc.label
        zone = v["zone_id"] or None

        def add(severity: Severity, code: str, message: str, zone=zone, label=label) -> None:
            problems.append(Problem(severity, code, message, zone, label))

        if not v["zone_id"]:
            add("error", "document_zone_missing", "no zone_id: every document belongs to a zone")
        elif v["zone_id"] not in zone_ids:
            add(
                "error",
                "document_zone_unknown",
                f"zone_id {v['zone_id']!r} is not a zone of the zones layer",
            )
        if not v["document_name"]:
            add("error", "document_name_missing", "no document_name")
        if v["document_type"] not in document_types:
            what = f"{v['document_type']!r} is not" if v["document_type"] else "missing, must be"
            add("error", "document_type_invalid", f"document_type {what} one of {type_list}")
        if v["status"] not in schema.DOCUMENT_STATUSES:
            what = f"{v['status']!r} is not" if v["status"] else "missing, must be"
            add(
                "error",
                "document_status_invalid",
                f"status {what} one of {' | '.join(schema.DOCUMENT_STATUSES)}",
            )
        adoption = v["adoption_date"]
        if adoption is not None and adoption > today:
            add(
                "error",
                "document_adoption_in_future",
                f"adoption_date {adoption.isoformat()} is in the future",
            )
        if v["source_url"] and not _is_http_url(v["source_url"]):
            add(
                "warning",
                "document_source_url_invalid",
                f"source_url {v['source_url']!r} is not an http(s) link",
            )
        if v["status"] == "adopted" and not v["eregistri_reference"]:
            add(
                "warning",
                "document_adopted_without_reference",
                "adopted but no eregistri_reference: confirm it against the registry",
            )
        if not v["confirmed"]:
            add(
                "error" if config.require_confirmed else "warning",
                "document_not_confirmed",
                "not confirmed with the client yet",
            )
        if v["status"] == "adopted":
            marker = _marked_invalid((v["eregistri_note"], v["notes"]), markers)
            if marker:
                add(
                    "warning",
                    "document_eregistri_invalid_but_adopted",
                    f"marked adopted, but the registry note says {marker!r}: check whether it "
                    "is still in force (superseded?)",
                )

    # Each document once: "every document belongs to exactly one zone".
    groups: dict[tuple[str, str], list[DocumentRecord]] = defaultdict(list)
    for doc in dataset.documents:
        if doc.values["document_name"] or doc.values["eregistri_reference"]:
            groups[schema.document_identity(doc.values)].append(doc)
    for (kind, _), group in groups.items():
        if len(group) < 2:
            continue
        zones = tuple(dict.fromkeys(d.zone_id or "(no zone)" for d in group))
        rows = ", ".join(f"row {d.row} ({d.zone_id or 'no zone'})" for d in group)
        by = "eRegistri id" if kind == "eregistri" else "name and year"
        where = f"in {len(zones)} zones" if len(zones) > 1 else f"twice in zone {zones[0]}"
        problems.append(
            Problem(
                "error",
                "document_duplicate",
                f"{group[0].values['document_name'] or group[0].values['eregistri_reference']} "
                f"is listed {where} (same {by}): {rows}; a document belongs to exactly one zone",
                zones[0],
                "; ".join(d.label for d in group),
                zone_ids=zones,
            )
        )
    return problems


def _check_zone_documents(dataset: ZoneDataset, problems: list[Problem]) -> dict[str, int]:
    """Zones against their documents; returns the adopted-document count per zone id."""
    listed: Counter[str] = Counter(d.zone_id for d in dataset.documents if d.zone_id)
    adopted: Counter[str] = Counter(
        d.zone_id for d in dataset.documents if d.zone_id and d.status == "adopted"
    )
    counts: dict[str, int] = {}
    for zone in dataset.zones:
        if not zone.zone_id or zone.zone_id in counts:
            continue  # no id: reported above; a duplicated id is checked once
        zid = zone.zone_id
        counts[zid] = adopted[zid]
        if zone.no_adopted_plan:
            if adopted[zid]:
                problems.append(
                    Problem(
                        "warning",
                        "zone_no_adopted_plan_contradiction",
                        f"zone {zid} is marked no_adopted_plan but lists {adopted[zid]} adopted "
                        "document(s): clear the flag or correct the status",
                        zid,
                    )
                )
            continue  # knowingly without an adopted plan: zero documents is fine
        if not listed[zid]:
            problems.append(
                Problem(
                    "warning",
                    "zone_without_documents",
                    f"no document row names zone {zid} (check the zone_id spelling in the "
                    "document list)",
                    zid,
                )
            )
        if not adopted[zid]:
            problems.append(
                Problem(
                    "error",
                    "zone_without_adopted_document",
                    f"zone {zid} has no adopted document: add one or mark the zone "
                    "no_adopted_plan (it then renders as not covered)",
                    zid,
                )
            )
    return counts
