"""A cadastral dataset in PostGIS: load, validate, stage, diff; and its application at publish.

Import (``import_dataset``, one transaction): the export's records go to a temporary table,
invalid geometries are repaired (``ST_MakeValid``) and counted, then the validation runs: a record
without a KO, a number or a polygon, a geometry that cannot be repaired, or two records of the same
(KO, number, sub-number) are errors and the dataset is recorded as ``invalid`` with nothing staged;
parcels outside the municipality's extent, a grid coverage of the extent below the profile's
minimum, KOs of the profile missing from the export are warnings. A valid dataset is staged: the
parcels as one ``cadastral_parcels`` batch (``feature_key = lower(ko_name)|number|sub-number``,
``area_m2`` computed in the profile's projected CRS, a geometry hash for the diff) and the KO
boundaries as one ``cadastral_municipalities`` batch (delivered, else derived from the parcels).
The diff against the previous version classifies every parcel (added, removed, geometry changed,
attributes changed, unchanged; parcels of KOs the new export does not cover are out of scope, never
removed), and a version removing more than the profile's share of the previous parcels is refused
unless the import accepts it. Nothing older is deleted: batches and dataset rows are the history.

Publish (``apply_cadastral_datasets``, called by the publish job after it upserted the batches):
the parcels of the dataset's KOs that the new version no longer contains are retired, the KO
parcel counts refreshed, and the dataset marked published (the previous one superseded).

No GDAL or shapely here: the publish job (API image) imports this module.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SAMPLE_LIMIT = 20
INSERT_CHUNK = 2000
DIFF_CLASSES = ("added", "removed", "geometry_changed", "attributes_changed", "unchanged")


class LargeChangeRefused(RuntimeError):
    """The new version would remove too many parcels of the previous one."""


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
class Validation:
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [f.as_json() for f in self.errors],
            "warnings": [f.as_json() for f in self.warnings],
            "stats": self.stats,
        }


@dataclass
class ImportOutcome:
    dataset_id: int
    dataset_version: str
    status: str  # staged | invalid
    validation: Validation
    diff: dict[str, Any] | None = None
    parcels_batch_id: int | None = None
    ko_batch_id: int | None = None
    superseded: list[str] = field(default_factory=list)
    refused: str | None = None  # why an otherwise valid dataset was not staged


@dataclass(frozen=True, slots=True)
class LoadRow:
    row: int
    source_fid: str | None
    ko_code: str | None
    ko_name: str | None
    parcel_number: str | None
    sub_number: str | None
    street_address: str | None
    geometry: str | None  # GeoJSON, EPSG:4326


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


# --- load ------------------------------------------------------------------------------------

CREATE_ROWS_SQL = text(
    """
    CREATE TEMP TABLE cad_rows (
        row_no integer PRIMARY KEY,
        source_fid text,
        ko_code text,
        ko_name text,
        parcel_number text,
        sub_number text,
        street_address text,
        public_ownership boolean,
        restitution_or_legal_burden boolean,
        geom geometry,
        repaired boolean NOT NULL DEFAULT false,
        parts integer NOT NULL DEFAULT 1
    ) ON COMMIT DROP
    """
)
INSERT_ROW_SQL = text(
    """
    INSERT INTO cad_rows (row_no, source_fid, ko_code, ko_name, parcel_number, sub_number,
                          street_address, geom)
    VALUES (:row, :fid, :ko_code, :ko_name, :number, :sub, :address,
            ST_SetSRID(ST_GeomFromGeoJSON(CAST(:geom AS text)), 4326))
    """
)
CREATE_KOS_SQL = text(
    "CREATE TEMP TABLE cad_kos (ko_code text, ko_name text, geom geometry) ON COMMIT DROP"
)
INSERT_KO_SQL = text(
    """
    INSERT INTO cad_kos (ko_code, ko_name, geom)
    VALUES (:ko_code, :ko_name, ST_SetSRID(ST_GeomFromGeoJSON(CAST(:geom AS text)), 4326))
    """
)
CREATE_FLAGS_SQL = text(
    """
    CREATE TEMP TABLE cad_flags (ko_name text, parcel_number text, sub_number text,
                                 public_ownership boolean, restitution_or_legal_burden boolean)
    ON COMMIT DROP
    """
)
INSERT_FLAG_SQL = text(
    """
    INSERT INTO cad_flags VALUES (:ko_name, :number, :sub, CAST(:po AS boolean),
                                  CAST(:rb AS boolean))
    """
)
APPLY_FLAGS_SQL = text(
    """
    WITH f AS (
        SELECT lower(ko_name) AS ko, parcel_number AS n, COALESCE(sub_number, '') AS s,
               CASE WHEN bool_and(public_ownership) IS NOT DISTINCT FROM bool_or(public_ownership)
                    THEN bool_and(public_ownership) END AS po,
               CASE WHEN bool_and(restitution_or_legal_burden)
                         IS NOT DISTINCT FROM bool_or(restitution_or_legal_burden)
                    THEN bool_and(restitution_or_legal_burden) END AS rb,
               (bool_and(public_ownership) IS DISTINCT FROM bool_or(public_ownership)
                OR bool_and(restitution_or_legal_burden)
                   IS DISTINCT FROM bool_or(restitution_or_legal_burden)) AS conflict
        FROM cad_flags WHERE ko_name IS NOT NULL AND parcel_number IS NOT NULL
        GROUP BY 1, 2, 3
    ), upd AS (
        UPDATE cad_rows r SET public_ownership = f.po, restitution_or_legal_burden = f.rb
        FROM f
        WHERE lower(r.ko_name) = f.ko AND r.parcel_number = f.n
          AND COALESCE(r.sub_number, '') = f.s
        RETURNING f.ko, f.n, f.s
    )
    SELECT (SELECT count(*) FROM upd) AS matched,
           (SELECT count(*) FROM f) AS keys,
           (SELECT count(*) FROM f WHERE conflict) AS conflicts
    """
)


async def _insert_chunks(
    session: AsyncSession, statement: Any, rows: Iterable[dict[str, Any]]
) -> int:
    chunk: list[dict[str, Any]] = []
    total = 0
    for row in rows:
        chunk.append(row)
        if len(chunk) >= INSERT_CHUNK:
            await session.execute(statement, chunk)
            total += len(chunk)
            chunk = []
    if chunk:
        await session.execute(statement, chunk)
        total += len(chunk)
    return total


# --- validation ------------------------------------------------------------------------------


def _samples(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


async def _count_and_sample(
    session: AsyncSession, where: str, params: Mapping[str, Any] | None = None
) -> tuple[int, list[dict[str, Any]]]:
    count = (
        await session.execute(text(f"SELECT count(*) FROM cad_rows WHERE {where}"), params or {})
    ).scalar_one()
    if not count:
        return 0, []
    rows = (
        await session.execute(
            text(
                "SELECT row_no, source_fid, ko_name, parcel_number, sub_number FROM cad_rows "
                f"WHERE {where} ORDER BY row_no LIMIT {SAMPLE_LIMIT}"
            ),
            params or {},
        )
    ).mappings()
    return int(count), _samples(rows)


INVALID_SQL = text(
    f"""
    SELECT row_no, source_fid, ko_name, parcel_number, sub_number,
           ST_IsValidReason(geom) AS reason
    FROM cad_rows WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) AND NOT ST_IsValid(geom)
    ORDER BY row_no LIMIT {SAMPLE_LIMIT}
    """
)
REPAIR_SQL = text(
    """
    UPDATE cad_rows SET geom = ST_Multi(ST_CollectionExtract(ST_MakeValid(geom), 3)),
                        repaired = true
    WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) AND NOT ST_IsValid(geom)
    """
)
DUPLICATES_SQL = text(
    """
    SELECT min(ko_name) AS ko_name, parcel_number, NULLIF(COALESCE(sub_number, ''), '') AS
           sub_number, count(*) AS records, array_agg(row_no ORDER BY row_no) AS rows
    FROM cad_rows WHERE ko_name IS NOT NULL AND parcel_number IS NOT NULL
    GROUP BY lower(ko_name), parcel_number, COALESCE(sub_number, '')
    HAVING count(*) > 1
    ORDER BY 1, 2, 3
    """
)
MERGE_SQL = text(
    """
    WITH g AS (
        SELECT min(row_no) AS keep, array_agg(row_no) AS rows, count(*) AS n,
               ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Union(geom)), 3)) AS geom
        FROM cad_rows WHERE ko_name IS NOT NULL AND parcel_number IS NOT NULL
        GROUP BY lower(ko_name), parcel_number, COALESCE(sub_number, '')
        HAVING count(*) > 1
    ), merged AS (
        UPDATE cad_rows r SET geom = g.geom, parts = g.n FROM g WHERE r.row_no = g.keep
    )
    DELETE FROM cad_rows r USING g WHERE r.row_no = ANY(g.rows) AND r.row_no <> g.keep
    """
)
KO_SUMMARY_SQL = text(
    """
    SELECT min(ko_name) AS ko_name, min(ko_code) AS ko_code,
           count(DISTINCT ko_code) AS codes, count(*) AS parcels
    FROM cad_rows WHERE ko_name IS NOT NULL GROUP BY lower(ko_name) ORDER BY 1
    """
)
EXTENT_SQL = text(
    """
    SELECT ST_XMin(e) AS min_lng, ST_YMin(e) AS min_lat, ST_XMax(e) AS max_lng,
           ST_YMax(e) AS max_lat
    FROM (SELECT ST_Extent(geom) AS e FROM cad_rows) x
    """
)
AREA_STATS_SQL = text(
    """
    WITH a AS (SELECT ST_Area(ST_Transform(geom, CAST(:srid AS integer))) AS m2 FROM cad_rows
               WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom))
    SELECT count(*) AS n, min(m2) AS min_m2, max(m2) AS max_m2, sum(m2) AS total_m2,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY m2) AS median_m2,
           count(*) FILTER (WHERE m2 < 1) AS under_1m2
    FROM a
    """
)
COVERAGE_SQL = text(
    """
    WITH b AS (
        SELECT ST_Transform(ST_MakeEnvelope(:x0, :y0, :x1, :y1, 4326), CAST(:srid AS integer))
               AS g
    ), cells AS (
        SELECT ST_Transform(s.geom, 4326) AS cell FROM b, ST_SquareGrid(:cell, b.g) AS s
    )
    SELECT count(*) AS cells,
           count(*) FILTER (WHERE EXISTS (
               SELECT 1 FROM cad_rows r WHERE ST_Intersects(r.geom, c.cell))) AS covered
    FROM cells c
    """
)


async def validate(
    session: AsyncSession,
    *,
    bounds: tuple[float, float, float, float],
    area_srid: int,
    on_duplicate: str,
    coverage_cell_m: float,
    min_coverage: float,
    expected_kos: Iterable[str],
) -> Validation:
    """Repair what can be repaired and report the rest (see the module docstring)."""
    v = Validation()
    total = (await session.execute(text("SELECT count(*) FROM cad_rows"))).scalar_one()
    v.stats["records"] = int(total)
    if not total:
        v.errors.append(Finding("empty", "the export has no records", 0))
        return v

    checks = (
        ("missing_ko", "ko_name IS NULL", "records without a cadastral municipality (KO)"),
        ("missing_number", "parcel_number IS NULL", "records without a parcel number"),
        ("no_geometry", "geom IS NULL OR ST_IsEmpty(geom)", "records without a geometry"),
        (
            "not_polygon",
            "geom IS NOT NULL AND NOT ST_IsEmpty(geom) "
            "AND GeometryType(geom) NOT IN ('POLYGON', 'MULTIPOLYGON')",
            "records whose geometry is not a polygon",
        ),
    )
    for code, where, message in checks:
        count, samples = await _count_and_sample(session, where)
        if count:
            v.errors.append(Finding(code, message, count, samples))

    invalid = (
        await session.execute(
            text(
                "SELECT count(*) FROM cad_rows WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) "
                "AND NOT ST_IsValid(geom)"
            )
        )
    ).scalar_one()
    v.stats["repaired"] = int(invalid)
    if invalid:
        samples = _samples((await session.execute(INVALID_SQL)).mappings())
        await session.execute(REPAIR_SQL)
        v.warnings.append(
            Finding(
                "repaired",
                "invalid geometries repaired with ST_MakeValid (polygon parts kept)",
                int(invalid),
                samples,
            )
        )
        count, samples = await _count_and_sample(session, "repaired AND ST_IsEmpty(geom)")
        if count:
            v.errors.append(
                Finding(
                    "unrepairable", "geometries with no polygon left after repair", count, samples
                )
            )
    still = (
        await session.execute(
            text("SELECT count(*) FROM cad_rows WHERE geom IS NOT NULL AND NOT ST_IsValid(geom)")
        )
    ).scalar_one()
    v.stats["invalid_after_repair"] = int(still)
    if still:
        v.errors.append(Finding("invalid", "geometries still invalid after repair", int(still)))

    duplicates = [dict(r) for r in (await session.execute(DUPLICATES_SQL)).mappings()]
    if duplicates:
        records = sum(int(d["records"]) for d in duplicates)
        if on_duplicate == "merge":
            await session.execute(MERGE_SQL)
            v.warnings.append(
                Finding(
                    "duplicates_merged",
                    "records of the same (KO, number, sub-number) merged into one parcel",
                    len(duplicates),
                    duplicates[:SAMPLE_LIMIT],
                )
            )
        else:
            v.errors.append(
                Finding(
                    "duplicates",
                    f"(KO, number, sub-number) recorded more than once ({records} records); "
                    'set [cadastre] on_duplicate = "merge" if they are parts of one parcel',
                    len(duplicates),
                    duplicates[:SAMPLE_LIMIT],
                )
            )

    await session.execute(text("CREATE INDEX ON cad_rows USING gist (geom)"))
    await session.execute(text("CREATE INDEX ON cad_rows (lower(ko_name))"))
    await session.execute(text("ANALYZE cad_rows"))

    x0, y0, x1, y1 = bounds
    envelope = {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
    count, samples = await _count_and_sample(
        session,
        "geom IS NOT NULL AND NOT ST_IsEmpty(geom) AND NOT ST_Intersects("
        "ST_PointOnSurface(geom), ST_MakeEnvelope(:x0, :y0, :x1, :y1, 4326))",
        envelope,
    )
    v.stats["outside_extent"] = count
    if count:
        v.warnings.append(
            Finding(
                "outside_extent",
                "parcels outside the municipality's extent (profile bounds)",
                count,
                samples,
            )
        )
    cov = (
        (
            await session.execute(
                COVERAGE_SQL, {**envelope, "srid": area_srid, "cell": coverage_cell_m}
            )
        )
        .mappings()
        .one()
    )
    cells, covered = int(cov["cells"]), int(cov["covered"])
    ratio = round(covered / cells, 4) if cells else 0.0
    v.stats["coverage"] = {
        "cell_m": coverage_cell_m,
        "cells": cells,
        "covered": covered,
        "ratio": ratio,
    }
    if ratio < min_coverage:
        v.warnings.append(
            Finding(
                "coverage",
                f"parcels reach {ratio:.1%} of the {coverage_cell_m:g} m cells over the "
                f"municipality's extent (expected at least {min_coverage:.0%}): the export may "
                "not cover the whole extent",
                cells - covered,
            )
        )
    extent = (await session.execute(EXTENT_SQL)).mappings().one()
    v.stats["extent"] = {
        k: (round(float(val), 6) if val is not None else None) for k, val in extent.items()
    }

    kos = [dict(r) for r in (await session.execute(KO_SUMMARY_SQL)).mappings()]
    v.stats["kos"] = [
        {"ko_name": k["ko_name"], "ko_code": k["ko_code"], "parcels": int(k["parcels"])}
        for k in kos
    ]
    mixed = [k for k in kos if int(k["codes"]) > 1]
    if mixed:
        v.warnings.append(
            Finding(
                "ko_codes", "KO names carrying several KO codes", len(mixed), mixed[:SAMPLE_LIMIT]
            )
        )
    present = {str(k["ko_name"]).casefold() for k in kos}
    missing = [name for name in expected_kos if name.casefold() not in present]
    if missing:
        v.warnings.append(
            Finding(
                "missing_kos",
                "KOs of the municipality profile missing from the export",
                len(missing),
                [{"ko_name": name} for name in missing],
            )
        )
    stats = (await session.execute(AREA_STATS_SQL, {"srid": area_srid})).mappings().one()
    v.stats["area_m2"] = {
        k: (round(float(val), 1) if val is not None else None)
        for k, val in stats.items()
        if k != "under_1m2"
    }
    if stats["under_1m2"]:
        count, samples = await _count_and_sample(
            session,
            "geom IS NOT NULL AND ST_Area(ST_Transform(geom, CAST(:srid AS integer))) < 1",
            {"srid": area_srid},
        )
        v.warnings.append(Finding("tiny", "parcels smaller than 1 m²", count, samples))
    return v


# --- staging ---------------------------------------------------------------------------------

LABELS_LIKE_SQL = text(
    "SELECT count(*) FROM cadastral_datasets WHERE municipality_id = :m AND dataset_version LIKE :p"
)
LABEL_TAKEN_SQL = text(
    "SELECT 1 FROM cadastral_datasets WHERE municipality_id = :m AND dataset_version = :label"
)
PREVIOUS_SQL = text(
    """
    SELECT id, dataset_version, parcels_batch_id, ownership FROM cadastral_datasets
    WHERE municipality_id = :m AND status IN ('published', 'staged', 'superseded')
      AND parcels_batch_id IS NOT NULL
    ORDER BY (status = 'published') DESC, id DESC LIMIT 1
    """
)
SUPERSEDE_SQL = text(
    """
    UPDATE cadastral_datasets SET status = 'superseded'
    WHERE municipality_id = :m AND status = 'staged'
    RETURNING dataset_version, parcels_batch_id, ko_batch_id
    """
)
SUPERSEDE_BATCH_SQL = text(
    "UPDATE geometry_batches SET status = 'superseded' WHERE id = :id AND status = 'staged'"
)
BATCH_SQL = text(
    """
    INSERT INTO geometry_batches (municipality_id, layer_id, status, feature_count, produced_by,
                                  qa_report)
    VALUES (:m, :layer, 'staged', :n, :by, CAST(:qa AS jsonb))
    RETURNING id
    """
)
STAGE_PARCELS_SQL = text(
    """
    INSERT INTO staging_geometry (municipality_id, batch_id, layer_id, feature_key, geom,
                                  properties)
    SELECT :m, :batch, 'cadastral_parcels',
           lower(r.ko_name) || '|' || r.parcel_number || '|' || COALESCE(r.sub_number, ''),
           r.geom,
           jsonb_build_object(
               'ko_code', r.ko_code, 'ko_name', r.ko_name, 'parcel_number', r.parcel_number,
               'sub_number', r.sub_number, 'street_address', r.street_address,
               'area_m2', round(CAST(ST_Area(ST_Transform(r.geom, CAST(:srid AS integer)))
                                     AS numeric), 1),
               'public_ownership', r.public_ownership,
               'restitution_or_legal_burden', r.restitution_or_legal_burden,
               'geom_hash', md5(ST_AsBinary(ST_Normalize(r.geom))),
               'source_fid', r.source_fid, 'repaired', r.repaired, 'parts', r.parts,
               'dataset_version', CAST(:label AS text))
    FROM cad_rows r
    """
)
STAGE_KOS_SQL = text(
    """
    WITH k AS (
        SELECT lower(ko_name) AS key, min(ko_name) AS ko_name, min(ko_code) AS ko_code,
               count(*) AS parcels
        FROM cad_rows GROUP BY lower(ko_name)
    ), d AS (
        SELECT lower(ko_name) AS key,
               ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Union(geom)), 3)) AS geom
        FROM cad_kos WHERE ko_name IS NOT NULL AND geom IS NOT NULL GROUP BY lower(ko_name)
    )
    INSERT INTO staging_geometry (municipality_id, batch_id, layer_id, feature_key, geom,
                                  properties)
    SELECT :m, :batch, 'cadastral_municipalities', k.key,
           COALESCE(d.geom, (SELECT ST_Multi(ST_CollectionExtract(ST_MakeValid(
                                        ST_Union(r.geom)), 3))
                             FROM cad_rows r WHERE lower(r.ko_name) = k.key)),
           jsonb_build_object('ko_name', k.ko_name, 'ko_code', k.ko_code,
                              'parcel_count', k.parcels,
                              'boundary_source', CASE WHEN d.geom IS NULL
                                                      THEN 'derived_from_parcels'
                                                      ELSE 'delivered' END,
                              'dataset_version', CAST(:label AS text))
    FROM k LEFT JOIN d USING (key)
    """
)
DATASET_SQL = text(
    """
    INSERT INTO cadastral_datasets (municipality_id, dataset_version, status, source_id,
        source_name, source_url, method, retrieved_at, access_basis, licence_note, file_name,
        file_sha256, file_size, file_key, source_crs, transform, parcels_batch_id, ko_batch_id,
        previous_dataset_id, parcel_count, ko_count, ownership, validation, diff, imported_by)
    VALUES (:m, :label, :status, :source_id, :source_name, :source_url, :method, :retrieved_at,
        :access_basis, :licence_note, :file_name, :file_sha256, :file_size, :file_key,
        :source_crs, :transform, :parcels_batch, :ko_batch, :previous, :parcels, :kos,
        CAST(:ownership AS jsonb), CAST(:validation AS jsonb), CAST(:diff AS jsonb), :by)
    RETURNING id
    """
)
DIFF_SQL = text(
    """
    CREATE TEMP TABLE cad_diff ON COMMIT DROP AS
    WITH o AS (
        SELECT feature_key AS k, properties AS p FROM staging_geometry
        WHERE batch_id = CAST(:old AS bigint)
    ), n AS (
        SELECT feature_key AS k, properties AS p FROM staging_geometry WHERE batch_id = :new
    ), kos AS (SELECT DISTINCT lower(p->>'ko_name') AS ko FROM n)
    SELECT COALESCE(n.k, o.k) AS feature_key,
           CASE WHEN o.k IS NULL THEN 'added'
                WHEN n.k IS NULL THEN
                    CASE WHEN lower(o.p->>'ko_name') IN (SELECT ko FROM kos) THEN 'removed'
                         ELSE 'out_of_scope' END
                WHEN o.p->>'geom_hash' IS DISTINCT FROM n.p->>'geom_hash'
                    THEN 'geometry_changed'
                WHEN (o.p - 'dataset_version' - 'source_fid' - 'repaired' - 'parts' - 'geom_hash'
                          - 'area_m2')
                     IS DISTINCT FROM
                     (n.p - 'dataset_version' - 'source_fid' - 'repaired' - 'parts' - 'geom_hash'
                          - 'area_m2')
                    THEN 'attributes_changed'
                ELSE 'unchanged' END AS change,
           COALESCE(n.p->>'ko_name', o.p->>'ko_name') AS ko_name,
           COALESCE(n.p->>'parcel_number', o.p->>'parcel_number') AS parcel_number,
           COALESCE(n.p->>'sub_number', o.p->>'sub_number') AS sub_number,
           CAST(o.p->>'area_m2' AS double precision) AS area_before,
           CAST(n.p->>'area_m2' AS double precision) AS area_after,
           o.p->>'street_address' AS address_before, n.p->>'street_address' AS address_after
    FROM o FULL JOIN n ON n.k = o.k
    """
)
DIFF_COUNTS_SQL = text(
    """
    SELECT ko_name, change, count(*) AS n FROM cad_diff
    GROUP BY ko_name, change ORDER BY ko_name, change
    """
)
DIFF_ROWS_SQL = text(
    """
    SELECT change, ko_name, parcel_number, sub_number, area_before, area_after, address_before,
           address_after
    FROM cad_diff WHERE change <> 'unchanged'
    ORDER BY change, lower(ko_name), parcel_number, sub_number
    """
)
PREVIOUS_IN_SCOPE_SQL = text(
    """
    SELECT count(*) FROM cad_diff WHERE change IN ('removed', 'geometry_changed',
                                                  'attributes_changed', 'unchanged')
    """
)


async def next_label(session: AsyncSession, municipality_id: str, prefix: str, today: date) -> str:
    stem = f"{prefix}-{today:%Y%m%d}"
    count = (
        await session.execute(LABELS_LIKE_SQL, {"m": municipality_id, "p": f"{stem}%"})
    ).scalar()
    return f"{stem}-{int(count or 0) + 1}"


async def _diff(
    session: AsyncSession, old_batch: int | None, new_batch: int, previous_label: str | None
) -> tuple[dict[str, Any], int]:
    await session.execute(DIFF_SQL, {"old": old_batch, "new": new_batch})
    per_ko: dict[str, dict[str, int]] = {}
    totals = dict.fromkeys((*DIFF_CLASSES, "out_of_scope"), 0)
    for row in (await session.execute(DIFF_COUNTS_SQL)).mappings():
        per_ko.setdefault(row["ko_name"], {})[row["change"]] = int(row["n"])
        totals[row["change"]] += int(row["n"])
    samples: dict[str, list[dict[str, Any]]] = {}
    for row in (await session.execute(DIFF_ROWS_SQL)).mappings():
        bucket = samples.setdefault(row["change"], [])
        if len(bucket) < SAMPLE_LIMIT:
            bucket.append(dict(row))
    in_scope = (await session.execute(PREVIOUS_IN_SCOPE_SQL)).scalar_one()
    summary = {
        "previous_version": previous_label,
        "totals": totals,
        "per_ko": per_ko,
        "samples": samples,
        "previous_in_scope": int(in_scope),
    }
    return summary, int(in_scope)


async def write_diff_csv(session: AsyncSession, path: Path) -> int:
    """Every parcel that changes, from the diff of the current transaction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "change",
                "ko_name",
                "parcel_number",
                "sub_number",
                "area_before_m2",
                "area_after_m2",
                "address_before",
                "address_after",
            ]
        )
        result = await session.stream(DIFF_ROWS_SQL)
        async for row in result:
            writer.writerow(list(row))
            count += 1
    return count


