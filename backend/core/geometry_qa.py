"""Topology QA of a staged geometry batch: the pilot technical scope's ``staging.geometry_draft``
``qa_status`` / ``qa_issues`` (overlaps, gaps, area deviation). It runs when a producer stages a
batch (georeferencing and GIS drawings, the zone import, the cadastral import) and on demand
(``python -m core.geometry_qa recheck``); the review API shows it and refuses to approve a batch
that fails.

Per layer (``LAYER_CHECKS``, data):

- **validity** (errors): an empty or invalid geometry. A failing batch cannot be approved: the
  producer is fixed and the batch staged again.
- **overlaps** (warnings): two features of the batch sharing more than ``OVERLAP_MIN_M2`` (the
  locate threshold), listed as pairs with the shared area.
- **gaps** (warnings): holes of the batch's union smaller than ``GAP_MAX_M2``, the slivers
  digitising leaves between neighbours; larger holes are land the layer leaves out (roads between
  planned parcels, a park inside a block). Listed with a point inside and the area.
- **area deviation** (warnings, planned urban parcels): the drawn area against the area the plan's
  table states for the parcel (the document's staged extraction item, else the value the current
  version serves) beyond ``AREA_DEVIATION_PCT``.

The producing dataset's own warnings (georeferencing, zone and cadastral validation) are listed
with the batches they concern. Areas are measured in the municipality's metric CRS
(``core.parcel_links.metric_srid``). ``qa_status``: fail with any error, warn with any warning,
else pass. Issues carry a code, a severity, an English sentence for the console, a count, up to
``MAX_LISTED`` feature keys or locations, and the area concerned.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

QA_VERSION = "1"
OVERLAP_MIN_M2 = 1.0
GAP_MIN_M2 = 0.05  # below this a hole is numerical noise of the snapping, not a gap
GAP_MAX_M2 = 50.0
AREA_DEVIATION_PCT = 5.0
MAX_LISTED = 25

LAYER_CHECKS: dict[str, frozenset[str]] = {
    "urban_parcels": frozenset({"validity", "overlaps", "gaps", "area_deviation"}),
    "urban_blocks": frozenset({"validity", "overlaps", "gaps"}),
    "document_coverage": frozenset({"validity"}),
    "zones": frozenset({"validity", "overlaps", "gaps"}),
    # the cadastral import checks the grid coverage itself; a union of the whole cadastre is slow
    "cadastral_parcels": frozenset({"validity", "overlaps"}),
    "cadastral_municipalities": frozenset({"validity", "overlaps"}),
    "land_use": frozenset({"validity"}),
}
LAYER_LABELS: dict[str, str] = {
    "urban_parcels": "Planned urban parcels",
    "urban_blocks": "Urban blocks",
    "document_coverage": "Plan boundary",
    "zones": "Zones",
    "cadastral_parcels": "Cadastral parcels",
    "cadastral_municipalities": "Cadastral municipalities (KO)",
    "land_use": "Land use",
}
# dataset warnings that concern one layer; any other applies to every batch of the dataset
DATASET_WARNING_LAYERS: dict[str, frozenset[str]] = {
    "parcel_overlaps": frozenset({"urban_parcels"}),
    "unnumbered_parcels": frozenset({"urban_parcels"}),
    "repeated_parcel_numbers": frozenset({"urban_parcels"}),
}

# a georeferencing dataset's source -> the pilot scope's origin of its batches
GEOREF_ORIGINS: dict[str, str] = {
    "extraction": "vector_pdf",
    "manual_redraw": "manual_qgis",
    "gis_file": "official_gis",
}

Severity = Literal["error", "warning"]
QaStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    severity: Severity
    message: str
    count: int = 1
    features: tuple[str, ...] = ()  # what the console lists (keys, pairs, parcel lines)
    keys: tuple[str, ...] = ()  # the feature keys concerned, for the preview to mark
    locations: tuple[tuple[float, float, float], ...] = ()  # lng, lat, m²
    area_m2: float | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "count": self.count,
            "features": list(self.features),
            "keys": list(self.keys),
            "locations": [list(p) for p in self.locations],
            "area_m2": self.area_m2,
        }


@dataclass(slots=True)
class QaResult:
    status: QaStatus
    issues: list[Issue] = field(default_factory=list)
    checks: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": list(self.checks),
            "issues": [i.as_json() for i in self.issues],
        }


def status_of(issues: Iterable[Issue]) -> QaStatus:
    severities = {i.severity for i in issues}
    if "error" in severities:
        return "fail"
    return "warn" if "warning" in severities else "pass"


def _m2(value: float) -> str:
    return f"{value:,.1f} m²"


# --- SQL ------------------------------------------------------------------------------------------

BATCH_SQL = text(
    """
    SELECT id, municipality_id, layer_id, status, document_id, dataset_version
    FROM geometry_batches WHERE id = :batch
    """
)
VALIDITY_SQL = text(
    """
    SELECT feature_key, ST_IsEmpty(geom) AS empty,
           CASE WHEN NOT ST_IsEmpty(geom) THEN ST_IsValidReason(geom) END AS reason,
           count(*) FILTER (WHERE ST_IsEmpty(geom)) OVER () AS n_empty,
           count(*) FILTER (WHERE NOT ST_IsEmpty(geom)) OVER () AS n_invalid
    FROM staging_geometry
    WHERE batch_id = :batch AND (ST_IsEmpty(geom) OR NOT ST_IsValid(geom))
    ORDER BY feature_key
    LIMIT :limit
    """
)
OVERLAPS_SQL = text(
    """
    WITH pairs AS (
        SELECT a.feature_key AS a_key, b.feature_key AS b_key,
               ST_Area(ST_Transform(ST_CollectionExtract(ST_Intersection(a.geom, b.geom), 3),
                                    CAST(:srid AS integer))) AS m2
        FROM staging_geometry a
        JOIN staging_geometry b
          ON b.batch_id = a.batch_id AND b.id > a.id AND a.geom && b.geom
         AND ST_Relate(a.geom, b.geom, '2********')
        WHERE a.batch_id = :batch
          AND ST_Dimension(a.geom) = 2 AND ST_Dimension(b.geom) = 2
          AND ST_IsValid(a.geom) AND ST_IsValid(b.geom)
    )
    SELECT a_key, b_key, m2, count(*) OVER () AS n, sum(m2) OVER () AS total
    FROM pairs WHERE m2 >= :min_m2
    ORDER BY m2 DESC, a_key, b_key
    LIMIT :limit
    """
)
GAPS_SQL = text(
    """
    WITH u AS (
        SELECT ST_Union(geom) AS g FROM staging_geometry
        WHERE batch_id = :batch AND ST_Dimension(geom) = 2 AND ST_IsValid(geom)
    ), parts AS (
        SELECT (ST_Dump(ST_CollectionExtract(g, 3))).geom AS g FROM u WHERE g IS NOT NULL
    ), holes AS (
        SELECT r.geom AS g
        FROM parts, LATERAL ST_DumpRings(parts.g) AS r
        WHERE r.path[1] > 0
    ), measured AS (
        SELECT g, ST_Area(ST_Transform(g, CAST(:srid AS integer))) AS m2 FROM holes
    )
    SELECT ST_X(ST_PointOnSurface(g)) AS lng, ST_Y(ST_PointOnSurface(g)) AS lat, m2,
           count(*) OVER () AS n, sum(m2) OVER () AS total
    FROM measured WHERE m2 >= :min_m2 AND m2 < :max_m2
    ORDER BY m2 DESC
    LIMIT :limit
    """
)
DRAWN_PARCELS_SQL = text(
    """
    SELECT feature_key, properties->>'urban_parcel_number' AS number,
           CAST(properties->>'document_id' AS bigint) AS document_id,
           ST_Area(ST_Transform(geom, CAST(:srid AS integer))) AS drawn
    FROM staging_geometry
    WHERE batch_id = :batch AND ST_Dimension(geom) = 2 AND NOT ST_IsEmpty(geom)
    """
)
# The plan's stated area per parcel: the document's staged extraction items (effective value,
# the latest reading) first, then what the current version serves.
STATED_AREAS_SQL = text(
    """
    SELECT e.document_id, COALESCE(u.urban_parcel_number, e.target_label) AS number,
           CASE WHEN e.review_state = 'amended' THEN e.amended_value_number
                ELSE e.value_number END AS value,
           0 AS rank
    FROM planning_parameter_extractions e
    LEFT JOIN urban_parcels u ON u.id = e.urban_parcel_id
    WHERE e.municipality_id = :m AND e.field_key = 'planned_parcel_area_m2'
      AND e.document_id = ANY(:docs) AND e.superseded_at IS NULL
      AND e.review_state <> 'rejected'
    UNION ALL
    SELECT u.document_id, u.urban_parcel_number, v.value_number, 1
    FROM planning_parameter_values v
    JOIN urban_parcels u ON u.id = v.urban_parcel_id
    JOIN publish_versions pv ON pv.id = v.publish_version_id AND pv.is_current
    WHERE v.municipality_id = :m AND v.field_key = 'planned_parcel_area_m2'
      AND u.document_id = ANY(:docs)
    """
)
DATASET_WARNINGS_SQL = text(
    """
    SELECT 'georef' AS kind, g.validation
    FROM georef_datasets g, jsonb_each_text(g.batches) AS layer(layer_id, batch_id)
    WHERE g.municipality_id = :m AND CAST(layer.batch_id AS bigint) = :batch
    UNION ALL
    SELECT 'zones', z.validation FROM zone_datasets z
    WHERE z.municipality_id = :m AND z.zones_batch_id = :batch
    UNION ALL
    SELECT 'cadastre', c.validation FROM cadastral_datasets c
    WHERE c.municipality_id = :m AND :batch IN (c.parcels_batch_id, c.ko_batch_id)
    """
)
STORE_SQL = text(
    """
    UPDATE geometry_batches SET qa_status = :status, qa_issues = CAST(:issues AS jsonb)
    WHERE id = :batch
    """
)
STAGED_SQL = text(
    """
    SELECT id FROM geometry_batches
    WHERE municipality_id = :m AND status = 'staged' ORDER BY id
    """
)


# --- checks ---------------------------------------------------------------------------------------


async def _validity(session: AsyncSession, batch_id: int) -> list[Issue]:
    rows = (
        (await session.execute(VALIDITY_SQL, {"batch": batch_id, "limit": MAX_LISTED}))
        .mappings()
        .all()
    )
    if not rows:
        return []
    n_invalid, n_empty = int(rows[0]["n_invalid"]), int(rows[0]["n_empty"])
    invalid = [r for r in rows if not r["empty"]]
    issues: list[Issue] = []
    if n_invalid:
        first = invalid[0] if invalid else None
        example = f", e.g. {first['feature_key']}: {first['reason']}" if first else ""
        issues.append(
            Issue(
                "invalid_geometry",
                "error",
                f"{n_invalid} feature(s) have an invalid geometry{example}",
                count=n_invalid,
                features=tuple(r["feature_key"] for r in invalid),
                keys=tuple(r["feature_key"] for r in invalid),
            )
        )
    if n_empty:
        issues.append(
            Issue(
                "empty_geometry",
                "error",
                f"{n_empty} feature(s) have no geometry",
                count=n_empty,
                features=tuple(r["feature_key"] for r in rows if r["empty"]),
                keys=tuple(r["feature_key"] for r in rows if r["empty"]),
            )
        )
    return issues


async def _overlaps(session: AsyncSession, batch_id: int, srid: int) -> list[Issue]:
    rows = (
        (
            await session.execute(
                OVERLAPS_SQL,
                {"batch": batch_id, "srid": srid, "min_m2": OVERLAP_MIN_M2, "limit": MAX_LISTED},
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return []
    n, total = int(rows[0]["n"]), float(rows[0]["total"])
    largest = rows[0]
    return [
        Issue(
            "overlap",
            "warning",
            f"{n} pair(s) of features overlap ({_m2(total)} in all); the largest: "
            f"{largest['a_key']} and {largest['b_key']} share {_m2(float(largest['m2']))}",
            count=n,
            features=tuple(f"{r['a_key']} ↔ {r['b_key']}: {_m2(float(r['m2']))}" for r in rows),
            keys=tuple(dict.fromkeys(k for r in rows for k in (r["a_key"], r["b_key"]))),
            area_m2=round(total, 1),
        )
    ]


async def _gaps(session: AsyncSession, batch_id: int, srid: int) -> list[Issue]:
    rows = (
        (
            await session.execute(
                GAPS_SQL,
                {
                    "batch": batch_id,
                    "srid": srid,
                    "min_m2": GAP_MIN_M2,
                    "max_m2": GAP_MAX_M2,
                    "limit": MAX_LISTED,
                },
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return []
    n, total = int(rows[0]["n"]), float(rows[0]["total"])
    return [
        Issue(
            "gap",
            "warning",
            f"{n} gap(s) between features ({_m2(total)} in all, holes under {GAP_MAX_M2:g} m²): "
            "slivers left between neighbours",
            count=n,
            locations=tuple(
                (round(float(r["lng"]), 7), round(float(r["lat"]), 7), round(float(r["m2"]), 2))
                for r in rows
            ),
            area_m2=round(total, 2),
        )
    ]


def _parcel_key(number: str | None, abbreviation: str) -> str | None:
    from core.extraction.normalise import parcel_key

    return parcel_key(number, abbreviation) if number else None


async def _area_deviation(
    session: AsyncSession, batch_id: int, municipality_id: str, srid: int, abbreviation: str
) -> list[Issue]:
    drawn = (
        (await session.execute(DRAWN_PARCELS_SQL, {"batch": batch_id, "srid": srid}))
        .mappings()
        .all()
    )
    documents = sorted({int(r["document_id"]) for r in drawn if r["document_id"] is not None})
    if not documents:
        return []
    stated: dict[tuple[int, str], tuple[int, float]] = {}
    for row in (
        (await session.execute(STATED_AREAS_SQL, {"m": municipality_id, "docs": documents}))
        .mappings()
        .all()
    ):
        key = _parcel_key(row["number"], abbreviation)
        if key is None or row["value"] is None or float(row["value"]) <= 0:
            continue
        slot = (int(row["document_id"]), key)
        if slot not in stated or int(row["rank"]) < stated[slot][0]:
            stated[slot] = (int(row["rank"]), float(row["value"]))
    deviations: list[tuple[float, str, str]] = []
    for row in drawn:
        key = _parcel_key(row["number"], abbreviation)
        if key is None or row["document_id"] is None:
            continue
        found = stated.get((int(row["document_id"]), key))
        if found is None:
            continue
        plan = found[1]
        pct = (float(row["drawn"]) - plan) / plan * 100
        if abs(pct) > AREA_DEVIATION_PCT:
            deviations.append(
                (
                    pct,
                    f"{row['number']}: drawn {_m2(float(row['drawn']))}, plan {_m2(plan)} "
                    f"({pct:+.1f} %)",
                    row["feature_key"],
                )
            )
    if not deviations:
        return []
    deviations.sort(key=lambda d: -abs(d[0]))
    return [
        Issue(
            "area_deviation",
            "warning",
            f"{len(deviations)} planned parcel(s) differ from the area the plan states by more "
            f"than {AREA_DEVIATION_PCT:g} %; the largest: {deviations[0][1]}",
            count=len(deviations),
            features=tuple(line for _, line, _ in deviations[:MAX_LISTED]),
            keys=tuple(key for _, _, key in deviations[:MAX_LISTED]),
        )
    ]


def dataset_issues(kind: str, validation: Mapping[str, Any] | None, layer_id: str) -> list[Issue]:
    """The producing dataset's warnings that concern this batch's layer."""
    if not validation:
        return []
    if kind == "zones":
        warnings = [p for p in validation.get("problems", []) if p.get("severity") == "warning"]
    else:
        warnings = list(validation.get("warnings", []))
    issues: list[Issue] = []
    for w in warnings:
        code = str(w.get("code") or "warning")
        layers = DATASET_WARNING_LAYERS.get(code)
        if layers is not None and layer_id not in layers:
            continue
        if kind == "cadastre" and layer_id != "cadastral_parcels":
            continue
        issues.append(
            Issue(
                f"{kind}.{code}",
                "warning",
                str(w.get("message") or code),
                count=int(w.get("count") or 1),
                area_m2=w.get("area_m2"),
            )
        )
    return issues


async def check_batch(
    session: AsyncSession,
    batch_id: int,
    *,
    municipality_id: str,
    srid: int | None = None,
    parcel_abbreviation: str | None = None,
) -> QaResult:
    """Every check of the batch's layer; nothing is written."""
    batch = (await session.execute(BATCH_SQL, {"batch": batch_id})).mappings().first()
    if batch is None:
        raise LookupError(f"no geometry batch {batch_id}")
    layer_id = batch["layer_id"]
    checks = LAYER_CHECKS.get(layer_id, frozenset({"validity"}))
    if srid is None:
        from core.parcel_links import metric_srid

        srid = metric_srid(municipality_id)
    issues: list[Issue] = []
    if "validity" in checks:
        issues += await _validity(session, batch_id)
    if "overlaps" in checks:
        issues += await _overlaps(session, batch_id, srid)
    if "gaps" in checks:
        issues += await _gaps(session, batch_id, srid)
    if "area_deviation" in checks:
        if parcel_abbreviation is None:
            from core.municipality import load_profile

            terminology = load_profile(municipality_id).terminology
            parcel_abbreviation = terminology.urban_parcel.abbreviation
        issues += await _area_deviation(
            session, batch_id, municipality_id, srid, parcel_abbreviation
        )
    for row in (
        (await session.execute(DATASET_WARNINGS_SQL, {"m": municipality_id, "batch": batch_id}))
        .mappings()
        .all()
    ):
        issues += dataset_issues(row["kind"], row["validation"], layer_id)
    ordered = tuple(c for c in ("validity", "overlaps", "gaps", "area_deviation") if c in checks)
    return QaResult(status_of(issues), issues, ordered)


