"""Single-statement queries of the display-shaped panels (``api.services.parcel_panel``).

Same rules as ``api.services.panel_sql`` (serving tables only, CTEs + ``jsonb_build_object``,
parameters cast explicitly, every access index-backed), with three differences:

- the cadastral ↔ planned parcel links come from ``parcel_links`` of the current publish version
  (computed by the publish job with the locate thresholds; rank 1 = the calculation basis),
  restricted to documents still adopted, live and current, and only for a covered parcel;
- a planning value resolves parcel → block → zone → document (the same precedence as the map's
  ``urban_parcels`` tile layer), each branch on its partial unique index of the current version;
- ``planning_value_gaps`` of the current version say which missing values were rejected in expert
  review (written by the publish job, so the review queue is never read here).

``STAMP_SQL`` is the cache key's input: the current version plus a fingerprint of everything that
changes the panels outside a publish (document status / coverage switch / new versions, current
market assumptions, current zone parameter sets).
"""

from __future__ import annotations

from api.services.panel_sql import (
    _AMENDMENTS,
    _FIELDS,
    _MARKET,
    _MARKET_COLUMN,
    _VERSION,
    _VERSION_COLUMNS,
    _document_ref,
)

_VALUE_COLUMNS = """v.id AS value_id, v.field_key, v.value_text, v.value_number, v.unit,
               v.source_page, v.source_bbox, v.source_note, v.document_id,
               d.name AS document_name, d.source_url AS registry_url, d.file_id"""


def _scoped(table: str, columns: str, join: str) -> str:
    """Rows of ``table`` for the parcel: its own, its block's, its zone's and its document's,
    ranked in that order; each branch hits one partial unique index of the current version."""
    return f"""
    SELECT {columns}, 0 AS rank, 'parcel' AS scope
    FROM {table} v {join}
    WHERE v.publish_version_id = (SELECT id FROM version)
      AND v.urban_parcel_id = (SELECT id FROM primary_up)
    UNION ALL
    SELECT {columns}, 1 AS rank, 'block' AS scope
    FROM {table} v {join}
    WHERE v.publish_version_id = (SELECT id FROM version)
      AND v.block_id = (SELECT id FROM blk)
    UNION ALL
    SELECT {columns}, 2 AS rank, 'zone' AS scope
    FROM {table} v {join}
    WHERE v.publish_version_id = (SELECT id FROM version)
      AND v.zone_id = (SELECT zone_id FROM zone_pick)
    UNION ALL
    SELECT {columns}, 3 AS rank, 'document' AS scope
    FROM {table} v {join}
    WHERE v.publish_version_id = (SELECT id FROM version)
      AND v.urban_parcel_id IS NULL AND v.block_id IS NULL AND v.zone_id IS NULL
      AND v.document_id = (SELECT document_id FROM basis_doc)"""


_LINK_ORDER = "rank ASC, id ASC"

