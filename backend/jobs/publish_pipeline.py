"""The publish pipeline: everything approved → a new serving version → tiles → pointer flip.

One database transaction covers the whole run (the flip is the commit), so visitors keep
seeing the previous version until the new one is complete, and a failure anywhere leaves nothing
behind but the job's error (an archive already uploaded is deleted again). Steps, each reported
to ``pipeline_jobs.progress``:

1. ``preflight``: no document may still have items pending review, and no staged geometry batch
   may wait for the reviewer's decision (geometry review, 0033); a hard failure otherwise;
2. ``version``: a new ``publish_versions`` row (not current yet) with the municipality's next
   ``version_no``;
3. ``values``: the previous version's serving values carried forward, overridden by the
   approved / amended review items (the amended value wins; every item cites its page), which
   are closed with ``published_value_id`` and ``published_version_id``; zone / block / document /
   parcel scopes; fields whose extracted value was rejected and nothing replaced become
   ``planning_value_gaps`` rows (the public panel says ``rejected`` without reading the review
   queue);
4. ``geometry``: the staged batches a reviewer approved applied (rejected ones never are):
   entity layers upserted by natural key (stable ids),
   generic layers copied into ``layer_features`` for the version (untouched layers carried
   forward), document coverage updated; batches marked published; zone, cadastral and
   georeferencing datasets whose batches were applied marked published;
5. ``links``: cadastral ↔ planned parcel overlaps recomputed with location resolution's
   thresholds (rank 1 = the panel's primary), unmatched cadastral parcels counted;
6. ``cells``: the heatmap surfaces (``core.choropleth``): coverage, FAR, floors and GFA per urban
   block from the effective parameters, the sale price per zone from the assumptions version that
   applies today, each layer's classes stored with its cells;
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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.services.audit import write_audit
from core.cadastre.dataset import apply_cadastral_datasets
from core.choropleth import compute_choropleth
from core.engine.shared import FORMULA_VERSION
from core.gis.georef.stage import apply_georef_datasets
from core.parcel_links import LinkRules, recompute_parcel_links
from core.zones.staging import apply_zone_datasets
from jobs.base import JobContext, JobResult
from jobs.publish_layers import (
    GENERIC_LAYER_IDS,
    LAYERS,
    STAGED_LAYERS,
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


def next_label(existing: Iterable[str], today: datetime) -> str:
    """``YYYY-MM-DD.n``: the first n not taken for the day."""
    prefix = today.strftime("%Y-%m-%d")
    taken = set(existing)
    n = 1
    while f"{prefix}.{n}" in taken:
        n += 1
    return f"{prefix}.{n}"


# --- SQL -----------------------------------------------------------------------------------------

PENDING_SQL = text(
    """
    SELECT d.id AS document_id, d.name AS document_name, count(*) AS pending
    FROM planning_parameter_extractions e
    JOIN planning_documents d ON d.id = e.document_id
    WHERE e.municipality_id = :m AND e.review_state = 'pending_review'
      AND e.superseded_at IS NULL
    GROUP BY d.id, d.name ORDER BY d.name, d.id
    """
)
# Staged geometry still waiting for the reviewer's decision (geometry review, 0033): publishing
# waits for it as it waits for pending values.
GEOMETRY_PENDING_SQL = text(
    """
    SELECT b.id AS batch_id, b.layer_id, b.document_id, d.name AS document_name,
           b.dataset_version, b.qa_status
    FROM geometry_batches b
    LEFT JOIN planning_documents d ON d.id = b.document_id
    WHERE b.municipality_id = :m AND b.status = 'staged'
      AND COALESCE(b.review_state, 'pending_review') = 'pending_review'
    ORDER BY b.id
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
    INSERT INTO publish_versions (municipality_id, label, version_no, published_by,
                                  formula_version, notes, is_current, previous_version_id, job_id,
                                  min_zoom, max_zoom)
    VALUES (:m, :label,
            (SELECT COALESCE(max(version_no), 0) + 1 FROM publish_versions
             WHERE municipality_id = :m),
            :by, :formula_version, :notes, false, :previous_id, :job_id, :min_zoom, :max_zoom)
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
           COALESCE(e.amended_unit, e.unit) AS unit, e.source_page, e.source_bbox, e.source_note,
           COALESCE((SELECT r.file_id FROM extraction_runs r WHERE r.id = e.run_id), d.file_id)
               AS source_file_id
    FROM planning_parameter_extractions e
    JOIN planning_documents d ON d.id = e.document_id AND d.is_current_version
    JOIN planning_fields f ON f.key = e.field_key AND NOT f.computed
    WHERE e.municipality_id = :m AND e.review_state IN ('approved', 'amended')
      AND e.published_value_id IS NULL AND e.entity_type <> 'market_data'
      AND e.superseded_at IS NULL AND e.source_page IS NOT NULL
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
      AND e.published_value_id IS NULL AND e.superseded_at IS NULL
      AND e.id NOT IN (SELECT id FROM ({ELIGIBLE_ITEMS_SQL}) eligible)
    ORDER BY e.id
    """
)
CARRY_FORWARD_SQL = text(
    f"""
    WITH eligible AS ({ELIGIBLE_ITEMS_SQL})
    INSERT INTO planning_parameter_values (municipality_id, document_id, urban_parcel_id,
        block_id, zone_id, field_key, value_text, value_number, unit, source_page, source_bbox,
        source_note, source_file_id, publish_version_id, dataset_version)
    SELECT v.municipality_id, v.document_id, v.urban_parcel_id, v.block_id, v.zone_id,
           v.field_key, v.value_text, v.value_number, v.unit, v.source_page, v.source_bbox,
           v.source_note, v.source_file_id, :new_version, v.dataset_version
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
        source_note, source_file_id, publish_version_id)
    VALUES (:m, :document_id, :urban_parcel_id, :block_id, :zone_id, :field_key, :value_text,
            :value_number, :unit, :source_page, CAST(:source_bbox AS jsonb), :source_note,
            :source_file_id, :new_version)
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
          AND e.superseded_at IS NULL
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
    "UPDATE planning_parameter_extractions "
    "SET published_value_id = :value_id, published_version_id = :version_id WHERE id = :id"
)
STAGED_BATCHES_SQL = text(
    """
    SELECT id, layer_id, feature_count FROM geometry_batches
    WHERE municipality_id = :m AND status = 'staged' AND review_state = 'approved'
    ORDER BY layer_id, id
    """
)
# whether any served parcel carries each cadastral flag (from a confirmed eKatastar extract)
FLAGS_LOADED_SQL = text(
    """
    SELECT COALESCE(bool_or(public_ownership IS NOT NULL), false) AS public_ownership,
           COALESCE(bool_or(restitution_or_legal_burden IS NOT NULL), false)
               AS restitution_or_legal_burden
    FROM cadastral_parcels WHERE municipality_id = :m AND retired_at IS NULL
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
    # the flags stay null unless the batch sets them (a confirmed eKatastar extract, core.cadastre):
    # never a default; a parcel present in the batch is served again if it had been retired
    "cadastral_parcels": [
        f"""
        INSERT INTO cadastral_parcels (municipality_id, parcel_number, sub_number, ko_name, ko_code,
            street_address, geom, area_m2, public_ownership, restitution_or_legal_burden,
            dataset_version)
        SELECT s.municipality_id, s.properties->>'parcel_number',
               NULLIF(s.properties->>'sub_number', ''), s.properties->>'ko_name',
               NULLIF(s.properties->>'ko_code', ''),
               s.properties->>'street_address', {_MULTIPOLYGON},
               COALESCE(CAST(s.properties->>'area_m2' AS double precision), {_AREA}),
               CAST(s.properties->>'public_ownership' AS boolean),
               CAST(s.properties->>'restitution_or_legal_burden' AS boolean),
               COALESCE(s.properties->>'dataset_version', :label)
        FROM staging_geometry s WHERE s.batch_id = :batch
        ON CONFLICT (municipality_id, lower(ko_name), parcel_number, COALESCE(sub_number, ''::text))
        DO UPDATE SET ko_name = EXCLUDED.ko_name, ko_code = EXCLUDED.ko_code,
                      street_address = EXCLUDED.street_address, geom = EXCLUDED.geom,
                      area_m2 = EXCLUDED.area_m2, public_ownership = EXCLUDED.public_ownership,
                      restitution_or_legal_burden = EXCLUDED.restitution_or_legal_burden,
                      dataset_version = EXCLUDED.dataset_version,
                      retired_at = NULL, retired_dataset_version = NULL
        """
    ],
    "cadastral_municipalities": [
        f"""
        INSERT INTO cadastral_municipalities (municipality_id, ko_name, ko_code, geom,
            parcel_count, boundary_source, dataset_version)
        SELECT s.municipality_id, s.properties->>'ko_name', NULLIF(s.properties->>'ko_code', ''),
               {_MULTIPOLYGON}, COALESCE(CAST(s.properties->>'parcel_count' AS integer), 0),
               COALESCE(s.properties->>'boundary_source', 'derived_from_parcels'),
               COALESCE(s.properties->>'dataset_version', :label)
        FROM staging_geometry s WHERE s.batch_id = :batch
        ON CONFLICT (municipality_id, lower(ko_name))
        DO UPDATE SET ko_name = EXCLUDED.ko_name,
                      ko_code = COALESCE(EXCLUDED.ko_code, cadastral_municipalities.ko_code),
                      geom = EXCLUDED.geom, parcel_count = EXCLUDED.parcel_count,
                      boundary_source = EXCLUDED.boundary_source,
                      dataset_version = EXCLUDED.dataset_version, updated_at = now()
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
                  AND b.block_ref = s.properties->>'block_ref'
                ORDER BY ST_Intersects(b.geom, s.geom) DESC, b.id LIMIT 1),
               CAST(s.properties->>'document_id' AS bigint), :label
        FROM staging_geometry s WHERE s.batch_id = :batch
        ON CONFLICT (document_id, urban_parcel_number)
        DO UPDATE SET geom = EXCLUDED.geom, area_m2 = EXCLUDED.area_m2,
                      block_id = COALESCE(EXCLUDED.block_id, urban_parcels.block_id),
                      dataset_version = EXCLUDED.dataset_version
        """
    ],
    # a block's letter recurs across plans ("Blok A" of two documents): a staged block updates
    # the block of that ref it overlaps, else it is a new block
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
          AND b.block_ref = s.properties->>'block_ref' AND ST_Intersects(b.geom, s.geom)
        """,
        f"""
        INSERT INTO urban_blocks (municipality_id, block_ref, geom, zone_id, dataset_version)
        SELECT s.municipality_id, s.properties->>'block_ref', {_MULTIPOLYGON},
               (SELECT z.id FROM zones z WHERE z.municipality_id = s.municipality_id
                AND z.name = s.properties->>'zone_name' ORDER BY z.id LIMIT 1), :label
        FROM staging_geometry s
        WHERE s.batch_id = :batch AND NOT EXISTS (
            SELECT 1 FROM urban_blocks b WHERE b.municipality_id = s.municipality_id
              AND b.block_ref = s.properties->>'block_ref' AND ST_Intersects(b.geom, s.geom))
        """,
    ],
    "zones": [
        # a zone dataset (core.zones) carries a stable zone_key: match by it, or take over a
        # legacy row of the same name once (it then keeps the key); the dataset is the truth for
        # the zone's attributes, so empty values clear them
        f"""
        UPDATE zones z
        SET geom = {_MULTIPOLYGON},
            name = s.properties->>'name',
            zone_key = s.properties->>'zone_key',
            general_planning_summary = NULLIF(s.properties->>'general_planning_summary', ''),
            zone_type = CASE WHEN s.properties->>'zone_type' IN ('res', 'com', 'mix', 'pub', 'grn')
                             THEN s.properties->>'zone_type' END,
            notes = NULLIF(s.properties->>'notes', ''),
            no_adopted_plan = COALESCE(CAST(s.properties->>'no_adopted_plan' AS boolean), false),
            dataset_version = :label
        FROM staging_geometry s
        WHERE s.batch_id = :batch AND (s.properties->>'zone_key') IS NOT NULL
          AND z.municipality_id = s.municipality_id
          AND (z.zone_key = s.properties->>'zone_key'
               OR (z.zone_key IS NULL AND z.name = s.properties->>'name'
                   AND NOT EXISTS (SELECT 1 FROM zones k
                                   WHERE k.municipality_id = s.municipality_id
                                     AND k.zone_key = s.properties->>'zone_key')))
        """,
        # staged by name only (the GIS ingestion contract): keep what the batch does not say
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
        WHERE s.batch_id = :batch AND (s.properties->>'zone_key') IS NULL
          AND z.municipality_id = s.municipality_id AND z.name = s.properties->>'name'
        """,
        f"""
        INSERT INTO zones (municipality_id, name, geom, general_planning_summary, zone_type,
                           zone_key, notes, no_adopted_plan, dataset_version)
        SELECT s.municipality_id, s.properties->>'name', {_MULTIPOLYGON},
               NULLIF(s.properties->>'general_planning_summary', ''),
               CASE WHEN s.properties->>'zone_type' IN ('res', 'com', 'mix', 'pub', 'grn')
                    THEN s.properties->>'zone_type' END,
               s.properties->>'zone_key', NULLIF(s.properties->>'notes', ''),
               COALESCE(CAST(s.properties->>'no_adopted_plan' AS boolean), false),
               :label
        FROM staging_geometry s
        WHERE s.batch_id = :batch AND NOT EXISTS (
            SELECT 1 FROM zones z WHERE z.municipality_id = s.municipality_id
              AND CASE WHEN (s.properties->>'zone_key') IS NOT NULL
                       THEN z.zone_key = s.properties->>'zone_key'
                       ELSE z.name = s.properties->>'name' END)
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
REFRESH_VERSION_SQL = text(
    """
    SELECT id, label, archive_key FROM publish_versions
    WHERE municipality_id = :m AND is_current FOR UPDATE
    """
)
REFRESH_ARCHIVE_SQL = text(
    """
    UPDATE publish_versions
    SET archive_size_bytes = :size, archive_sha256 = :sha256, layers = CAST(:layers AS jsonb),
        counts = COALESCE(counts, '{}'::jsonb) || CAST(:counts AS jsonb)
    WHERE id = :id
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
    text("DELETE FROM choropleth_cells WHERE publish_version_id = :id"),
    text("DELETE FROM choropleth_classes WHERE publish_version_id = :id"),
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
        link_rules: LinkRules | None = None,
        price_breaks: Sequence[float] = (),
        keep_versions: int = 3,
        min_zoom: int = 8,
        max_zoom: int = 16,
        layers: tuple[LayerSpec, ...] = LAYERS,
        tmp_dir: str | None = None,
        timezone: str = "UTC",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.tile_builder = tile_builder
        self.municipality_id = municipality_id
        self.timezone = timezone
        self.link_rules = link_rules or LinkRules(
            min_overlap_m2=float(min_overlap_m2), min_overlap_fraction=float(min_overlap_fraction)
        )
        self.price_breaks = tuple(float(b) for b in price_breaks)
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
        uploaded: str | None = None  # an archive uploaded for a version not (yet) committed
        try:
            async with self.session_factory() as session:
                await progress.start("preflight")
                blockers = await self._pending(session)
                if blockers:
                    names = ", ".join(
                        f"{b['document_name']} ({b['pending']} pending)" for b in blockers
                    )
                    raise PublishBlocked(f"items pending review: {names}")
                geometry = (await session.execute(GEOMETRY_PENDING_SQL, {"m": m})).all()
                if geometry:
                    raise PublishBlocked(
                        f"geometry pending review: {len(geometry)} staged batch(es) "
                        f"({', '.join(str(g.batch_id) for g in geometry[:10])})"
                    )
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
                links = await self._compute_links(session, version_id)
                counts.update(
                    parcel_links=links["parcel_links"],
                    cadastral_unmatched=links["cadastral_unmatched"],
                    link_relations=links["relations"],
                    links_duration_ms=links["duration_ms"],
                )
                await progress.done(
                    current,
                    {
                        k: links[k]
                        for k in ("parcel_links", "cadastral_unmatched", "relations", "duration_ms")
                    },
                )

                current = "cells"
                await progress.start(current)
                heat = await self._compute_cells(session, version_id)
                counts["choropleth_cells"] = {k: v["count"] for k, v in heat.items()}
                await progress.done(current, counts["choropleth_cells"])

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
                        "available": lf.available,
                        "unavailable_reason": lf.unavailable_reason,
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
                uploaded = archive_key
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
                uploaded = None
                await progress.done(current, {"current_version": label})
        except Exception as exc:
            await progress.fail(current, f"{type(exc).__name__}: {exc}")
            if uploaded:  # the version was rolled back: its archive would be served by nothing
                try:
                    await asyncio.to_thread(self.storage.delete, uploaded)
                except Exception as cleanup:  # noqa: BLE001 - the publish error is what matters
                    log.warning("publish: could not delete %s: %s", uploaded, cleanup)
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
                        "source_file_id": item["source_file_id"],
                        "new_version": version_id,
                    },
                )
            ).scalar_one()
            await session.execute(
                CLOSE_ITEM_SQL,
                {"value_id": value_id, "version_id": version_id, "id": item["id"]},
            )
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
        zone_batches: list[int] = []
        cadastral_batches: list[int] = []
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
            if layer_id == "zones":
                zone_batches.append(batch["id"])
            if layer_id == "cadastral_parcels":
                cadastral_batches.append(batch["id"])
        # the planning-document list of a zone dataset follows its zones (core.zones.staging)
        zone_documents = await apply_zone_datasets(
            session, municipality_id=m, version_id=version_id, published_batch_ids=zone_batches
        )
        if zone_documents["zone_datasets"]:
            counts["zone_documents"] = zone_documents
        # an imported cadastral dataset retires the parcels its KOs no longer contain
        cadastre = await apply_cadastral_datasets(
            session, municipality_id=m, version_id=version_id, published_batch_ids=cadastral_batches
        )
        if cadastre["cadastral_datasets"]:
            counts["cadastre"] = cadastre
        # a georeferenced document (core.gis.georef) is published once its batches are applied
        georef = await apply_georef_datasets(session, municipality_id=m, version_id=version_id)
        if georef["georef_datasets"]:
            counts["georef"] = georef
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

    async def _compute_links(self, session: AsyncSession, version_id: int) -> dict[str, Any]:
        return await recompute_parcel_links(
            session,
            municipality_id=self.municipality_id,
            version_id=version_id,
            rules=self.link_rules,
        )

    async def refresh(self, job: JobContext) -> JobResult:
        """``refresh_heatmaps``: the current version's heatmaps recomputed (the sale price from the
        assumptions versions that apply today) and its archive rebuilt around them in place, so
        the map follows another assumptions version without a new publish. The version, its
        values and links stay; a version without an archive (the seeded one) gets its cells only.
        """
        started = perf_counter()
        m = self.municipality_id
        work_dir = Path(tempfile.mkdtemp(prefix=f"heatmaps-{job.id}-", dir=self.tmp_dir))
        try:
            async with self.session_factory() as session:
                row = (await session.execute(REFRESH_VERSION_SQL, {"m": m})).mappings().first()
                if row is None:
                    return JobResult(result={"refreshed": False, "reason": "unpublished"})
                version_id, label = int(row["id"]), row["label"]
                heat = await self._compute_cells(session, version_id)
                cells = {k: v["count"] for k, v in heat.items()}
                archive_key = row["archive_key"]
                layers: list[dict[str, Any]] = []
                if archive_key:
                    layer_files = await self._export_layers(session, version_id, work_dir)
                    layers = [
                        {
                            "id": lf.layer_id,
                            "geometry_type": lf.geometry_type,
                            "min_zoom": lf.min_zoom,
                            "max_zoom": lf.max_zoom,
                            "features": lf.feature_count,
                            "available": lf.available,
                            "unavailable_reason": lf.unavailable_reason,
                        }
                        for lf in layer_files
                    ]
                    report = await asyncio.to_thread(
                        self.tile_builder.build,
                        [lf for lf in layer_files if lf.feature_count > 0],
                        work_dir / f"{label}.pmtiles",
                    )
                    # the same key: the pointer and every signed link stay valid, PMTiles
                    # readers see the new ETag and reload
                    await asyncio.to_thread(
                        self.storage.put_file, archive_key, report.archive, ARCHIVE_CONTENT_TYPE
                    )
                    await session.execute(
                        REFRESH_ARCHIVE_SQL,
                        {
                            "id": version_id,
                            "size": report.size_bytes,
                            "sha256": report.sha256,
                            "layers": json.dumps(layers),
                            "counts": json.dumps({"choropleth_cells": cells}),
                        },
                    )
                await write_audit(
                    session,
                    municipality_id=m,
                    action="heatmaps.refresh",
                    actor=(job.payload or {}).get("requested_by") or "system",
                    entity_type="publish_version",
                    entity_id=version_id,
                    details={
                        "label": label,
                        "job_id": job.id,
                        "cells": cells,
                        "archive_rebuilt": bool(archive_key),
                    },
                )
                await session.commit()
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        duration_ms = int((perf_counter() - started) * 1000)
        log.info(
            "heatmaps of version %s (%s) refreshed in %s ms: %s",
            version_id,
            label,
            duration_ms,
            cells,
        )
        return JobResult(
            result={
                "refreshed": True,
                "version_id": version_id,
                "label": label,
                "choropleth_cells": cells,
                "archive_rebuilt": bool(archive_key),
                "layers": layers,
                "duration_ms": duration_ms,
            }
        )

    async def _compute_cells(self, session: AsyncSession, version_id: int) -> dict[str, Any]:
        return await compute_choropleth(
            session,
            municipality_id=self.municipality_id,
            version_id=version_id,
            timezone=self.timezone,
            price_breaks=self.price_breaks,
        )

    async def _export_layers(
        self, session: AsyncSession, version_id: int, work_dir: Path
    ) -> list[LayerFile]:
        files: list[LayerFile] = []
        loaded = (
            (await session.execute(FLAGS_LOADED_SQL, {"m": self.municipality_id})).mappings().one()
        )
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
                    available=spec.requires_flag is None or bool(loaded[spec.requires_flag]),
                    unavailable_reason=(
                        None
                        if spec.requires_flag is None or loaded[spec.requires_flag]
                        else "ownership_data_not_loaded"
                    ),
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