@dataclass(frozen=True, slots=True)
class Provenance:
    source_id: str
    source_name: str
    source_url: str | None
    method: str
    retrieved_at: Any
    access_basis: str | None
    licence_note: str | None
    file_name: str | None
    file_sha256: str | None
    file_size: int | None
    file_key: str | None
    source_crs: str | None
    transform: str | None


async def import_dataset(
    session: AsyncSession,
    *,
    municipality_id: str,
    label: str,
    provenance: Provenance,
    rows: Iterable[LoadRow],
    kos: Iterable[dict[str, Any]] = (),
    flags: Iterable[dict[str, Any]] | None = None,
    ownership: dict[str, Any],
    bounds: tuple[float, float, float, float],
    area_srid: int,
    on_duplicate: str,
    coverage_cell_m: float,
    min_coverage: float,
    mass_change_threshold: float,
    accept_large_change: bool,
    expected_kos: Iterable[str],
    imported_by: str,
    diff_csv: Path | None = None,
    extra_warnings: Iterable[Mapping[str, Any]] = (),
) -> ImportOutcome:
    """Load, validate and stage one dataset (see the module docstring). Does not commit."""
    if (await session.execute(LABEL_TAKEN_SQL, {"m": municipality_id, "label": label})).first():
        raise ValueError(f"cadastral dataset version {label!r} exists already")
    await session.execute(CREATE_ROWS_SQL)
    await _insert_chunks(
        session,
        INSERT_ROW_SQL,
        (
            {
                "row": r.row,
                "fid": r.source_fid,
                "ko_code": r.ko_code,
                "ko_name": r.ko_name,
                "number": r.parcel_number,
                "sub": r.sub_number,
                "address": r.street_address,
                "geom": r.geometry,
            }
            for r in rows
        ),
    )
    await session.execute(CREATE_KOS_SQL)
    await _insert_chunks(session, INSERT_KO_SQL, kos)
    ownership = dict(ownership)
    if flags is not None:
        await session.execute(CREATE_FLAGS_SQL)
        loaded = await _insert_chunks(session, INSERT_FLAG_SQL, flags)
        applied = (await session.execute(APPLY_FLAGS_SQL)).mappings().one()
        ownership.update(
            records=loaded,
            parcels_matched=int(applied["matched"]),
            keys=int(applied["keys"]),
            conflicting_keys=int(applied["conflicts"]),
        )

    validation = await validate(
        session,
        bounds=bounds,
        area_srid=area_srid,
        on_duplicate=on_duplicate,
        coverage_cell_m=coverage_cell_m,
        min_coverage=min_coverage,
        expected_kos=expected_kos,
    )
    if flags is not None and ownership.get("keys", 0) > ownership.get("parcels_matched", 0):
        validation.warnings.append(
            Finding(
                "ownership_unmatched",
                "ownership records matching no parcel of the export",
                int(ownership["keys"]) - int(ownership["parcels_matched"]),
            )
        )
    if flags is not None and ownership.get("conflicting_keys"):
        validation.warnings.append(
            Finding(
                "ownership_conflicts",
                "parcels with contradicting ownership records (flags left unknown)",
                int(ownership["conflicting_keys"]),
            )
        )
    for w in extra_warnings:
        validation.warnings.append(Finding(str(w["code"]), str(w["message"]), int(w["count"])))
    previous = (await session.execute(PREVIOUS_SQL, {"m": municipality_id})).mappings().first()
    if (
        previous is not None
        and (previous["ownership"] or {}).get("status") == "loaded"
        and ownership.get("status") != "loaded"
    ):
        validation.warnings.append(
            Finding(
                "ownership_cleared",
                f"{previous['dataset_version']} loaded the ownership flags and this import does "
                "not: publishing it leaves them unknown (import the eKatastar export with it)",
                1,
            )
        )
    base = {
        "m": municipality_id,
        "label": label,
        "source_id": provenance.source_id,
        "source_name": provenance.source_name,
        "source_url": provenance.source_url,
        "method": provenance.method,
        "retrieved_at": provenance.retrieved_at,
        "access_basis": provenance.access_basis,
        "licence_note": provenance.licence_note,
        "file_name": provenance.file_name,
        "file_sha256": provenance.file_sha256,
        "file_size": provenance.file_size,
        "file_key": provenance.file_key,
        "source_crs": provenance.source_crs,
        "transform": provenance.transform,
        "ownership": _json(ownership),
        "by": imported_by,
    }
    if not validation.ok:
        dataset_id = (
            await session.execute(
                DATASET_SQL,
                {
                    **base,
                    "status": "invalid",
                    "parcels_batch": None,
                    "ko_batch": None,
                    "previous": previous["id"] if previous else None,
                    "parcels": validation.stats.get("records", 0),
                    "kos": len(validation.stats.get("kos", [])),
                    "validation": _json(validation.as_json()),
                    "diff": None,
                },
            )
        ).scalar_one()
        return ImportOutcome(dataset_id, label, "invalid", validation)

    parcels = (await session.execute(text("SELECT count(*) FROM cad_rows"))).scalar_one()
    ko_count = len(validation.stats.get("kos", []))
    qa = {"dataset_version": label, "records": parcels, "warnings": len(validation.warnings)}
    savepoint = await session.begin_nested()
    superseded = (await session.execute(SUPERSEDE_SQL, {"m": municipality_id})).all()
    for _, parcels_batch, ko_batch in superseded:
        for batch in (parcels_batch, ko_batch):
            if batch is not None:
                await session.execute(SUPERSEDE_BATCH_SQL, {"id": batch})
    by = f"import_cadastre:{imported_by}"
    parcels_batch = (
        await session.execute(
            BATCH_SQL,
            {
                "m": municipality_id,
                "layer": "cadastral_parcels",
                "n": parcels,
                "by": by,
                "qa": _json(qa),
            },
        )
    ).scalar_one()
    await session.execute(
        STAGE_PARCELS_SQL,
        {"m": municipality_id, "batch": parcels_batch, "srid": area_srid, "label": label},
    )
    ko_batch = (
        await session.execute(
            BATCH_SQL,
            {
                "m": municipality_id,
                "layer": "cadastral_municipalities",
                "n": ko_count,
                "by": by,
                "qa": _json(qa),
            },
        )
    ).scalar_one()
    await session.execute(STAGE_KOS_SQL, {"m": municipality_id, "batch": ko_batch, "label": label})
    diff, previous_in_scope = await _diff(
        session,
        previous["parcels_batch_id"] if previous else None,
        parcels_batch,
        previous["dataset_version"] if previous else None,
    )
    removed = diff["totals"]["removed"]
    share = removed / previous_in_scope if previous_in_scope else 0.0
    diff["removed_share"] = round(share, 4)
    if share > mass_change_threshold and not accept_large_change:
        await savepoint.rollback()
        reason = (
            f"the new version removes {removed} of the {previous_in_scope} parcels the previous "
            f"version ({diff['previous_version']}) has in the same KOs ({share:.1%}, more than "
            f"{mass_change_threshold:.0%}): check the export (a KO renamed? a partial export?) "
            "and import again with --accept-large-change if it is right"
        )
        validation.errors.append(Finding("large_change", reason, removed))
        dataset_id = (
            await session.execute(
                DATASET_SQL,
                {
                    **base,
                    "status": "invalid",
                    "parcels_batch": None,
                    "ko_batch": None,
                    "previous": previous["id"] if previous else None,
                    "parcels": parcels,
                    "kos": ko_count,
                    "validation": _json(validation.as_json()),
                    "diff": _json(diff),
                },
            )
        ).scalar_one()
        return ImportOutcome(dataset_id, label, "invalid", validation, diff=diff, refused=reason)
    if diff_csv is not None:
        diff["csv_rows"] = await write_diff_csv(session, diff_csv)
    await savepoint.commit()
    dataset_id = (
        await session.execute(
            DATASET_SQL,
            {
                **base,
                "status": "staged",
                "parcels_batch": parcels_batch,
                "ko_batch": ko_batch,
                "previous": previous["id"] if previous else None,
                "parcels": parcels,
                "kos": ko_count,
                "validation": _json(validation.as_json()),
                "diff": _json(diff),
            },
        )
    ).scalar_one()
    return ImportOutcome(
        dataset_id,
        label,
        "staged",
        validation,
        diff=diff,
        parcels_batch_id=parcels_batch,
        ko_batch_id=ko_batch,
        superseded=[row[0] for row in superseded],
    )