PARCEL_PANEL_SQL = f"""WITH{_VERSION},{_FIELDS},
cad AS (
    SELECT c.id, c.parcel_number, c.sub_number, c.ko_name, c.street_address, c.area_m2,
           c.public_ownership, c.restitution_or_legal_burden, c.geom
    FROM cadastral_parcels c
    WHERE c.id = CAST(:id AS bigint) AND c.municipality_id = :municipality_id
),
anchor AS (
    SELECT ST_PointOnSurface(geom) AS pt FROM cad
),
doc AS (
    -- governing document: adopted, live, contains the anchor, most specific coverage first
    SELECT d.id, d.name, d.type, d.status, d.source, d.source_url, d.zone_id,
           d.amends_document_id, d.coverage_live, d.adopted_on
    FROM planning_documents d, anchor a
    WHERE d.municipality_id = :municipality_id
      AND d.status = 'adopted'
      AND d.coverage_live
      AND ST_Intersects(d.coverage_geom, a.pt)
    ORDER BY ST_Area(d.coverage_geom) ASC, d.id DESC
    LIMIT 1
),
links AS (
    -- published links of the current version, of documents still adopted, live and current
    SELECT u.id, u.urban_parcel_number, u.area_m2, u.document_id, u.block_id,
           l.overlap_m2, l.overlap_fraction, l.area_delta_m2, l.rank,
           {_document_ref("d")} AS document,
           CASE WHEN b.id IS NULL THEN NULL
                ELSE jsonb_build_object('id', b.id, 'block_ref', b.block_ref) END AS urban_block
    FROM parcel_links l
    JOIN urban_parcels u ON u.id = l.urban_parcel_id
    JOIN planning_documents d ON d.id = u.document_id AND d.status = 'adopted'
         AND d.coverage_live AND d.is_current_version
    LEFT JOIN urban_blocks b ON b.id = u.block_id
    WHERE l.publish_version_id = (SELECT id FROM version)
      AND l.cadastral_parcel_id = CAST(:id AS bigint)
      AND EXISTS (SELECT 1 FROM doc)
),
primary_up AS (
    SELECT * FROM links ORDER BY {_LINK_ORDER} LIMIT 1
),
blk AS (
    -- the primary planned parcel's block (by primary key), else the block containing the anchor
    SELECT id, block_ref
    FROM (
        SELECT b.id, b.block_ref, 0 AS rank, 0.0 AS area
        FROM urban_blocks b
        WHERE b.id = (SELECT block_id FROM primary_up)
        UNION ALL
        SELECT b.id, b.block_ref, 1 AS rank, ST_Area(b.geom) AS area
        FROM urban_blocks b, anchor a
        WHERE b.municipality_id = :municipality_id
          AND ST_Intersects(b.geom, a.pt)
    ) AS candidates
    ORDER BY rank ASC, area ASC, id ASC
    LIMIT 1
),
zone_pick AS (
    -- the governing document's zone, else the zone containing the anchor
    SELECT COALESCE(
        (SELECT zone_id FROM doc),
        (SELECT z.id
         FROM zones z, anchor a
         WHERE z.municipality_id = :municipality_id
           AND ST_Intersects(z.geom, a.pt)
         ORDER BY ST_Area(z.geom) ASC, z.id ASC
         LIMIT 1)
    ) AS zone_id
),
zone AS (
    SELECT z.id, z.name FROM zones z WHERE z.id = (SELECT zone_id FROM zone_pick)
),{_AMENDMENTS},
basis_doc AS (
    -- urban basis: the primary planned parcel's document; cadastral basis: the governing one
    SELECT COALESCE((SELECT document_id FROM primary_up), (SELECT id FROM doc)) AS document_id
),
basis_document AS (
    SELECT {_document_ref("d")} AS document
    FROM planning_documents d
    WHERE d.id = (SELECT document_id FROM basis_doc)
),
vals AS ({
    _scoped(
        "planning_parameter_values",
        _VALUE_COLUMNS,
        "JOIN planning_documents d ON d.id = v.document_id",
    )
}
),
values_json AS (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
               'value_id', value_id, 'field_key', field_key, 'value_text', value_text,
               'value_number', value_number, 'unit', unit, 'source_page', source_page,
               'source_bbox', source_bbox, 'source_note', source_note,
               'document_id', document_id, 'document_name', document_name,
               'registry_url', registry_url, 'file_id', file_id, 'scope', scope)
               ORDER BY rank ASC, field_key ASC), '[]'::jsonb) AS vals
    FROM vals
),
gaps AS ({
    _scoped(
        "planning_value_gaps",
        "v.field_key, v.reason, v.document_id",
        "",
    )
}
),
gaps_json AS (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
               'field_key', field_key, 'reason', reason, 'document_id', document_id,
               'scope', scope)
               ORDER BY rank ASC, field_key ASC), '[]'::jsonb) AS gaps
    FROM gaps
),{_MARKET}
SELECT
    (SELECT jsonb_build_object(
        'id', id, 'parcel_number', parcel_number, 'sub_number', sub_number,
        'ko_name', ko_name, 'street_address', street_address, 'area_m2', area_m2,
        'public_ownership', public_ownership,
        'restitution_or_legal_burden', restitution_or_legal_burden,
        'centroid', jsonb_build_object('lat', ST_Y(ST_Centroid(geom)),
                                       'lng', ST_X(ST_Centroid(geom))),
        'bbox', jsonb_build_array(ST_XMin(geom), ST_YMin(geom), ST_XMax(geom), ST_YMax(geom)))
     FROM cad) AS cadastral,
    (SELECT {_document_ref("doc")} FROM doc) AS governing_document,
    (SELECT document FROM basis_document) AS basis_document,
    (SELECT jsonb_build_object('id', id, 'block_ref', block_ref) FROM blk) AS urban_block,
    (SELECT jsonb_build_object('id', id, 'name', name) FROM zone) AS zone,
    (SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'id', id, 'urban_parcel_number', urban_parcel_number, 'area_m2', area_m2,
        'overlap_m2', overlap_m2, 'overlap_fraction', overlap_fraction,
        'area_delta_m2', area_delta_m2, 'rank', rank, 'document', document,
        'urban_block', urban_block)
        ORDER BY {_LINK_ORDER}), '[]'::jsonb)
     FROM links) AS links,
    (SELECT docs FROM amendments) AS amendments,
    (SELECT fields FROM fields) AS fields,
    (SELECT vals FROM values_json) AS "values",
    (SELECT gaps FROM gaps_json) AS gaps,{_MARKET_COLUMN},
    (SELECT id FROM version) AS version_id,{_VERSION_COLUMNS}
"""

