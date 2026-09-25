"""The publish pipeline: everything approved → a new serving version → tiles → pointer flip.

One database transaction covers the whole run (the flip is the commit), so visitors keep
seeing the previous version until the new one is complete, and a failure anywhere leaves nothing
behind but the job's error. Steps, each reported to ``pipeline_jobs.progress``:

1. ``preflight``: no document may still have items pending review (a hard failure otherwise);
2. ``version``: a new ``publish_versions`` row (not current yet);
3. ``values``: the previous version's serving values carried forward, overridden by the
   approved / amended review items (the amended value wins; every item cites its page), which
   are closed with ``published_value_id``; zone / block / document / parcel scopes; fields whose
   extracted value was rejected and nothing replaced become ``planning_value_gaps`` rows (the
   public panel says ``rejected`` without reading the review queue);
4. ``geometry``: staged batches applied: entity layers upserted by natural key (stable ids),
   generic layers copied into ``layer_features`` for the version (untouched layers carried
   forward), document coverage updated; batches marked published;
5. ``links``: cadastral ↔ planned parcel overlaps recomputed with location resolution's
   thresholds (rank 1 = the panel's primary), unmatched cadastral parcels counted;
6. ``cells``: block and zone heatmap cells from the effective parameters and the current market
   assumptions, through the shared formula engine (no arithmetic of its own);
7. ``export``: one newline-delimited GeoJSON file per catalogue layer (``jobs.publish_layers``);
8. ``tiles``: the tile builder (tippecanoe + tile-join) makes one PMTiles archive;
9. ``upload``: the archive goes to the private bucket under ``{m}/tiles/{version}/…``;
10. ``flip``: the version becomes current, the audit row is written, the transaction commits;
11. ``prune``: archives and derived rows of versions beyond ``keep_versions`` are removed
    (never the current or the previous version; version rows and values stay for history).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.services.audit import write_audit
from core.engine.feasibility import Assumptions, MarketInputs, compute_feasibility
from core.engine.shared import FORMULA_VERSION
from core.parcel_links import recompute_parcel_links
from jobs.base import JobContext, JobResult
from jobs.publish_layers import (
    GENERIC_LAYER_IDS,
    LAYERS,
    STAGED_LAYERS,
    URBAN_PARCEL_INPUTS_SQL,
    LayerSpec,
)
from jobs.tiles import LayerFile, TileBuilder

log = logging.getLogger("urbanview.jobs.publish")

STEPS: tuple[str, ...] = (
    "preflight",
    "version",
    "values",
    "geometry",
    "links",
    "cells",
    "export",
    "tiles",
    "upload",
    "flip",
    "prune",
)
ARCHIVE_CONTENT_TYPE = "application/vnd.pmtiles"


class PublishBlocked(RuntimeError):
    """Items are still pending review: the run stops before touching anything."""


class ArchiveStorage(Protocol):
    def put_file(self, key: str, path: Path, content_type: str = ...) -> str: ...

    def delete(self, key: str) -> None: ...


# --- progress ------------------------------------------------------------------------------------


class Progress:
    """The per-step record written to the job row after every transition."""

    def __init__(self, job: JobContext, clock: Callable[[], datetime]) -> None:
        self.job = job
        self.clock = clock
        self.steps: list[dict[str, Any]] = [
            {"name": name, "status": "pending", "started_at": None, "finished_at": None}
            for name in STEPS
        ]

    def _step(self, name: str) -> dict[str, Any]:
        return next(step for step in self.steps if step["name"] == name)

    async def _push(self, current: str) -> None:
        if self.job.report is not None:
            await self.job.report({"step": current, "steps": self.steps})

    async def start(self, name: str) -> None:
        step = self._step(name)
        step["status"] = "running"
        step["started_at"] = self.clock().isoformat()
        log.info("publish job %s: %s", self.job.id, name)
        await self._push(name)

    async def done(self, name: str, detail: Mapping[str, Any] | None = None) -> None:
        step = self._step(name)
        step["status"] = "done"
        step["finished_at"] = self.clock().isoformat()
        if detail:
            step["detail"] = dict(detail)
        await self._push(name)

    async def fail(self, name: str, error: str) -> None:
        step = self._step(name)
        step["status"] = "failed"
        step["finished_at"] = self.clock().isoformat()
        step["error"] = error
        await self._push(name)


# --- pure helpers (unit-tested) -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParcelFigures:
    area_m2: float
    max_far: float | None
    max_site_coverage_pct: float | None
    max_height_m: float | None
    max_gfa_m2: float | None
    saleable_area_m2: float | None
    market_value_eur: float | None


@dataclass(slots=True)
class CellFigures:
    parcel_count: int = 0
    stated_count: int = 0
    max_site_coverage_pct: float | None = None
    max_height_m: float | None = None
    max_far: float | None = None
    max_gfa_m2: float | None = None
    saleable_area_m2: float | None = None
    market_value_eur: float | None = None


def aggregate_cell(parcels: Iterable[ParcelFigures]) -> CellFigures:
    """Area-weighted means for coverage and FAR, the maximum height, sums for the areas and the
    market value; a figure stays ``None`` until at least one parcel states it."""
    out = CellFigures()
    cov_weight = cov_sum = far_weight = far_sum = 0.0
    gfa = saleable = value = 0.0
    has_gfa = has_saleable = has_value = False
    for p in parcels:
        out.parcel_count += 1
        stated = False
        if p.max_site_coverage_pct is not None:
            cov_weight += p.area_m2
            cov_sum += p.max_site_coverage_pct * p.area_m2
            stated = True
        if p.max_far is not None:
            far_weight += p.area_m2
            far_sum += p.max_far * p.area_m2
            stated = True
        if p.max_height_m is not None:
            out.max_height_m = max(out.max_height_m or 0.0, p.max_height_m)
            stated = True
        if p.max_gfa_m2 is not None:
            gfa += p.max_gfa_m2
            has_gfa = stated = True
        if p.saleable_area_m2 is not None:
            saleable += p.saleable_area_m2
            has_saleable = True
        if p.market_value_eur is not None:
            value += p.market_value_eur
            has_value = True
        if stated:
            out.stated_count += 1
    if cov_weight:
        out.max_site_coverage_pct = round(cov_sum / cov_weight, 2)
    if far_weight:
        out.max_far = round(far_sum / far_weight, 3)
    if has_gfa:
        out.max_gfa_m2 = round(gfa, 2)
    if has_saleable:
        out.saleable_area_m2 = round(saleable, 2)
    if has_value:
        out.market_value_eur = round(value, 2)
    return out


def price_bands(rates: Mapping[int, float], bands: int = 3) -> dict[int, int]:
    """Tercile (by default) band per zone from its sale rate: 1 = lowest ... ``bands`` = highest;
    equal rates share a band."""
    ordered = sorted(set(rates.values()))
    if not ordered:
        return {}
    band_of_rate = {rate: 1 + (index * bands) // len(ordered) for index, rate in enumerate(ordered)}
    return {zone_id: band_of_rate[rate] for zone_id, rate in rates.items()}


def sale_rate_range(market: MarketInputs | None) -> tuple[float | None, float | None]:
    """Low / high sale rate of a market row: its absolute bounds, else expected × the range
    factors (the bounds the feasibility engine uses)."""
    if market is None:
        return None, None
    if market.sale_bounds is not None:
        return float(market.sale_bounds[0]), float(market.sale_bounds[1])
    rate = float(market.sale_rate_eur_m2)
    return round(rate * float(market.range_low_factor), 2), round(
        rate * float(market.range_high_factor), 2
    )


def expected_or_none(result: Any, key: str) -> float | None:
    figure = result.get(key)
    return float(figure.expected) if figure.status == "ok" and figure.expected is not None else None


def next_label(existing: Iterable[str], today: datetime) -> str:
    """``YYYY-MM-DD.n``: the first n not taken for the day."""
    prefix = today.strftime("%Y-%m-%d")
    taken = set(existing)
    n = 1
    while f"{prefix}.{n}" in taken:
        n += 1
    return f"{prefix}.{n}"


def market_inputs_from_row(row: Mapping[str, Any]) -> MarketInputs:
    def bounds(prefix: str) -> tuple[float, float] | None:
        low, high = row.get(f"{prefix}_low_eur_m2"), row.get(f"{prefix}_high_eur_m2")
        return (float(low), float(high)) if low is not None and high is not None else None

    return MarketInputs(
        land_rate_eur_m2=float(row["land_rate_eur_m2"]),
        build_rate_eur_m2=float(row["build_rate_eur_m2"]),
        design_rate_eur_m2=float(row["design_rate_eur_m2"]),
        sale_rate_eur_m2=float(row["sale_rate_eur_m2"]),
        range_low_factor=float(row["range_low_factor"]),
        range_high_factor=float(row["range_high_factor"]),
        land_bounds=bounds("land_rate"),
        build_bounds=bounds("build_rate"),
        design_bounds=bounds("design_rate"),
        sale_bounds=bounds("sale_rate"),
    )


# --- SQL -----------------------------------------------------------------------------------------

PENDING_SQL = text(
    """
    SELECT d.id AS document_id, d.name AS document_name, count(*) AS pending
    FROM planning_parameter_extractions e
    JOIN planning_documents d ON d.id = e.document_id
    WHERE e.municipality_id = :m AND e.review_state = 'pending_review'
    GROUP BY d.id, d.name ORDER BY d.name, d.id
    """
)
CURRENT_VERSION_SQL = text(
    "SELECT id, label FROM publish_versions WHERE municipality_id = :m AND is_current FOR UPDATE"
)
LABELS_SQL = text(
    "SELECT label FROM publish_versions WHERE municipality_id = :m AND label LIKE :prefix"
)
INSERT_VERSION_SQL = text(
    """
    INSERT INTO publish_versions (municipality_id, label, published_by, formula_version, notes,
                                  is_current, previous_version_id, job_id, min_zoom, max_zoom)
    VALUES (:m, :label, :by, :formula_version, :notes, false, :previous_id, :job_id, :min_zoom,
            :max_zoom)
    RETURNING id
    """
)
# Review items that can be published: approved / amended, still open, with a value, a page and a
# consistent target; one per (scope, field): the latest decision wins.
ELIGIBLE_ITEMS_SQL = """
    SELECT DISTINCT ON (e.entity_type, e.urban_parcel_id, e.block_id, e.zone_id, e.document_id,
                        e.field_key)
           e.id, e.entity_type, e.document_id, e.field_key,
           CASE e.entity_type WHEN 'urban_parcel' THEN e.urban_parcel_id END AS urban_parcel_id,
           CASE e.entity_type WHEN 'block' THEN e.block_id END AS block_id,
           CASE e.entity_type WHEN 'zone' THEN e.zone_id END AS zone_id,
           CASE WHEN e.review_state = 'amended' THEN e.amended_value_text
                ELSE e.value_text END AS value_text,
           CASE WHEN e.review_state = 'amended' THEN e.amended_value_number
                ELSE e.value_number END AS value_number,
           COALESCE(e.amended_unit, e.unit) AS unit, e.source_page, e.source_bbox, e.source_note
    FROM planning_parameter_extractions e
    JOIN planning_documents d ON d.id = e.document_id AND d.is_current_version
    JOIN planning_fields f ON f.key = e.field_key AND NOT f.computed
    WHERE e.municipality_id = :m AND e.review_state IN ('approved', 'amended')
      AND e.published_value_id IS NULL AND e.entity_type <> 'market_data'
      AND e.source_page IS NOT NULL
      AND num_nonnulls(
            CASE WHEN e.review_state = 'amended' THEN e.amended_value_text ELSE e.value_text END,
            CASE WHEN e.review_state = 'amended' THEN e.amended_value_number
                 ELSE e.value_number END) = 1
      AND ((e.entity_type = 'urban_parcel' AND e.urban_parcel_id IS NOT NULL
            AND EXISTS (SELECT 1 FROM urban_parcels u
                        WHERE u.id = e.urban_parcel_id AND u.document_id = e.document_id))
           OR (e.entity_type = 'block' AND e.block_id IS NOT NULL)
           OR (e.entity_type = 'zone' AND e.zone_id IS NOT NULL)
           OR e.entity_type = 'document')
    ORDER BY e.entity_type, e.urban_parcel_id, e.block_id, e.zone_id, e.document_id, e.field_key,
             e.reviewed_at DESC NULLS LAST, e.id DESC