# --- publish ---------------------------------------------------------------------------------

DATASETS_OF_BATCHES_SQL = text(
    """
    SELECT id, dataset_version, parcels_batch_id FROM cadastral_datasets
    WHERE municipality_id = :m AND status = 'staged'
      AND parcels_batch_id = ANY(CAST(:batches AS bigint[]))
    ORDER BY id
    """
)
RETIRE_SQL = text(
    """
    UPDATE cadastral_parcels c
    SET retired_at = now(), retired_dataset_version = :label
    WHERE c.municipality_id = :m AND c.retired_at IS NULL
      AND lower(c.ko_name) IN (SELECT DISTINCT lower(s.properties->>'ko_name')
                               FROM staging_geometry s WHERE s.batch_id = :batch)
      AND NOT EXISTS (
          SELECT 1 FROM staging_geometry s
          WHERE s.batch_id = :batch
            AND s.feature_key = lower(c.ko_name) || '|' || c.parcel_number || '|'
                                || COALESCE(c.sub_number, ''))
    """
)
SUPERSEDE_PUBLISHED_SQL = text(
    """
    UPDATE cadastral_datasets SET status = 'superseded'
    WHERE municipality_id = :m AND status = 'published'
    """
)
MARK_PUBLISHED_SQL = text(
    """
    UPDATE cadastral_datasets
    SET status = 'published', published_version_id = :v, published_at = now()
    WHERE id = :id
    """
)
KO_COUNTS_SQL = text(
    """
    UPDATE cadastral_municipalities k
    SET parcel_count = (
        SELECT count(*) FROM cadastral_parcels c
        WHERE c.municipality_id = k.municipality_id AND lower(c.ko_name) = lower(k.ko_name)
          AND c.retired_at IS NULL),
        updated_at = now()
    WHERE k.municipality_id = :m
    """
)
DERIVE_KOS_SQL = text(
    """
    INSERT INTO cadastral_municipalities (municipality_id, ko_name, ko_code, geom, parcel_count,
                                          boundary_source, dataset_version)
    SELECT :m, min(c.ko_name), min(c.ko_code),
           ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Union(c.geom)), 3)), count(*),
           'derived_from_parcels', max(c.dataset_version)
    FROM cadastral_parcels c
    WHERE c.municipality_id = :m AND c.retired_at IS NULL
    GROUP BY lower(c.ko_name)
    ON CONFLICT (municipality_id, lower(ko_name)) DO UPDATE
    SET geom = CASE WHEN cadastral_municipalities.boundary_source = 'delivered'
                    THEN cadastral_municipalities.geom ELSE EXCLUDED.geom END,
        ko_code = COALESCE(EXCLUDED.ko_code, cadastral_municipalities.ko_code),
        parcel_count = EXCLUDED.parcel_count,
        dataset_version = EXCLUDED.dataset_version,
        updated_at = now()
    """
)