async def run_batch_qa(
    session: AsyncSession,
    batch_id: int,
    *,
    municipality_id: str,
    srid: int | None = None,
    parcel_abbreviation: str | None = None,
) -> QaResult:
    """Check the batch and store the outcome on it (the caller commits)."""
    result = await check_batch(
        session,
        batch_id,
        municipality_id=municipality_id,
        srid=srid,
        parcel_abbreviation=parcel_abbreviation,
    )
    await session.execute(
        STORE_SQL,
        {
            "batch": batch_id,
            "status": result.status,
            "issues": json.dumps([i.as_json() for i in result.issues], ensure_ascii=False),
        },
    )
    return result


async def run_batches_qa(
    session: AsyncSession, batch_ids: Sequence[int], *, municipality_id: str, **kw: Any
) -> dict[int, QaResult]:
    return {
        int(b): await run_batch_qa(session, int(b), municipality_id=municipality_id, **kw)
        for b in batch_ids
    }


# --- CLI ------------------------------------------------------------------------------------------


async def _cli(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from core.config import get_settings

    url = args.url or get_settings().database_url
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as session:
            if args.batch:
                ids = list(args.batch)
            else:
                ids = [r[0] for r in (await session.execute(STAGED_SQL, {"m": args.municipality}))]
            out: dict[str, Any] = {}
            for batch_id in ids:
                if args.command == "recheck":
                    result = await run_batch_qa(
                        session, batch_id, municipality_id=args.municipality
                    )
                else:
                    result = await check_batch(session, batch_id, municipality_id=args.municipality)
                out[str(batch_id)] = result.as_json()
            if args.command == "recheck":
                await session.commit()
    finally:
        await engine.dispose()
    if args.json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
    else:
        if not out:
            print("no staged geometry batches")
        for batch_id, result in out.items():
            print(f"batch {batch_id}: {result['status']} ({', '.join(result['checks'])})")
            for issue in result["issues"]:
                print(f"  {issue['severity']:7} {issue['code']}: {issue['message']}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.geometry_qa",
        description="Topology QA of staged geometry batches (overlaps, gaps, area deviation).",
    )
    parser.add_argument(
        "command", choices=("check", "recheck"), help="check: print; recheck: store the outcome"
    )
    parser.add_argument("--batch", type=int, action="append", help="a batch id (repeatable)")
    parser.add_argument("--municipality", default="podgorica")
    parser.add_argument("--url", help="SQLAlchemy URL (default: settings)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(_cli(args))


if __name__ == "__main__":
    sys.exit(main())
