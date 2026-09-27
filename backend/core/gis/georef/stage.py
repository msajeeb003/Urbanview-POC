"""A georeferenced document in PostGIS: snap to the cadastral base, validate, stage, record.

- **Snapping** (planned parcels and blocks only): every vertex within ``snap_tolerance_m`` of a
  cadastral vertex moves onto it (``ST_Snap`` in the metric CRS against the served cadastral
  parcels around the feature), which removes the hairline gaps and overlaps where the plan follows
  existing boundaries. Vertices farther away stay where the plan put them: an intentional
  re-parcelling is the plan's point, not noise. Each feature carries ``vertices``,
  ``snapped_vertices`` and ``snapped_ratio``; the log lists every moved vertex and every near miss
  (a cadastral vertex beyond the tolerance but within three tolerances, not moved) for review.
- **Validation**: every feature inside the municipality's extent; the planned parcels overlap the
  cadastral parcels under them (none at all while cadastral parcels are there = a bad transform);
  planned parcels of the document overlapping each other by more than 1 m² are reported. Errors
  refuse the dataset (recorded ``invalid``, nothing staged).
- **Staging**: one batch per layer (document coverage, planned parcels, blocks, land use, traffic),
  ``document_id`` and ``dataset_version`` on every feature. Land use and traffic are generic
  layers (the newest staged batch replaces the layer at publish), so their batch carries the other
  documents' features forward. A newer run of the document supersedes its staged dataset. The
  ``georef_datasets`` row records the CRS, the transform, the RMSE per sheet, the snapping and the
  validation.

The module imports only SQLAlchemy at load time: the publish job imports
``apply_georef_datasets`` from here.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SNAPPED = ("urban_parcels", "urban_blocks")
GENERIC = (("planned_land_use", "land_use"), ("planned_traffic", "traffic_network"))
OVERLAP_MIN_M2 = 1.0
NEAR_FACTOR = 3.0
SAMPLES = 20
OFFSET_MIN_SAMPLES = 5  # vertices near the cadastre needed to judge a systematic offset
OFFSET_WARNING = 0.5  # x the tolerance: a mean offset above this is systematic
COMMON_MIN_VERTICES = 10  # a plan this big with no vertex near the cadastre is suspicious


@dataclass
class Finding:
    code: str
    message: str
    count: int
    samples: list[dict[str, Any]] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "count": self.count,
            "samples": self.samples,
        }


@dataclass
class StageOutcome:
    dataset_id: int
    dataset_version: str
    status: str  # staged | invalid
    errors: list[Finding]
    warnings: list[Finding]
    snap: dict[str, Any]
    overlap: dict[str, Any]
    batches: dict[str, int]
    counts: dict[str, int]
    log: list[dict[str, Any]] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _degrees(metres: float) -> float:
    """A generous bounding-box margin in degrees for a distance in metres (1° > 75 km here)."""
    return metres / 75_000.0


CREATE_SQL = (
    "DROP TABLE IF EXISTS geo_rows",
    """
    CREATE TEMP TABLE geo_rows (
        row_no integer PRIMARY KEY,
        layer text NOT NULL,
        props jsonb NOT NULL,
        geom geometry NOT NULL,
        snapped geometry,
        vertices integer NOT NULL DEFAULT 0,
        moved integer NOT NULL DEFAULT 0,
        near integer NOT NULL DEFAULT 0
    ) ON COMMIT DROP
    """,
)
INSERT_SQL = text(
    """
    INSERT INTO geo_rows (row_no, layer, props, geom)
    VALUES (:row, :layer, CAST(:props AS jsonb),
            ST_SetSRID(ST_GeomFromWKB(CAST(:wkb AS bytea)), 4326))
    """
)
SNAP_SQL = text(
    """
    UPDATE geo_rows g
    SET snapped = COALESCE(
        ST_Snap(ST_Transform(g.geom, CAST(:srid AS integer)),
                (SELECT ST_Collect(ST_Transform(c.geom, CAST(:srid AS integer)))
                 FROM cadastral_parcels c
                 WHERE c.municipality_id = :m AND c.retired_at IS NULL
                   AND c.geom && ST_Expand(g.geom, :deg)),
                :tol),
        ST_Transform(g.geom, CAST(:srid AS integer)))
    WHERE g.layer = ANY(CAST(:layers AS text[]))
    """
)
# every distinct vertex of a snapped feature: moved or not (exact coordinates: ST_Snap copies the
# cadastral vertex, an untouched vertex keeps its own), and the vector to the nearest cadastral
# vertex (near misses; the mean vector over the close vertices is the plan's systematic offset)
VERTICES_SQL = text(
    """
    WITH pts AS (
        SELECT DISTINCT ON (g.row_no, ST_X(p.geom), ST_Y(p.geom))
               g.row_no, p.path, p.geom AS pt
        FROM geo_rows g,
             LATERAL ST_DumpPoints(ST_Transform(g.geom, CAST(:srid AS integer))) p
        WHERE g.snapped IS NOT NULL
        ORDER BY g.row_no, ST_X(p.geom), ST_Y(p.geom), p.path
    ), after AS (
        SELECT g.row_no, ST_X(q.geom) AS x, ST_Y(q.geom) AS y
        FROM geo_rows g, LATERAL ST_DumpPoints(g.snapped) q
        WHERE g.snapped IS NOT NULL
    ), cad AS (
        SELECT g.row_no,
               ST_Points(ST_Collect(ST_Transform(c.geom, CAST(:srid AS integer)))) AS cp
        FROM geo_rows g
        JOIN cadastral_parcels c ON c.municipality_id = :m AND c.retired_at IS NULL
             AND c.geom && ST_Expand(g.geom, :deg_near)
        WHERE g.snapped IS NOT NULL
        GROUP BY g.row_no
    )
    SELECT pts.row_no, array_to_string(pts.path, '.') AS vertex,
           ST_X(ST_Transform(pts.pt, 4326)) AS lng, ST_Y(ST_Transform(pts.pt, 4326)) AS lat,
           NOT EXISTS (SELECT 1 FROM after a WHERE a.row_no = pts.row_no
                       AND a.x = ST_X(pts.pt) AND a.y = ST_Y(pts.pt)) AS moved,
           ST_Distance(pts.pt, cad.cp) AS to_cadastre,
           ST_X(ST_ClosestPoint(cad.cp, pts.pt)) - ST_X(pts.pt) AS dx,
           ST_Y(ST_ClosestPoint(cad.cp, pts.pt)) - ST_Y(pts.pt) AS dy
    FROM pts LEFT JOIN cad ON cad.row_no = pts.row_no
    ORDER BY pts.row_no, pts.path
    """
)
COUNTS_SQL = text(
    "UPDATE geo_rows SET vertices = :vertices, moved = :moved, near = :near WHERE row_no = :row"
)
APPLY_SNAP_SQL = text(
    """
    UPDATE geo_rows
    SET geom = ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Transform(snapped, 4326)), 3))
    WHERE snapped IS NOT NULL AND moved > 0
    """
)
OUTSIDE_SQL = text(
    """
    SELECT layer, props->>'feature_key' AS feature_key FROM geo_rows
    WHERE NOT ST_Within(geom, ST_MakeEnvelope(:x0, :y0, :x1, :y1, 4326))
    ORDER BY row_no
    """
)
OVERLAP_SQL = text(
    """
    WITH u AS (SELECT row_no, geom FROM geo_rows WHERE layer = 'urban_parcels'),
    ext AS (SELECT ST_SetSRID(ST_Extent(geom), 4326) AS e FROM u)
    SELECT
        (SELECT count(*) FROM u) AS parcels,
        (SELECT COALESCE(sum(ST_Area(ST_Transform(geom, CAST(:srid AS integer)))), 0) FROM u)
            AS planned_m2,
        (SELECT count(*) FROM cadastral_parcels c, ext
         WHERE c.municipality_id = :m AND c.retired_at IS NULL AND c.geom && ext.e)
            AS cadastral_in_extent,
        (SELECT COALESCE(sum(ST_Area(ST_Transform(ST_Intersection(u.geom, c.geom),
                                                  CAST(:srid AS integer)))), 0)
         FROM u JOIN cadastral_parcels c ON c.municipality_id = :m AND c.retired_at IS NULL
              AND ST_Intersects(c.geom, u.geom)) AS overlap_m2
    """
)
TOPOLOGY_SQL = text(
    """
    SELECT a.props->>'feature_key' AS a, b.props->>'feature_key' AS b,
           round(CAST(ST_Area(ST_Transform(ST_Intersection(a.geom, b.geom),
                                           CAST(:srid AS integer))) AS numeric), 1) AS m2
    FROM geo_rows a JOIN geo_rows b
      ON a.row_no < b.row_no AND a.layer = 'urban_parcels' AND b.layer = 'urban_parcels'
     AND ST_Intersects(a.geom, b.geom)
    WHERE ST_Area(ST_Transform(ST_Intersection(a.geom, b.geom), CAST(:srid AS integer))) > :min
    ORDER BY 3 DESC, 1, 2
    """
)
DOCUMENT_SQL = text("SELECT id FROM planning_documents WHERE id = :d AND municipality_id = :m")
LABEL_TAKEN_SQL = text(
    "SELECT 1 FROM georef_datasets WHERE municipality_id = :m AND dataset_version = :label"
)
LABELS_LIKE_SQL = text(
    "SELECT count(*) FROM georef_datasets WHERE municipality_id = :m AND dataset_version LIKE :p"
)
SUPERSEDE_SQL = text(
    """
    UPDATE georef_datasets SET status = 'superseded'
    WHERE municipality_id = :m AND document_id = :d AND status = 'staged'
    RETURNING dataset_version, batches
    """
)
SUPERSEDE_BATCH_SQL = text(
    "UPDATE geometry_batches SET status = 'superseded' WHERE id = :id AND status = 'staged'"
)
BATCH_SQL = text(
    """
    INSERT INTO geometry_batches (municipality_id, layer_id, status, feature_count, produced_by,
                                  qa_report)
    VALUES (:m, :layer, 'staged', 0, :by, CAST(:qa AS jsonb))
    RETURNING id
    """
)
BATCH_COUNT_SQL = text(
    """
    UPDATE geometry_batches
    SET feature_count = (SELECT count(*) FROM staging_geometry WHERE batch_id = :id)
    WHERE id = :id
    RETURNING feature_count
    """
)
STAGE_COVERAGE_SQL = text(
    """
    INSERT INTO staging_geometry (municipality_id, batch_id, layer_id, feature_key, geom,
                                  properties)
    SELECT :m, :batch, 'document_coverage', CAST(CAST(:d AS bigint) AS text),
           ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Union(geom)), 3)),
           jsonb_build_object('document_id', CAST(:d AS bigint),
                              'dataset_version', CAST(:label AS text))
    FROM geo_rows WHERE layer = 'plan_boundary'
    HAVING count(*) > 0
    """
)
# the plan prints "12"; UrbanView stores the profile's term with it ("UP 12": seeds, panel, keys)
_NUMBER = """
    CASE WHEN upper(props->>'urban_parcel_number') LIKE upper(CAST(:prefix AS text)) || ' %'
         THEN props->>'urban_parcel_number'
         ELSE CAST(:prefix AS text) || ' ' || (props->>'urban_parcel_number') END
