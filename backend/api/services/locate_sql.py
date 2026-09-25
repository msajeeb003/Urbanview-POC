"""Single-statement PostGIS location resolution.

Both endpoints run ONE statement (one round trip) built from the same body; only the anchor
differs:

- ``LOCATE_POINT_SQL``: the anchor is the query point; the cadastral parcel is the one that
  contains it.
- ``LOCATE_PARCEL_SQL``: the cadastral parcel is looked up by (KO, number, sub-number) and the
  anchor is a point guaranteed inside it (``ST_PointOnSurface``).

Resolution rules:
- coverage / governing document: the *adopted* document whose coverage contains the anchor;
  when several do (a DUP inside a PUP), the most specific one (smallest coverage) governs;
- planned urban parcels: those containing the anchor, plus those overlapping the cadastral parcel
  by at least ``min_overlap_m2`` and ``min_overlap_fraction`` of its area (so the correspondence is
  surfaced even when the point falls in land the plan takes for roads, and digitising slivers are
  ignored); ordered point-match first, governing document first, largest overlap first;
- urban block: the primary urban parcel's block, else the block containing the anchor;
- zone: the governing document's zone, else the zone containing the anchor. Nothing planning-related
  (document, block, zone, urban parcels) is returned for an uncovered anchor.

Every table access is index-backed (GiST on geometry, the unique KO+number index, primary keys).
Parameters: municipality_id, min_overlap_m2, min_overlap_fraction and either (lat, lng) or
(ko, parcel_number, sub_number). All are cast explicitly so NULLs type-check under asyncpg.
"""

from __future__ import annotations

_CADASTRAL_COLUMNS = """c.id, c.parcel_number, c.sub_number, c.ko_name, c.street_address, c.area_m2,
           c.public_ownership, c.restitution_or_legal_burden, c.geom"""

_POINT_ANCHOR = f"""
anchor AS (
    SELECT ST_SetSRID(ST_MakePoint(CAST(:lng AS double precision),
                                   CAST(:lat AS double precision)), 4326) AS pt
),
cad AS (
    SELECT {_CADASTRAL_COLUMNS}
    FROM cadastral_parcels c, anchor a
    WHERE c.municipality_id = :municipality_id
      AND ST_Intersects(c.geom, a.pt)
    ORDER BY ST_Area(c.geom) ASC, c.id ASC
    LIMIT 1
)"""

_PARCEL_ANCHOR = f"""
cad AS (
    SELECT {_CADASTRAL_COLUMNS}
    FROM cadastral_parcels c
    WHERE c.municipality_id = :municipality_id
      AND lower(c.ko_name) = lower(CAST(:ko AS text))
      AND c.parcel_number = CAST(:parcel_number AS text)
      AND coalesce(c.sub_number, '') = coalesce(CAST(:sub_number AS text), '')
    ORDER BY c.id ASC
    LIMIT 1
),
anchor AS (
    SELECT ST_PointOnSurface(geom) AS pt FROM cad
)"""