async def apply_cadastral_datasets(
    session: AsyncSession,
    *,
    municipality_id: str,
    version_id: int,
    published_batch_ids: list[int],
) -> dict[str, Any]:
    """After the publish job upserted the staged batches: retire, recount, mark published."""
    counts: dict[str, Any] = {"cadastral_datasets": 0, "parcels_retired": 0}
    if not published_batch_ids:
        return counts
    datasets = (
        (
            await session.execute(
                DATASETS_OF_BATCHES_SQL, {"m": municipality_id, "batches": published_batch_ids}
            )
        )
        .mappings()
        .all()
    )
    if not datasets:
        return counts
    await session.execute(SUPERSEDE_PUBLISHED_SQL, {"m": municipality_id})
    for dataset in datasets:
        retired = await session.execute(
            RETIRE_SQL,
            {
                "m": municipality_id,
                "batch": dataset["parcels_batch_id"],
                "label": dataset["dataset_version"],
            },
        )
        counts["parcels_retired"] += retired.rowcount or 0
        await session.execute(MARK_PUBLISHED_SQL, {"id": dataset["id"], "v": version_id})
        counts["cadastral_datasets"] += 1
    counts["dataset_versions"] = [d["dataset_version"] for d in datasets]
    await session.execute(KO_COUNTS_SQL, {"m": municipality_id})
    return counts


async def refresh_derived_kos(session: AsyncSession, municipality_id: str) -> int:
    """KOs of the parcels already served, boundaries derived from them (delivered boundaries are
    kept): for data loaded without a KO layer (the seeded sample)."""
    result = await session.execute(DERIVE_KOS_SQL, {"m": municipality_id})
    await session.execute(KO_COUNTS_SQL, {"m": municipality_id})
    return result.rowcount or 0