"""
_SNAP_PROPS = """
    'vertices', vertices, 'snapped_vertices', moved,
    'snapped_ratio', CASE WHEN vertices > 0 THEN round(CAST(moved AS numeric) / vertices, 3) END
"""
STAGE_PARCELS_SQL = text(
    f"""
    INSERT INTO staging_geometry (municipality_id, batch_id, layer_id, feature_key, geom,
                                  properties)
    SELECT DISTINCT ON (number)
           :m, :batch, 'urban_parcels', CAST(CAST(:d AS bigint) AS text) || '|' || number, geom,
           jsonb_build_object(
               'document_id', CAST(:d AS bigint),
               'urban_parcel_number', number,
               'block_ref', props->>'block_ref',
               'area_m2', round(CAST(ST_Area(ST_Transform(geom, CAST(:srid AS integer)))
                                     AS numeric), 1),
               'dataset_version', CAST(:label AS text),
               {_SNAP_PROPS},
               'source_key', props->>'feature_key', 'sheet', props->>'sheet',
               'page', props->'page', 'source_bbox', props->'source_bbox',
               'qa_flags', props->'qa_flags')
    FROM (SELECT g.*, {_NUMBER} AS number FROM geo_rows g
          WHERE layer = 'urban_parcels'
            AND COALESCE(props->>'urban_parcel_number', '') <> '') x
    ORDER BY number, row_no
    """
)
STAGE_BLOCKS_SQL = text(
    f"""
    INSERT INTO staging_geometry (municipality_id, batch_id, layer_id, feature_key, geom,
                                  properties)
    SELECT DISTINCT ON (props->>'block_ref')
           :m, :batch, 'urban_blocks',
           CAST(CAST(:d AS bigint) AS text) || '|' || (props->>'block_ref'), geom,
           jsonb_build_object(
               'block_ref', props->>'block_ref', 'zone_name', NULL,
               'document_id', CAST(:d AS bigint), 'dataset_version', CAST(:label AS text),
               {_SNAP_PROPS})
    FROM geo_rows
    WHERE layer = 'urban_blocks' AND COALESCE(props->>'block_ref', '') <> ''
    ORDER BY props->>'block_ref', row_no
    """
)
NEWEST_STAGED_SQL = text(
    """
    SELECT id FROM geometry_batches
    WHERE municipality_id = :m AND layer_id = :layer AND status = 'staged'
    ORDER BY id DESC LIMIT 1
    """
)
# a generic layer's new batch replaces the layer at publish: it carries the other documents'
# features forward from the newest staged batch of the layer, else from the current version
CARRY_STAGED_SQL = text(
    """
    INSERT INTO staging_geometry (municipality_id, batch_id, layer_id, feature_key, geom,
                                  properties)
    SELECT s.municipality_id, :batch, s.layer_id, s.feature_key, s.geom, s.properties
    FROM staging_geometry s
    WHERE s.batch_id = :source
      AND COALESCE(s.properties->>'document_id', '') <> CAST(CAST(:d AS bigint) AS text)
    """
)
CARRY_SERVED_SQL = text(
    """
    INSERT INTO staging_geometry (municipality_id, batch_id, layer_id, feature_key, geom,
                                  properties)
    SELECT f.municipality_id, :batch, f.layer_id, f.feature_key, f.geom, f.properties
    FROM layer_features f
    JOIN publish_versions v ON v.id = f.publish_version_id AND v.is_current
    WHERE f.municipality_id = :m AND f.layer_id = :layer
      AND COALESCE(f.properties->>'document_id', '') <> CAST(CAST(:d AS bigint) AS text)
    """
)
STAGE_GENERIC_SQL = text(
    """
    INSERT INTO staging_geometry (municipality_id, batch_id, layer_id, feature_key, geom,
                                  properties)
    SELECT :m, :batch, :staged,
           CAST(CAST(:d AS bigint) AS text) || '|' || (props->>'feature_key'), geom,
           jsonb_strip_nulls(jsonb_build_object(
               'code', props->>'code', 'name', props->>'name',
               'road_class', props->>'road_class',
               'urban_parcel_number', props->>'urban_parcel_number',
               'document_id', CAST(:d AS bigint), 'dataset_version', CAST(:label AS text)))
    FROM geo_rows WHERE layer = :layer
    """
)
DATASET_SQL = text(
    """
    INSERT INTO georef_datasets (municipality_id, document_id, dataset_version, status, source,
        crs, method, transform, rmse_m, max_residual_m, points_used, sheets, snap, validation,
        batches, output_sha256, gpkg_key, imported_by)
    VALUES (:m, :d, :label, :status, :source, :crs, :method, CAST(:transform AS jsonb), :rmse,
        :max_residual, :points, CAST(:sheets AS jsonb), CAST(:snap AS jsonb),
        CAST(:validation AS jsonb), CAST(:batches AS jsonb), :sha, :gpkg_key, :by)
    RETURNING id
    """
)


async def next_label(
    session: AsyncSession, municipality_id: str, document_id: int, today: date
) -> str:
    stem = f"geo-{document_id}-{today:%Y%m%d}"
    count = (
        await session.execute(LABELS_LIKE_SQL, {"m": municipality_id, "p": f"{stem}-%"})
    ).scalar()
    return f"{stem}-{int(count or 0) + 1}"


async def _load(
    session: AsyncSession, features: Mapping[str, list[tuple[Any, dict[str, Any]]]]
) -> dict[str, int]:
    import shapely  # the gis extra, present wherever georeferencing runs

    from core.gis.extract.rules import STAGED_AS

    for statement in CREATE_SQL:
        await session.execute(text(statement))
    counts: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for layer, items in features.items():
        if layer not in STAGED_AS:
            continue
        counts[layer] = len(items)
        for geom, props in items:
            rows.append(
                {
                    "row": len(rows) + 1,
                    "layer": layer,
                    "props": _json(props),
                    "wkb": shapely.to_wkb(geom, byte_order=1, output_dimension=2),
                }
            )
    if rows:
        await session.execute(INSERT_SQL, rows)
    await session.execute(text("CREATE INDEX ON geo_rows USING gist (geom)"))
    await session.execute(text("ANALYZE geo_rows"))
    return counts


async def _snap(
    session: AsyncSession, params: dict[str, Any], tolerance: float
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    snap: dict[str, Any] = {
        "tolerance_m": tolerance,
        "metric_srid": params["srid"],
        "vertices": 0,
        "snapped_vertices": 0,
        "snapped_ratio": 0.0,
        "near_misses": 0,
        "features_snapped": 0,
        "offset_samples": 0,
        "offset_vector_m": None,
        "systematic_offset_m": None,
    }
    if tolerance <= 0:
        return snap, []
    await session.execute(SNAP_SQL, params)
    per_row: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0])
    log: list[dict[str, Any]] = []
    close: list[tuple[float, float]] = []  # vectors to the cadastre of the vertices near it
    for v in (await session.execute(VERTICES_SQL, params)).mappings():
        stats = per_row[v["row_no"]]
        stats[0] += 1
        distance = v["to_cadastre"]
        if distance is not None and distance <= tolerance * NEAR_FACTOR:
            close.append((float(v["dx"]), float(v["dy"])))
        if v["moved"]:
            stats[1] += 1
            kind = "snapped"
        elif distance is not None and tolerance < distance <= tolerance * NEAR_FACTOR:
            stats[2] += 1
            kind = "near_miss"
        else:
            continue
        log.append(
            {
                "row_no": v["row_no"],
                "kind": kind,
                "vertex": v["vertex"],
                "lng": v["lng"],
                "lat": v["lat"],
                "distance_m": None if distance is None else round(float(distance), 3),
            }
        )
    if per_row:
        await session.execute(
            COUNTS_SQL,
            [{"row": r, "vertices": s[0], "moved": s[1], "near": s[2]} for r, s in per_row.items()],
        )
    await session.execute(APPLY_SNAP_SQL)
    total = sum(s[0] for s in per_row.values())
    moved = sum(s[1] for s in per_row.values())
    snap.update(
        vertices=total,
        snapped_vertices=moved,
        snapped_ratio=round(moved / total, 4) if total else 0.0,
        near_misses=sum(s[2] for s in per_row.values()),
        features_snapped=sum(1 for s in per_row.values() if s[1]),
    )
    if close:
        mx = sum(x for x, _ in close) / len(close)
        my = sum(y for _, y in close) / len(close)
        snap.update(
            offset_samples=len(close),
            offset_vector_m=[round(mx, 3), round(my, 3)],
            systematic_offset_m=round(math.hypot(mx, my), 3),
        )
    return snap, log


def _offset_findings(snap: dict[str, Any], overlap: dict[str, Any]) -> list[Finding]:
    """The overlay check: planned vertices that follow the cadastre sit on it on average. A mean
    vector well away from zero is a systematic offset (datum, transform); a sizeable plan with no
    vertex near a cadastral vertex at all is offset beyond the snapping range (or re-parcels
    everything: the reviewer decides)."""
    tolerance = snap.get("tolerance_m") or 0
    if tolerance <= 0:
        return []
    found = []
    offset = snap.get("systematic_offset_m")
    if (
        snap["offset_samples"] >= OFFSET_MIN_SAMPLES
        and offset is not None
        and offset > tolerance * OFFSET_WARNING
    ):
        dx, dy = snap["offset_vector_m"]
        found.append(
            Finding(
                "systematic_offset",
                f"the plan's vertices near the cadastre are off by {offset} m on average in one "
                f"direction ({dx:+} m east, {dy:+} m north): check the control points and the "
                "datum transformation",
                snap["offset_samples"],
            )
        )
    if (
        overlap.get("cadastral_parcels_in_extent")
        and snap["vertices"] >= COMMON_MIN_VERTICES
        and not snap["offset_samples"]
    ):
        found.append(
            Finding(
                "no_common_vertices",
                f"no planned vertex lies within {tolerance * NEAR_FACTOR:g} m of a cadastral "
                "vertex: the plan is probably offset (datum, transform)",
                snap["vertices"],
            )
        )
    return found


def _parcel_number(value: Any, prefix: str) -> str:
    n = str(value or "").strip()
    if not n or n.upper().startswith(f"{prefix.upper()} "):
        return n
    return f"{prefix} {n}"


async def _validate(
    session: AsyncSession,
    features: Mapping[str, list[tuple[Any, dict[str, Any]]]],
    *,
    municipality_id: str,
    bounds: tuple[float, float, float, float],
    srid: int,
    parcel_prefix: str,
) -> tuple[list[Finding], list[Finding], dict[str, Any]]:
    errors: list[Finding] = []
    warnings: list[Finding] = []
    if not any(features.get(layer) for layer in ("plan_boundary", *SNAPPED, "planned_land_use")):
        errors.append(Finding("no_features", "the document has no georeferenced polygons", 0))
    x0, y0, x1, y1 = bounds
    outside = [
        dict(r)
        for r in (
            await session.execute(OUTSIDE_SQL, {"x0": x0, "y0": y0, "x1": x1, "y1": y1})
        ).mappings()
    ]
    if outside:
        errors.append(
            Finding(
                "outside_extent",
                "features outside the municipality's extent: the transform is wrong",
                len(outside),
                outside[:SAMPLES],
            )
        )
    ov = (await session.execute(OVERLAP_SQL, {"m": municipality_id, "srid": srid})).mappings().one()
    planned = float(ov["planned_m2"])
    overlap = {
        "planned_parcels": int(ov["parcels"]),
        "planned_m2": round(planned, 1),
        "cadastral_parcels_in_extent": int(ov["cadastral_in_extent"]),
        "overlap_m2": round(float(ov["overlap_m2"]), 1),
        "overlap_share": round(float(ov["overlap_m2"]) / planned, 4) if planned else None,
    }
    if ov["parcels"] and ov["cadastral_in_extent"] and not ov["overlap_m2"]:
        errors.append(
            Finding(
                "no_cadastral_overlap",
                "the planned parcels overlap no cadastral parcel although cadastral parcels lie "
                "around them: the transform is wrong",
                int(ov["parcels"]),
            )
        )
    elif ov["parcels"] and not ov["cadastral_in_extent"]:
        warnings.append(
            Finding(
                "no_cadastral_base",
                "no cadastral parcels are loaded under the document: the overlap check and the "
                "snapping had nothing to compare with",
                0,
            )
        )
    overlaps = [
        dict(r)
        for r in (
            await session.execute(TOPOLOGY_SQL, {"srid": srid, "min": OVERLAP_MIN_M2})
        ).mappings()
    ]
    if overlaps:
        warnings.append(
            Finding(
                "parcel_overlaps",
                f"planned parcels of the document overlapping each other by more than "
                f"{OVERLAP_MIN_M2:g} m²",
                len(overlaps),
                [{**o, "m2": float(o["m2"])} for o in overlaps[:SAMPLES]],
            )
        )
    numbers: dict[str, int] = defaultdict(int)
    unnumbered = 0
    for _, props in features.get("urban_parcels", []):
        n = _parcel_number(props.get("urban_parcel_number"), parcel_prefix)
        if n:
            numbers[n] += 1
        else:
            unnumbered += 1
    if unnumbered:
        warnings.append(
            Finding(
                "unnumbered_parcels", "planned parcels without a number are not staged", unnumbered
            )
        )
    repeated = sorted(n for n, c in numbers.items() if c > 1)
    if repeated:
        warnings.append(
            Finding(
                "repeated_parcel_numbers",
                "parcel numbers drawn more than once: only the first polygon is staged",
                len(repeated),
                [{"urban_parcel_number": n} for n in repeated[:SAMPLES]],
            )
        )
    return errors, warnings, overlap


async def stage_document(
    session: AsyncSession,
    *,
    municipality_id: str,
    document_id: int,
    label: str,
    features: Mapping[str, list[tuple[Any, dict[str, Any]]]],
    transform: dict[str, Any],
    source: str,
    bounds: tuple[float, float, float, float],
    snap_tolerance_m: float,
    metric_srid: int,
    parcel_prefix: str,
    output_sha256: str,
    gpkg_key: str | None,
    imported_by: str,
    log_csv: Path | None = None,
) -> StageOutcome:
    """Snap, validate and stage one georeferenced document (see the module docstring). The
    caller commits."""
    found = await session.execute(DOCUMENT_SQL, {"d": document_id, "m": municipality_id})
    if found.first() is None:
        raise ValueError(f"no planning document {document_id} in {municipality_id}")
    if (await session.execute(LABEL_TAKEN_SQL, {"m": municipality_id, "label": label})).first():
        raise ValueError(f"georeferencing dataset {label!r} exists already")
    counts = await _load(session, features)
    params = {
        "m": municipality_id,
        "srid": metric_srid,
        "tol": snap_tolerance_m,
        "deg": _degrees(snap_tolerance_m * 2 + 1),
        "deg_near": _degrees(snap_tolerance_m * NEAR_FACTOR + 1),
        "layers": list(SNAPPED),
    }
    snap, log = await _snap(session, params, snap_tolerance_m)
    keys = {
        r["row_no"]: (r["layer"], r["feature_key"])
        for r in (
            await session.execute(
                text("SELECT row_no, layer, props->>'feature_key' AS feature_key FROM geo_rows")
            )
        ).mappings()
    }
    for entry in log:
        entry["layer"], entry["feature_key"] = keys.get(entry["row_no"], (None, None))
    if log_csv is not None:
        write_snap_log(log_csv, log)
    snap["log_rows"] = len(log)
    errors, warnings, overlap = await _validate(
        session,
        features,
        municipality_id=municipality_id,
        bounds=bounds,
        srid=metric_srid,
        parcel_prefix=parcel_prefix,
    )
    warnings += _offset_findings(snap, overlap)
    record = {
        "m": municipality_id,
        "d": document_id,
        "label": label,
        "source": source,
        "crs": transform["crs"],
        "method": transform["method"],
        "transform": _json(transform),
        "rmse": transform["rmse_m"],
        "max_residual": transform.get("max_residual_m"),
        "points": transform["points_used"],
        "sheets": _json(
            [
                {k: s.get(k) for k in ("sheet", "page", "points", "rmse_m")}
                for s in transform.get("sheets", [])
            ]
        ),
        "snap": _json({**snap, "overlap": overlap}),
        "sha": output_sha256,
        "gpkg_key": gpkg_key,
        "by": imported_by,
    }

    def validation(staged: dict[str, int]) -> str:
        return _json(
            {
                "ok": not errors,
                "errors": [f.as_json() for f in errors],
                "warnings": [f.as_json() for f in warnings],
                "features": counts,
                "staged": staged,
            }
        )

    if errors:
        dataset_id = (
            await session.execute(
                DATASET_SQL,
                {**record, "status": "invalid", "validation": validation({}), "batches": "{}"},
            )
        ).scalar_one()
        return StageOutcome(
            dataset_id, label, "invalid", errors, warnings, snap, overlap, {}, counts, log
        )

    # a newer run of the document supersedes its staged one; its generic batches are replaced
    # below by batches without its features (they may carry other documents' features)
    previous = (
        await session.execute(SUPERSEDE_SQL, {"m": municipality_id, "d": document_id})
    ).all()
    restage: set[str] = set()
    generic_ids = {staged for _, staged in GENERIC}
    for _, old in previous:
        for layer_id, batch_id in (old or {}).items():
            if layer_id in generic_ids:
                restage.add(layer_id)
            else:
                await session.execute(SUPERSEDE_BATCH_SQL, {"id": int(batch_id)})
    qa = _json(
        {"dataset_version": label, "document_id": document_id, "rmse_m": transform["rmse_m"]}
    )
    stage = {"m": municipality_id, "d": document_id, "label": label, "srid": metric_srid}
    batches: dict[str, int] = {}

    async def new_batch(layer_id: str) -> int:
        batch_id = (
            await session.execute(
                BATCH_SQL,
                {"m": municipality_id, "layer": layer_id, "by": f"georef:{imported_by}", "qa": qa},
            )
        ).scalar_one()
        batches[layer_id] = batch_id
        return batch_id

    if counts.get("plan_boundary"):
        await session.execute(
            STAGE_COVERAGE_SQL, {**stage, "batch": await new_batch("document_coverage")}
        )
    if counts.get("urban_blocks"):
        await session.execute(STAGE_BLOCKS_SQL, {**stage, "batch": await new_batch("urban_blocks")})
    if counts.get("urban_parcels"):
        await session.execute(
            STAGE_PARCELS_SQL,
            {**stage, "batch": await new_batch("urban_parcels"), "prefix": parcel_prefix},
        )
    for layer, staged in GENERIC:
        if not counts.get(layer) and staged not in restage:
            continue
        newest = (
            await session.execute(NEWEST_STAGED_SQL, {"m": municipality_id, "layer": staged})
        ).scalar()
        batch_id = await new_batch(staged)
        if newest is not None:
            await session.execute(
                CARRY_STAGED_SQL, {"batch": batch_id, "source": newest, "d": document_id}
            )
            await session.execute(SUPERSEDE_BATCH_SQL, {"id": newest})
        else:
            await session.execute(
                CARRY_SERVED_SQL,
                {"batch": batch_id, "m": municipality_id, "layer": staged, "d": document_id},
            )
        if counts.get(layer):
            await session.execute(
                STAGE_GENERIC_SQL, {**stage, "batch": batch_id, "staged": staged, "layer": layer}
            )
    staged_counts = {
        layer_id: (await session.execute(BATCH_COUNT_SQL, {"id": batch_id})).scalar_one()
        for layer_id, batch_id in batches.items()
    }
    dataset_id = (
        await session.execute(
            DATASET_SQL,
            {
                **record,
                "status": "staged",
                "validation": validation(staged_counts),
                "batches": _json(batches),
            },
        )
    ).scalar_one()
    return StageOutcome(
        dataset_id,
        label,
        "staged",
        [],
        warnings,
        snap,
        overlap,
        batches,
        counts,
        log,
        superseded=[r[0] for r in previous],
    )


def write_snap_log(path: Path, log: list[dict[str, Any]]) -> None:
    """Every snapped vertex and near miss, for review (CSV, WGS 84)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["kind", "layer", "feature_key", "vertex", "lng", "lat", "distance_m"])
        for e in log:
            writer.writerow(
                [
                    e["kind"],
                    e.get("layer"),
                    e.get("feature_key"),
                    e["vertex"],
                    f"{e['lng']:.9f}",
                    f"{e['lat']:.9f}",
                    "" if e["distance_m"] is None else f"{e['distance_m']:.3f}",
                ]
            )