ZONE_PANEL_SQL = f"""WITH{_VERSION},
zone AS (
    SELECT z.id, z.name, z.general_planning_summary
    FROM zones z
    WHERE z.id = CAST(:id AS bigint) AND z.municipality_id = :municipality_id
),
docs AS (
    -- current document versions of the zone (earlier registered versions are history)
    SELECT COALESCE(jsonb_agg({_document_ref("d")} || jsonb_build_object(
                   'file_available', d.file_key IS NOT NULL)
               ORDER BY CASE d.status::text WHEN 'adopted' THEN 0 WHEN 'in_progress' THEN 1
                        ELSE 2 END ASC, d.name ASC, d.id ASC), '[]'::jsonb) AS docs,
           count(*) AS documents,
           count(*) FILTER (WHERE d.status = 'adopted') AS adopted,
           count(*) FILTER (WHERE d.status = 'in_progress') AS in_progress,
           count(*) FILTER (WHERE d.status = 'superseded') AS superseded
    FROM planning_documents d
    WHERE d.zone_id = CAST(:id AS bigint)
      AND d.municipality_id = :municipality_id
      AND d.is_current_version
),
typical AS (
    SELECT p.id, p.version, p.land_use, p.max_far, p.max_site_coverage_pct, p.max_height_m,
           p.max_floors, p.notes, p.verified_on, p.verified_by, p.source_page, p.source_note,
           p.source_document_id, d.name AS document_name, d.source_url AS registry_url
    FROM zone_parameter_sets p
    LEFT JOIN planning_documents d ON d.id = p.source_document_id
    WHERE p.zone_id = CAST(:id AS bigint) AND p.is_current
    LIMIT 1
)
SELECT
    (SELECT jsonb_build_object('id', id, 'name', name,
                               'general_planning_summary', general_planning_summary)
     FROM zone) AS zone,
    (SELECT docs FROM docs) AS documents,
    (SELECT jsonb_build_object('documents', documents, 'adopted', adopted,
                               'in_progress', in_progress, 'superseded', superseded)
     FROM docs) AS counts,
    (SELECT jsonb_build_object(
        'id', id, 'version', version, 'land_use', land_use, 'max_far', max_far,
        'max_site_coverage_pct', max_site_coverage_pct, 'max_height_m', max_height_m,
        'max_floors', max_floors, 'notes', notes, 'verified_on', verified_on,
        'verified_by', verified_by, 'source_document_id', source_document_id,
        'document_name', document_name, 'registry_url', registry_url,
        'source_page', source_page, 'source_note', source_note)
     FROM typical) AS typical_parameters,
    (SELECT id FROM version) AS version_id,{_VERSION_COLUMNS}
"""

# The cache key's input (one cheap statement: the documents table holds a few hundred rows).
STAMP_SQL = """
SELECT
    (SELECT id FROM publish_versions WHERE municipality_id = :municipality_id AND is_current)
        AS version_id,
    (SELECT created_at::text FROM publish_versions
     WHERE municipality_id = :municipality_id AND is_current) AS version_created,
    (SELECT md5(COALESCE(string_agg(concat_ws(':', id, name, status::text, coverage_live,
                                              is_current_version, zone_id, amends_document_id,
                                              file_id, file_key, page_count,
                                              page_images_rendered, source_url),
                                    '|' ORDER BY id), ''))
     FROM planning_documents WHERE municipality_id = :municipality_id) AS documents,
    (SELECT md5(COALESCE(string_agg(id::text, ',' ORDER BY id), ''))
     FROM financial_assumptions WHERE municipality_id = :municipality_id AND is_current)
        AS market,
    (SELECT md5(COALESCE(string_agg(id::text, ',' ORDER BY id), ''))
     FROM zone_parameter_sets WHERE municipality_id = :municipality_id AND is_current)
        AS typical
"""