_BODY = """,
doc AS (
    SELECT d.id, d.name, d.type, d.status::text AS status, d.source, d.source_url, d.zone_id
    FROM planning_documents d, anchor a
    WHERE d.municipality_id = :municipality_id
      AND d.status = 'adopted'
      AND d.coverage_live
      AND ST_Intersects(d.coverage_geom, a.pt)
    ORDER BY ST_Area(d.coverage_geom) ASC, d.id DESC
    LIMIT 1
),
up_candidates AS (
    SELECT u.id
    FROM urban_parcels u, anchor a
    WHERE u.municipality_id = :municipality_id
      AND ST_Intersects(u.geom, a.pt)
    UNION
    SELECT u.id
    FROM urban_parcels u, cad
    WHERE u.municipality_id = :municipality_id
      AND ST_Intersects(u.geom, cad.geom)
),
ups AS (
    SELECT u.id, u.urban_parcel_number, u.area_m2, u.block_id, b.block_ref, u.document_id,
           d.name AS document_name, d.type AS document_type, d.status::text AS document_status,
           ST_Intersects(u.geom, a.pt) AS contains_point,
           (SELECT ST_Area(ST_Intersection(u.geom, cad.geom)::geography) FROM cad) AS overlap_m2,
           ST_Area(u.geom) AS geom_area_deg,
           ST_AsGeoJSON(u.geom, 7)::jsonb AS geometry,
           (u.document_id = (SELECT id FROM doc)) AS governing
    FROM up_candidates uc
    JOIN urban_parcels u ON u.id = uc.id
    JOIN planning_documents d ON d.id = u.document_id AND d.status = 'adopted' AND d.coverage_live
    LEFT JOIN urban_blocks b ON b.id = u.block_id
    CROSS JOIN anchor a
),
ups_kept AS (
    SELECT *
    FROM ups
    WHERE contains_point
       OR (overlap_m2 IS NOT NULL
           AND overlap_m2 >= CAST(:min_overlap_m2 AS double precision)
           AND overlap_m2 >= CAST(:min_overlap_fraction AS double precision)
                             * (SELECT area_m2 FROM cad))
),
primary_up AS (
    SELECT *
    FROM ups_kept
    ORDER BY contains_point DESC, governing DESC NULLS LAST, overlap_m2 DESC NULLS LAST,
             geom_area_deg ASC, id ASC
    LIMIT 1
),
blk AS (
    -- the primary urban parcel's block (by primary key), else the block containing the anchor
    -- (GiST); two index-backed branches instead of an OR that would force a scan
    SELECT id, block_ref, zone_id
    FROM (
        SELECT b.id, b.block_ref, b.zone_id, 0 AS rank, 0.0 AS area
        FROM urban_blocks b
        WHERE EXISTS (SELECT 1 FROM doc)
          AND b.id = (SELECT block_id FROM primary_up)
        UNION ALL
        SELECT b.id, b.block_ref, b.zone_id, 1 AS rank, ST_Area(b.geom) AS area
        FROM urban_blocks b, anchor a
        WHERE EXISTS (SELECT 1 FROM doc)
          AND b.municipality_id = :municipality_id
          AND ST_Intersects(b.geom, a.pt)
    ) AS candidates
    ORDER BY rank ASC, area ASC, id ASC
    LIMIT 1
),
zone_pick AS (
    SELECT COALESCE(
        (SELECT zone_id FROM doc),
        (SELECT z.id
         FROM zones z, anchor a
         WHERE EXISTS (SELECT 1 FROM doc)
           AND z.municipality_id = :municipality_id
           AND ST_Intersects(z.geom, a.pt)
         ORDER BY ST_Area(z.geom) ASC, z.id ASC
         LIMIT 1)
    ) AS zone_id
),
zone AS (
    SELECT z.id, z.name, z.general_planning_summary
    FROM zones z
    WHERE z.id = (SELECT zone_id FROM zone_pick)
),
zone_docs AS (
    SELECT COALESCE(jsonb_agg(
               jsonb_build_object('id', d.id, 'name', d.name, 'type', d.type,
                                  'status', d.status::text)
               ORDER BY (d.status = 'adopted') DESC, d.name ASC), '[]'::jsonb) AS docs
    FROM planning_documents d
    WHERE d.zone_id = (SELECT id FROM zone)
)
SELECT
    (SELECT ST_Y(pt) FROM anchor) AS anchor_lat,
    (SELECT ST_X(pt) FROM anchor) AS anchor_lng,
    (SELECT jsonb_build_object(
        'parcel_id', id, 'parcel_number', parcel_number, 'sub_number', sub_number,
        'ko_name', ko_name, 'street_address', street_address, 'area_m2', area_m2,
        'public_ownership', public_ownership,
        'restitution_or_legal_burden', restitution_or_legal_burden,
        'centroid', jsonb_build_object('lat', ST_Y(ST_Centroid(geom)),
                                       'lng', ST_X(ST_Centroid(geom))),
        'geometry', ST_AsGeoJSON(geom, 7)::jsonb)
     FROM cad) AS cadastral_parcel,
    (SELECT jsonb_build_object('id', id, 'name', name, 'type', type, 'status', status,
                               'source', source, 'source_url', source_url, 'zone_id', zone_id)
     FROM doc) AS planning_document,
    (SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'id', id, 'urban_parcel_number', urban_parcel_number, 'area_m2', area_m2,
        'block_id', block_id, 'block_ref', block_ref,
        'planning_document', jsonb_build_object('id', document_id, 'name', document_name,
                                                'type', document_type,
                                                'status', document_status),
        'match', CASE WHEN contains_point THEN 'point' ELSE 'overlap' END,
        'overlap_m2', overlap_m2, 'geometry', geometry)
        ORDER BY contains_point DESC, governing DESC NULLS LAST, overlap_m2 DESC NULLS LAST,
                 geom_area_deg ASC, id ASC), '[]'::jsonb)
     FROM ups_kept) AS urban_parcels,
    (SELECT jsonb_build_object('id', id, 'block_ref', block_ref, 'zone_id', zone_id)
     FROM blk) AS urban_block,
    (SELECT jsonb_build_object('id', id, 'name', name,
                               'general_planning_summary', general_planning_summary)
     FROM zone) AS zone,
    (SELECT docs FROM zone_docs) AS zone_documents
"""

LOCATE_POINT_SQL = "WITH" + _POINT_ANCHOR + _BODY
LOCATE_PARCEL_SQL = "WITH" + _PARCEL_ANCHOR + _BODY