# --- publish -----------------------------------------------------------------------------------

APPLIED_SQL = text(
    """
    SELECT g.id, g.document_id, g.dataset_version,
           COALESCE(bool_and(b.status IN ('published', 'superseded')), true) AS applied
    FROM georef_datasets g
    LEFT JOIN LATERAL jsonb_each_text(g.batches) e(layer_id, batch_id) ON true
    LEFT JOIN geometry_batches b ON b.id = CAST(e.batch_id AS bigint)
    WHERE g.municipality_id = :m AND g.status = 'staged'
    GROUP BY g.id, g.document_id, g.dataset_version
    ORDER BY g.id
    """
)
SUPERSEDE_PUBLISHED_SQL = text(
    """
    UPDATE georef_datasets SET status = 'superseded'
    WHERE municipality_id = :m AND document_id = :d AND status = 'published'
    """
)
MARK_PUBLISHED_SQL = text(
    """
    UPDATE georef_datasets
    SET status = 'published', published_version_id = :v, published_at = now()
    WHERE id = :id
    """
)


async def apply_georef_datasets(
    session: AsyncSession, *, municipality_id: str, version_id: int
) -> dict[str, Any]:
    """After the publish job's geometry step: a staged dataset whose batches were all applied
    (published, or carried forward by a later generic batch) is published, and the document's
    previously published dataset superseded."""
    published: list[str] = []
    for d in (await session.execute(APPLIED_SQL, {"m": municipality_id})).mappings().all():
        if not d["applied"]:
            continue
        await session.execute(
            SUPERSEDE_PUBLISHED_SQL, {"m": municipality_id, "d": d["document_id"]}
        )
        await session.execute(MARK_PUBLISHED_SQL, {"id": d["id"], "v": version_id})
        published.append(d["dataset_version"])
    return {"georef_datasets": len(published), "georef_versions": published}