"""
SKIPPED_ITEMS_SQL = text(
    f"""
    SELECT e.id, e.document_id, e.entity_type, e.field_key,
           CASE WHEN e.source_page IS NULL THEN 'missing_source_page'
                WHEN e.entity_type = 'market_data' THEN 'market_data_not_published_yet'
                WHEN e.field_key IS NULL THEN 'no_field'
                WHEN NOT EXISTS (SELECT 1 FROM planning_documents d
                                 WHERE d.id = e.document_id AND d.is_current_version)
                     THEN 'document_not_current'
                WHEN e.entity_type = 'urban_parcel' AND NOT EXISTS (
                     SELECT 1 FROM urban_parcels u
                     WHERE u.id = e.urban_parcel_id AND u.document_id = e.document_id)
                     THEN 'parcel_document_mismatch'
                WHEN e.entity_type = 'block' AND e.block_id IS NULL THEN 'no_block'
                WHEN e.entity_type = 'zone' AND e.zone_id IS NULL THEN 'no_zone'
                ELSE 'no_value' END AS reason
    FROM planning_parameter_extractions e
    WHERE e.municipality_id = :m AND e.review_state IN ('approved', 'amended')
      AND e.published_value_id IS NULL
      AND e.id NOT IN (SELECT id FROM ({ELIGIBLE_ITEMS_SQL}) eligible)
    ORDER BY e.id
    """
)
CARRY_FORWARD_SQL = text(
    f"""
    WITH eligible AS ({ELIGIBLE_ITEMS_SQL})
    INSERT INTO planning_parameter_values (municipality_id, document_id, urban_parcel_id,
        block_id, zone_id, field_key, value_text, value_number, unit, source_page, source_bbox,
        source_note, publish_version_id, dataset_version)
    SELECT v.municipality_id, v.document_id, v.urban_parcel_id, v.block_id, v.zone_id,
           v.field_key, v.value_text, v.value_number, v.unit, v.source_page, v.source_bbox,
           v.source_note, :new_version, v.dataset_version
    FROM planning_parameter_values v
    JOIN planning_documents d ON d.id = v.document_id AND d.is_current_version
    WHERE v.municipality_id = :m AND v.publish_version_id = :prev_version
      AND NOT EXISTS (
          SELECT 1 FROM eligible e
          WHERE e.field_key = v.field_key
            AND e.urban_parcel_id IS NOT DISTINCT FROM v.urban_parcel_id
            AND e.block_id IS NOT DISTINCT FROM v.block_id
            AND e.zone_id IS NOT DISTINCT FROM v.zone_id
            AND (e.urban_parcel_id IS NOT NULL OR e.block_id IS NOT NULL
                 OR e.zone_id IS NOT NULL OR e.document_id = v.document_id))
    """
)
INSERT_VALUE_SQL = text(
    """
    INSERT INTO planning_parameter_values (municipality_id, document_id, urban_parcel_id,
        block_id, zone_id, field_key, value_text, value_number, unit, source_page, source_bbox,
        source_note, publish_version_id)
    VALUES (:m, :document_id, :urban_parcel_id, :block_id, :zone_id, :field_key, :value_text,
            :value_number, :unit, :source_page, CAST(:source_bbox AS jsonb), :source_note,
            :new_version)
    RETURNING id
    """
)
GAPS_SQL = text(
    """
    WITH items AS (
        SELECT e.id, e.entity_type, e.document_id, e.field_key, e.reviewed_at,
               CASE e.entity_type WHEN 'urban_parcel' THEN e.urban_parcel_id END AS urban_parcel_id,
               CASE e.entity_type WHEN 'block' THEN e.block_id END AS block_id,
               CASE e.entity_type WHEN 'zone' THEN e.zone_id END AS zone_id,
               CASE e.entity_type WHEN 'document' THEN e.document_id END AS document_scope
        FROM planning_parameter_extractions e
        JOIN planning_documents d ON d.id = e.document_id AND d.is_current_version
        JOIN planning_fields f ON f.key = e.field_key AND NOT f.computed
        WHERE e.municipality_id = :m AND e.review_state = 'rejected'
          AND e.published_value_id IS NULL AND e.entity_type <> 'market_data'
          AND ((e.entity_type = 'urban_parcel'
                AND EXISTS (SELECT 1 FROM urban_parcels u
                            WHERE u.id = e.urban_parcel_id AND u.document_id = e.document_id))
               OR (e.entity_type = 'block' AND e.block_id IS NOT NULL)
               OR (e.entity_type = 'zone' AND e.zone_id IS NOT NULL)
               OR e.entity_type = 'document')
    ),
    latest AS (
        SELECT DISTINCT ON (urban_parcel_id, block_id, zone_id, document_scope, field_key) *
        FROM items
        ORDER BY urban_parcel_id, block_id, zone_id, document_scope, field_key,
                 reviewed_at DESC NULLS LAST, id DESC
    )
    INSERT INTO planning_value_gaps (municipality_id, publish_version_id, document_id,
                                     urban_parcel_id, block_id, zone_id, field_key, reason)
    SELECT :m, :v, l.document_id, l.urban_parcel_id, l.block_id, l.zone_id, l.field_key,
           'rejected'
    FROM latest l
    WHERE NOT EXISTS (
        SELECT 1 FROM planning_parameter_values v
        WHERE v.publish_version_id = :v AND v.field_key = l.field_key
          AND v.urban_parcel_id IS NOT DISTINCT FROM l.urban_parcel_id
          AND v.block_id IS NOT DISTINCT FROM l.block_id
          AND v.zone_id IS NOT DISTINCT FROM l.zone_id
          AND (l.document_scope IS NULL OR v.document_id = l.document_scope))
    """
)
CLOSE_ITEM_SQL = text(
    "UPDATE planning_parameter_extractions SET published_value_id = :value_id WHERE id = :id"
)
STAGED_BATCHES_SQL = text(
    """
    SELECT id, layer_id, feature_count FROM geometry_batches
    WHERE municipality_id = :m AND status = 'staged' ORDER BY layer_id, id
    """
)
BATCH_PUBLISHED_SQL = text(
    """
    UPDATE geometry_batches
    SET status = :status, published_version_id = :v, published_at = now()
    WHERE id = :id
    """
)
_MULTIPOLYGON = "ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Force2D(s.geom)), 3))"
_AREA = "round(CAST(ST_Area(CAST(s.geom AS geography)) AS numeric), 1)"
UPSERT_SQL: dict[str, list[str]] = {
    "cadastral_parcels": [
        f"""
        INSERT INTO cadastral_parcels (municipality_id, parcel_number, sub_number, ko_name,
            street_address, geom, area_m2, public_ownership, restitution_or_legal_burden,
            dataset_version)
        SELECT s.municipality_id, s.properties->>'parcel_number',
               NULLIF(s.properties->>'sub_number', ''), s.properties->>'ko_name',
               s.properties->>'street_address', {_MULTIPOLYGON},
               COALESCE(CAST(s.properties->>'area_m2' AS double precision), {_AREA}),
               COALESCE(CAST(s.properties->>'public_ownership' AS boolean), false),
               COALESCE(CAST(s.properties->>'restitution_or_legal_burden' AS boolean), false),
               :label
        FROM staging_geometry s WHERE s.batch_id = :batch
        ON CONFLICT (municipality_id, lower(ko_name), parcel_number, COALESCE(sub_number, ''::text))
        DO UPDATE SET street_address = EXCLUDED.street_address, geom = EXCLUDED.geom,
                      area_m2 = EXCLUDED.area_m2, public_ownership = EXCLUDED.public_ownership,
                      restitution_or_legal_burden = EXCLUDED.restitution_or_legal_burden,
                      dataset_version = EXCLUDED.dataset_version
        """
    ],
    "urban_parcels": [
        f"""
        INSERT INTO urban_parcels (municipality_id, urban_parcel_number, geom, area_m2, block_id,
            document_id, dataset_version)
        SELECT s.municipality_id, s.properties->>'urban_parcel_number', {_MULTIPOLYGON},
               COALESCE(CAST(s.properties->>'area_m2' AS double precision), {_AREA}),
               (SELECT b.id FROM urban_blocks b
                WHERE b.municipality_id = s.municipality_id
                  AND b.block_ref = s.properties->>'block_ref' ORDER BY b.id LIMIT 1),
               CAST(s.properties->>'document_id' AS bigint), :label
        FROM staging_geometry s WHERE s.batch_id = :batch
        ON CONFLICT (document_id, urban_parcel_number)
        DO UPDATE SET geom = EXCLUDED.geom, area_m2 = EXCLUDED.area_m2,
                      block_id = COALESCE(EXCLUDED.block_id, urban_parcels.block_id),
                      dataset_version = EXCLUDED.dataset_version
        """
    ],
    "urban_blocks": [
        f"""
        UPDATE urban_blocks b
        SET geom = {_MULTIPOLYGON},
            zone_id = COALESCE((SELECT z.id FROM zones z WHERE z.municipality_id = b.municipality_id
                                AND z.name = s.properties->>'zone_name' ORDER BY z.id LIMIT 1),
                               b.zone_id),
            dataset_version = :label
        FROM staging_geometry s
        WHERE s.batch_id = :batch AND b.municipality_id = s.municipality_id
          AND b.block_ref = s.properties->>'block_ref'
        """,
        f"""
        INSERT INTO urban_blocks (municipality_id, block_ref, geom, zone_id, dataset_version)
        SELECT s.municipality_id, s.properties->>'block_ref', {_MULTIPOLYGON},
               (SELECT z.id FROM zones z WHERE z.municipality_id = s.municipality_id
                AND z.name = s.properties->>'zone_name' ORDER BY z.id LIMIT 1), :label
        FROM staging_geometry s
        WHERE s.batch_id = :batch AND NOT EXISTS (
            SELECT 1 FROM urban_blocks b WHERE b.municipality_id = s.municipality_id
              AND b.block_ref = s.properties->>'block_ref')
        """,
    ],
    "zones": [
        f"""
        UPDATE zones z
        SET geom = {_MULTIPOLYGON},
            general_planning_summary = COALESCE(s.properties->>'general_planning_summary',
                                                z.general_planning_summary),
            zone_type = COALESCE(
                CASE WHEN s.properties->>'zone_type' IN ('res', 'com', 'mix', 'pub', 'grn')
                     THEN s.properties->>'zone_type' END,
                z.zone_type),
            dataset_version = :label
        FROM staging_geometry s
        WHERE s.batch_id = :batch AND z.municipality_id = s.municipality_id
          AND z.name = s.properties->>'name'
        """,
        f"""
        INSERT INTO zones (municipality_id, name, geom, general_planning_summary, zone_type,
                           dataset_version)
        SELECT s.municipality_id, s.properties->>'name', {_MULTIPOLYGON},
               s.properties->>'general_planning_summary',
               CASE WHEN s.properties->>'zone_type' IN ('res', 'com', 'mix', 'pub', 'grn')
                    THEN s.properties->>'zone_type' END,
               :label
        FROM staging_geometry s
        WHERE s.batch_id = :batch AND NOT EXISTS (
            SELECT 1 FROM zones z WHERE z.municipality_id = s.municipality_id
              AND z.name = s.properties->>'name')
        """,
    ],
    "document_coverage": [
        f"""
        UPDATE planning_documents d
        SET coverage_geom = {_MULTIPOLYGON}, dataset_version = :label
        FROM staging_geometry s
        WHERE s.batch_id = :batch AND d.municipality_id = s.municipality_id
          AND d.id = CAST(s.properties->>'document_id' AS bigint)
        """
    ],
}
COPY_GENERIC_SQL = text(
    """
    INSERT INTO layer_features (municipality_id, publish_version_id, layer_id, feature_key, geom,
                                properties)
    SELECT s.municipality_id, :v, s.layer_id, s.feature_key, ST_Force2D(s.geom), s.properties
    FROM staging_geometry s WHERE s.batch_id = :batch
    """
)
CARRY_GENERIC_SQL = text(
    """
    INSERT INTO layer_features (municipality_id, publish_version_id, layer_id, feature_key, geom,
                                properties)
    SELECT f.municipality_id, :v, f.layer_id, f.feature_key, f.geom, f.properties
    FROM layer_features f
    WHERE f.municipality_id = :m AND f.publish_version_id = :prev AND f.layer_id = :layer
    """
)
ASSUMPTIONS_SQL = text(
    """
    SELECT zone_id, land_rate_eur_m2, build_rate_eur_m2, design_rate_eur_m2, sale_rate_eur_m2,
           range_low_factor, range_high_factor,
           land_rate_low_eur_m2, land_rate_high_eur_m2, build_rate_low_eur_m2,
           build_rate_high_eur_m2, design_rate_low_eur_m2, design_rate_high_eur_m2,
           sale_rate_low_eur_m2, sale_rate_high_eur_m2
    FROM financial_assumptions WHERE municipality_id = :m AND is_current
    """
)
PARCEL_ZONE_SQL = text(
    f"""
    WITH inputs AS ({URBAN_PARCEL_INPUTS_SQL})
    SELECT i.*, COALESCE(i.zone_id, (
               SELECT z.id FROM zones z
               WHERE z.municipality_id = :m
                 AND ST_Contains(z.geom, ST_PointOnSurface(u.geom))
               ORDER BY ST_Area(z.geom) ASC, z.id ASC LIMIT 1)) AS effective_zone_id
    FROM inputs i JOIN urban_parcels u ON u.id = i.id
    """
)
INSERT_CELL_SQL = {
    "block": text(
        """
        INSERT INTO heatmap_cells (municipality_id, publish_version_id, cell_type, cell_id,
            cell_ref, geom, parcel_count, stated_count, max_site_coverage_pct, max_height_m,
            max_far, max_gfa_m2, saleable_area_m2, sale_rate_eur_m2, sale_rate_low_eur_m2,
            sale_rate_high_eur_m2, market_value_eur, price_band)
        SELECT :m, :v, 'block', b.id, b.block_ref, b.geom, :parcel_count, :stated_count,
               :max_site_coverage_pct, :max_height_m, :max_far, :max_gfa_m2, :saleable_area_m2,
               :sale_rate_eur_m2, :sale_rate_low_eur_m2, :sale_rate_high_eur_m2,
               :market_value_eur, :price_band
        FROM urban_blocks b WHERE b.id = :cell_id AND b.municipality_id = :m
        """
    ),
    "zone": text(
        """
        INSERT INTO heatmap_cells (municipality_id, publish_version_id, cell_type, cell_id,
            cell_ref, geom, parcel_count, stated_count, max_site_coverage_pct, max_height_m,
            max_far, max_gfa_m2, saleable_area_m2, sale_rate_eur_m2, sale_rate_low_eur_m2,
            sale_rate_high_eur_m2, market_value_eur, price_band)
        SELECT :m, :v, 'zone', z.id, z.name, z.geom, :parcel_count, :stated_count,
               :max_site_coverage_pct, :max_height_m, :max_far, :max_gfa_m2, :saleable_area_m2,
               :sale_rate_eur_m2, :sale_rate_low_eur_m2, :sale_rate_high_eur_m2,
               :market_value_eur, :price_band
        FROM zones z WHERE z.id = :cell_id AND z.municipality_id = :m
        """
    ),
}
ALL_CELL_IDS_SQL = {
    "block": text(
        "SELECT b.id, b.zone_id FROM urban_blocks b WHERE b.municipality_id = :m ORDER BY b.id"
    ),
    "zone": text(
        "SELECT z.id, z.id AS zone_id FROM zones z WHERE z.municipality_id = :m ORDER BY z.id"
    ),
}
UNSET_CURRENT_SQL = text(
    "UPDATE publish_versions SET is_current = false WHERE municipality_id = :m AND is_current"
)
FLIP_SQL = text(
    """
    UPDATE publish_versions
    SET is_current = true, published_at = now(), archive_key = :archive_key,
        archive_size_bytes = :size, archive_sha256 = :sha256, layers = CAST(:layers AS jsonb),
        counts = CAST(:counts AS jsonb), duration_ms = :duration_ms
    WHERE id = :id
    RETURNING published_at
    """
)
PRUNE_CANDIDATES_SQL = text(
    """
    SELECT id, label, archive_key FROM publish_versions
    WHERE municipality_id = :m AND NOT is_current AND archive_pruned_at IS NULL
    ORDER BY id DESC OFFSET :keep
    """
)
PRUNE_ROWS_SQL = (
    text("DELETE FROM parcel_links WHERE publish_version_id = :id"),
    text("DELETE FROM heatmap_cells WHERE publish_version_id = :id"),
    text("DELETE FROM layer_features WHERE publish_version_id = :id"),
    text(
        "UPDATE publish_versions SET archive_pruned_at = now(), archive_key = NULL WHERE id = :id"
    ),
)


# --- the pipeline --------------------------------------------------------------------------------


@dataclass(slots=True)
class PublishOutcome:
    version_id: int
    label: str
    archive_key: str | None
    layers: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)
    skipped_items: list[dict[str, Any]] = field(default_factory=list)
    pruned_versions: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0


class PublishPipeline:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        storage: ArchiveStorage,
        tile_builder: TileBuilder,
        municipality_id: str,
        min_overlap_m2: float = 1.0,
        min_overlap_fraction: float = 0.02,
        keep_versions: int = 3,
        min_zoom: int = 8,
        max_zoom: int = 16,
        layers: tuple[LayerSpec, ...] = LAYERS,
        tmp_dir: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.tile_builder = tile_builder
        self.municipality_id = municipality_id
        self.min_overlap_m2 = float(min_overlap_m2)
        self.min_overlap_fraction = float(min_overlap_fraction)
        self.keep_versions = max(2, int(keep_versions))
        self.min_zoom = int(min_zoom)
        self.max_zoom = int(max_zoom)
        self.layers = layers
        self.tmp_dir = tmp_dir
        self.clock = clock

    # --- entry point -----------------------------------------------------------------------------

    async def run(self, job: JobContext) -> JobResult:
        started = perf_counter()
        progress = Progress(job, self.clock)
        payload = dict(job.payload or {})
        work_dir = Path(tempfile.mkdtemp(prefix=f"publish-{job.id}-", dir=self.tmp_dir))
        current = "preflight"
        m = self.municipality_id
        try:
            async with self.session_factory() as session:
                await progress.start("preflight")
                blockers = await self._pending(session)
                if blockers:
                    names = ", ".join(
                        f"{b['document_name']} ({b['pending']} pending)" for b in blockers
                    )
                    raise PublishBlocked(f"items pending review: {names}")
                previous = (await session.execute(CURRENT_VERSION_SQL, {"m": m})).mappings().first()
                await progress.done(
                    "preflight", {"previous_version": previous and previous["label"]}
                )

                current = "version"
                await progress.start(current)
                label = payload.get("label") or await self._next_label(session)
                version_id = int(
                    (
                        await session.execute(
                            INSERT_VERSION_SQL,
                            {
                                "m": m,
                                "label": label,
                                "by": payload.get("requested_by") or "system",
                                "formula_version": FORMULA_VERSION,
                                "notes": payload.get("notes"),
                                "previous_id": previous["id"] if previous else None,
                                "job_id": job.id,
                                "min_zoom": self.min_zoom,
                                "max_zoom": self.max_zoom,
                            },
                        )
                    ).scalar_one()
                )
                outcome = PublishOutcome(version_id=version_id, label=label, archive_key=None)
                await progress.done(current, {"version_id": version_id, "label": label})

                current = "values"
                await progress.start(current)
                counts = outcome.counts
                counts.update(
                    await self._publish_values(
                        session, version_id, previous["id"] if previous else None, outcome
                    )
                )
                await progress.done(
                    current,
                    {
                        k: counts[k]
                        for k in (
                            "values_carried",
                            "values_published",
                            "items_skipped",
                            "values_rejected",
                        )
                    },
                )

                current = "geometry"
                await progress.start(current)
                counts.update(
                    await self._apply_geometry(
                        session, version_id, previous["id"] if previous else None, label
                    )
                )
                await progress.done(current, {"batches_published": counts["batches_published"]})

                current = "links"
                await progress.start(current)
                counts.update(await self._compute_links(session, version_id))
                await progress.done(
                    current, {k: counts[k] for k in ("parcel_links", "cadastral_unmatched")}
                )

                current = "cells"
                await progress.start(current)
                counts.update(await self._compute_cells(session, version_id))
                await progress.done(current, {k: counts[k] for k in ("block_cells", "zone_cells")})

                current = "export"
                await progress.start(current)
                layer_files = await self._export_layers(session, version_id, work_dir)
                outcome.layers = [
                    {
                        "id": lf.layer_id,
                        "geometry_type": lf.geometry_type,
                        "min_zoom": lf.min_zoom,
                        "max_zoom": lf.max_zoom,
                        "features": lf.feature_count,
                    }
                    for lf in layer_files
                ]
                counts["features_exported"] = sum(lf.feature_count for lf in layer_files)
                await progress.done(current, {"features": counts["features_exported"]})

                current = "tiles"
                await progress.start(current)
                archive = work_dir / f"{label}.pmtiles"
                report = await asyncio.to_thread(
                    self.tile_builder.build,
                    [lf for lf in layer_files if lf.feature_count > 0],
                    archive,
                )
                await progress.done(current, {"size_bytes": report.size_bytes, "tool": report.tool})

                current = "upload"
                await progress.start(current)
                archive_key = f"{m}/tiles/{version_id}/{label}.pmtiles"
                await asyncio.to_thread(
                    self.storage.put_file, archive_key, report.archive, ARCHIVE_CONTENT_TYPE
                )
                outcome.archive_key = archive_key
                await progress.done(current, {"archive_key": archive_key})

                current = "flip"
                await progress.start(current)
                outcome.duration_ms = int((perf_counter() - started) * 1000)
                await session.execute(UNSET_CURRENT_SQL, {"m": m})
                await session.execute(
                    FLIP_SQL,
                    {
                        "id": version_id,
                        "archive_key": archive_key,
                        "size": report.size_bytes,
                        "sha256": report.sha256,
                        "layers": json.dumps(outcome.layers),
                        "counts": json.dumps(counts),
                        "duration_ms": outcome.duration_ms,
                    },
                )
                await write_audit(
                    session,
                    municipality_id=m,
                    action="publish.complete",
                    actor=payload.get("requested_by") or "system",
                    entity_type="publish_version",
                    entity_id=version_id,
                    details={
                        "label": label,
                        "job_id": job.id,
                        "archive_key": archive_key,
                        "counts": counts,
                        "skipped_items": outcome.skipped_items,
                    },
                    before={
                        "current_version_id": previous["id"] if previous else None,
                        "current_label": previous["label"] if previous else None,
                    },
                    after={"current_version_id": version_id, "current_label": label},
                )
                await session.commit()
                await progress.done(current, {"current_version": label})
        except Exception as exc:
            await progress.fail(current, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        current = "prune"
        await progress.start(current)
        outcome.pruned_versions = await self._prune()
        await progress.done(current, {"pruned": [p["label"] for p in outcome.pruned_versions]})
        outcome.duration_ms = int((perf_counter() - started) * 1000)
        log.info(
            "publish job %s: version %s (%s) is current after %s ms",
            job.id,
            version_id,
            label,
            outcome.duration_ms,
        )
        return JobResult(
            result={
                "version_id": outcome.version_id,
                "label": outcome.label,
                "archive_key": outcome.archive_key,
                "layers": outcome.layers,
                "counts": outcome.counts,
                "skipped_items": outcome.skipped_items,
                "pruned_versions": outcome.pruned_versions,
                "duration_ms": outcome.duration_ms,
            }
        )

    # --- steps -----------------------------------------------------------------------------------

    async def _pending(self, session: AsyncSession) -> list[dict[str, Any]]:
        rows = (await session.execute(PENDING_SQL, {"m": self.municipality_id})).mappings().all()
        return [dict(r) for r in rows]

    async def _next_label(self, session: AsyncSession) -> str:
        today = self.clock()
        rows = await session.execute(
            LABELS_SQL, {"m": self.municipality_id, "prefix": today.strftime("%Y-%m-%d") + ".%"}
        )
        return next_label([r[0] for r in rows], today)

    async def _publish_values(
        self,
        session: AsyncSession,
        version_id: int,
        previous_id: int | None,
        outcome: PublishOutcome,
    ) -> dict[str, int]:
        m = self.municipality_id
        carried = 0
        if previous_id is not None:
            result = await session.execute(
                CARRY_FORWARD_SQL, {"m": m, "new_version": version_id, "prev_version": previous_id}
            )
            carried = result.rowcount or 0
        eligible = (await session.execute(text(ELIGIBLE_ITEMS_SQL), {"m": m})).mappings().all()
        published = 0
        for item in eligible:
            value_id = (
                await session.execute(
                    INSERT_VALUE_SQL,
                    {
                        "m": m,
                        "document_id": item["document_id"],
                        "urban_parcel_id": item["urban_parcel_id"],
                        "block_id": item["block_id"],
                        "zone_id": item["zone_id"],
                        "field_key": item["field_key"],
                        "value_text": item["value_text"],
                        "value_number": item["value_number"],
                        "unit": item["unit"],
                        "source_page": item["source_page"],
                        "source_bbox": json.dumps(item["source_bbox"])
                        if item["source_bbox"] is not None
                        else None,
                        "source_note": item["source_note"],
                        "new_version": version_id,
                    },
                )
            ).scalar_one()
            await session.execute(CLOSE_ITEM_SQL, {"value_id": value_id, "id": item["id"]})
            published += 1
        gaps = await session.execute(GAPS_SQL, {"m": m, "v": version_id})
        skipped = (await session.execute(SKIPPED_ITEMS_SQL, {"m": m})).mappings().all()
        outcome.skipped_items = [dict(r) for r in skipped]
        for item in outcome.skipped_items:
            log.warning("publish: item %s skipped (%s)", item["id"], item["reason"])
        return {
            "values_carried": carried,
            "values_published": published,
            "items_skipped": len(skipped),
            "values_rejected": gaps.rowcount or 0,
        }

    async def _apply_geometry(
        self, session: AsyncSession, version_id: int, previous_id: int | None, label: str
    ) -> dict[str, Any]:
        m = self.municipality_id
        batches = (await session.execute(STAGED_BATCHES_SQL, {"m": m})).mappings().all()
        counts: dict[str, Any] = {"batches_published": 0, "batches_superseded": 0, "geometry": {}}
        newest_generic: dict[str, int] = {}
        for batch in batches:
            if batch["layer_id"] in GENERIC_LAYER_IDS:
                newest_generic[batch["layer_id"]] = batch["id"]  # ordered by id: the last wins
        for batch in batches:
            layer_id = batch["layer_id"]
            if layer_id not in STAGED_LAYERS:
                raise ValueError(f"staged batch {batch['id']} has unknown layer {layer_id!r}")
            if layer_id in GENERIC_LAYER_IDS and newest_generic[layer_id] != batch["id"]:
                await session.execute(
                    BATCH_PUBLISHED_SQL,
                    {"status": "superseded", "v": version_id, "id": batch["id"]},
                )
                counts["batches_superseded"] += 1
                continue
            params = {"batch": batch["id"], "label": label, "v": version_id}
            if layer_id in GENERIC_LAYER_IDS:
                result = await session.execute(COPY_GENERIC_SQL, params)
                touched = result.rowcount or 0
            else:
                touched = 0
                for statement in UPSERT_SQL[layer_id]:
                    result = await session.execute(text(statement), params)
                    touched += result.rowcount or 0
            counts["geometry"][layer_id] = counts["geometry"].get(layer_id, 0) + touched
            await session.execute(
                BATCH_PUBLISHED_SQL, {"status": "published", "v": version_id, "id": batch["id"]}
            )
            counts["batches_published"] += 1
        if previous_id is not None:
            for layer_id in GENERIC_LAYER_IDS:
                if layer_id not in newest_generic:
                    result = await session.execute(
                        CARRY_GENERIC_SQL,
                        {"m": m, "v": version_id, "prev": previous_id, "layer": layer_id},
                    )
                    if result.rowcount:
                        counts["geometry"][f"{layer_id}_carried"] = result.rowcount
        return counts

    async def _compute_links(self, session: AsyncSession, version_id: int) -> dict[str, int]:
        return await recompute_parcel_links(
            session,
            municipality_id=self.municipality_id,
            version_id=version_id,
            min_overlap_m2=self.min_overlap_m2,
            min_overlap_fraction=self.min_overlap_fraction,
        )

    async def _compute_cells(self, session: AsyncSession, version_id: int) -> dict[str, int]:
        m = self.municipality_id
        market_rows = (await session.execute(ASSUMPTIONS_SQL, {"m": m})).mappings().all()
        market_by_zone: dict[int | None, MarketInputs] = {
            row["zone_id"]: market_inputs_from_row(row) for row in market_rows
        }
        default_market = market_by_zone.get(None)
        bands = price_bands(
            {zone_id: mk.sale_rate_eur_m2 for zone_id, mk in market_by_zone.items() if zone_id}
        )
        parcels = (
            (await session.execute(PARCEL_ZONE_SQL, {"m": m, "v": version_id})).mappings().all()
        )
        by_block: dict[int, list[ParcelFigures]] = {}
        by_zone: dict[int, list[ParcelFigures]] = {}
        for p in parcels:
            zone_id = p["effective_zone_id"]
            market = market_by_zone.get(zone_id, default_market) if zone_id else default_market
            result = compute_feasibility(
                p["area_m2"], p["max_far"], p["max_site_coverage_pct"], market, Assumptions()
            )
            figures = ParcelFigures(
                area_m2=float(p["area_m2"] or 0.0),
                max_far=p["max_far"],
                max_site_coverage_pct=p["max_site_coverage_pct"],
                max_height_m=p["max_height_m"],
                max_gfa_m2=expected_or_none(result, "max_gfa_m2"),
                saleable_area_m2=expected_or_none(result, "saleable_area_m2"),
                market_value_eur=expected_or_none(result, "revenue_eur"),
            )
            if p["block_id"] is not None:
                by_block.setdefault(p["block_id"], []).append(figures)
            if zone_id is not None:
                by_zone.setdefault(zone_id, []).append(figures)
        written = {"block_cells": 0, "zone_cells": 0}
        for cell_type, groups in (("block", by_block), ("zone", by_zone)):
            cells = (await session.execute(ALL_CELL_IDS_SQL[cell_type], {"m": m})).mappings().all()
            for cell in cells:
                figures = aggregate_cell(groups.get(cell["id"], []))
                zone_id = cell["zone_id"]
                market = market_by_zone.get(zone_id, default_market) if zone_id else default_market
                await session.execute(
                    INSERT_CELL_SQL[cell_type],
                    {
                        "m": m,
                        "v": version_id,
                        "cell_id": cell["id"],
                        "parcel_count": figures.parcel_count,
                        "stated_count": figures.stated_count,
                        "max_site_coverage_pct": figures.max_site_coverage_pct,
                        "max_height_m": figures.max_height_m,
                        "max_far": figures.max_far,
                        "max_gfa_m2": figures.max_gfa_m2,
                        "saleable_area_m2": figures.saleable_area_m2,
                        "sale_rate_eur_m2": market.sale_rate_eur_m2 if market else None,
                        "sale_rate_low_eur_m2": sale_rate_range(market)[0],
                        "sale_rate_high_eur_m2": sale_rate_range(market)[1],
                        "market_value_eur": figures.market_value_eur,
                        "price_band": bands.get(zone_id) if zone_id else None,
                    },
                )
                written[f"{cell_type}_cells"] += 1
        return written

    async def _export_layers(
        self, session: AsyncSession, version_id: int, work_dir: Path
    ) -> list[LayerFile]:
        files: list[LayerFile] = []
        for spec in self.layers:
            path = work_dir / f"{spec.id}.geojson"
            count = 0
            with path.open("w", encoding="utf-8") as handle:
                result = await session.stream(
                    text(spec.sql), {"m": self.municipality_id, "v": version_id}
                )
                async for row in result:
                    handle.write(json.dumps(row[0], ensure_ascii=False, separators=(",", ":")))
                    handle.write("\n")
                    count += 1
            files.append(
                LayerFile(
                    layer_id=spec.id,
                    path=path,
                    feature_count=count,
                    geometry_type=spec.geometry_type,
                    min_zoom=max(self.min_zoom, spec.min_zoom),
                    max_zoom=min(self.max_zoom, spec.max_zoom),
                )
            )
            log.info("publish: exported %s features of %s", count, spec.id)
        return files

    async def _prune(self) -> list[dict[str, Any]]:
        """Retention: keep the current version plus ``keep_versions - 1`` older ones with their
        archives and derived rows; older versions lose the archive object and the derived rows
        (values and the version row stay for history). Best effort: never fails the publish."""
        pruned: list[dict[str, Any]] = []
        try:
            async with self.session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            PRUNE_CANDIDATES_SQL,
                            {"m": self.municipality_id, "keep": self.keep_versions - 1},
                        )
                    )
                    .mappings()
                    .all()
                )
                for row in rows:
                    if row["archive_key"]:
                        await asyncio.to_thread(self.storage.delete, row["archive_key"])
                    for statement in PRUNE_ROWS_SQL:
                        await session.execute(statement, {"id": row["id"]})
                    pruned.append(
                        {
                            "version_id": row["id"],
                            "label": row["label"],
                            "archive_key": row["archive_key"],
                        }
                    )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - retention must never undo a publish
            log.warning("publish: retention failed: %s: %s", type(exc).__name__, exc)
        return pruned
