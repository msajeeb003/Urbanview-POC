"""Single-statement PostGIS queries for the information panel: one statement per panel type.

Same style as ``api.services.locate_sql``: CTEs plus ``jsonb_build_object`` / ``jsonb_agg`` so a
panel is one round trip, every parameter cast explicitly so NULLs type-check under asyncpg, every
table access index-backed. The statements read the SERVING tables only: planning values come from
``planning_parameter_values`` and the review queue ``planning_parameter_extractions`` is never
touched (rejected or under-review values are simply absent). ``data_version`` is the label of the
current ``publish_versions`` row (``is_current``), read fresh on every call.

Parameters: ``municipality_id`` and ``id`` for every statement; the cadastral and urban
statements also take ``min_overlap_m2`` and ``min_overlap_fraction`` (the locate thresholds that
decide when a planned urban parcel and a cadastral parcel are linked).

Resolution rules shared with locate: the governing document of a cadastral parcel is the
*adopted* document whose coverage contains ``ST_PointOnSurface(geom)``, most specific (smallest
coverage) first; a cadastral parcel and a planned urban parcel are linked when their overlap is at
least ``min_overlap_m2`` and ``min_overlap_fraction`` of the cadastral area; only urban parcels of
adopted documents are linked. The primary urban parcel of a cadastral panel is the link with the
largest overlap, then the smallest planned area, then the lowest id (contract 5.3); the primary
cadastral parcel of an urban panel is the link with the largest overlap.
"""

from __future__ import annotations


def _document_ref(alias: str) -> str:
    """``DocumentRef`` minus the status labels, which the service adds from ``panel_text``."""
    return f"""jsonb_build_object(
        'id', {alias}.id, 'name', {alias}.name, 'type', {alias}.type,
        'status', {alias}.status::text, 'source', {alias}.source,
        'registry_url', {alias}.source_url, 'amends_document_id', {alias}.amends_document_id)"""


_VERSION = """
version AS (
    SELECT label, published_at
    FROM publish_versions
    WHERE municipality_id = :municipality_id AND is_current
    LIMIT 1
)"""

_VERSION_COLUMNS = """
    (SELECT label FROM version) AS data_version,
    (SELECT (published_at AT TIME ZONE 'UTC')::date FROM version) AS data_version_date"""

# The field dictionary (13 rows) travels with every planning panel so a panel stays one round
# trip and nothing is cached between requests.
_FIELDS = """
fields AS (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
               'key', key, 'label_en', label_en, 'label_me', label_me,
               'abbreviation', abbreviation, 'unit', unit, 'value_type', value_type,
               'computed', computed, 'formula', formula)
               ORDER BY sort_order ASC, key ASC), '[]'::jsonb) AS fields
    FROM planning_fields
)"""

# In-progress amendments of the document in the ``doc`` CTE, linked by amends_document_id only.
_AMENDMENTS = f"""
amendments AS (
    SELECT COALESCE(jsonb_agg({_document_ref("a")} ORDER BY a.name ASC, a.id ASC),
                    '[]'::jsonb) AS docs
    FROM planning_documents a
    WHERE a.amends_document_id = (SELECT id FROM doc)
      AND a.status = 'in_progress'
)"""

# Current market inputs: the zone's row, else the municipality-wide default (zone_id null). Two
# index-backed branches (the partial unique indexes) instead of an OR.
_MARKET = """
market AS (
    SELECT id, zone_id, land_rate_eur_m2, build_rate_eur_m2, design_rate_eur_m2,
           sale_rate_eur_m2, range_low_factor, range_high_factor, source, source_date,
           created_at AS effective_from
    FROM (
        SELECT f.*, 0 AS rank
        FROM financial_assumptions f
        WHERE f.municipality_id = :municipality_id
          AND f.is_current
          AND f.zone_id = (SELECT zone_id FROM zone_pick)
        UNION ALL
        SELECT f.*, 1 AS rank
        FROM financial_assumptions f
        WHERE f.municipality_id = :municipality_id
          AND f.is_current
          AND f.zone_id IS NULL
    ) AS candidates
    ORDER BY rank ASC, id DESC
    LIMIT 1
)"""

_MARKET_COLUMN = """
    (SELECT jsonb_build_object(
        'id', id, 'zone_id', zone_id, 'land_rate_eur_m2', land_rate_eur_m2,
        'build_rate_eur_m2', build_rate_eur_m2, 'design_rate_eur_m2', design_rate_eur_m2,
        'sale_rate_eur_m2', sale_rate_eur_m2, 'range_low_factor', range_low_factor,
        'range_high_factor', range_high_factor, 'source', source, 'source_date', source_date,
        'effective_from', effective_from)
     FROM market) AS market"""


def _values(parcel_expr: str, document_expr: str) -> str:
    """Serving values for one planning panel: the parcel-level rows of ``parcel_expr`` plus the
    document-level rows (``urban_parcel_id IS NULL``) of ``document_expr``. Each branch hits one
    of the two partial unique indexes."""
    columns = """v.field_key, v.value_text, v.value_number, v.unit, v.source_page,
               v.source_bbox, v.source_note, v.document_id, d.name AS document_name,
               d.source_url AS registry_url"""
    return f"""
vals AS (
    SELECT {columns}, true AS parcel_level
    FROM planning_parameter_values v
    JOIN planning_documents d ON d.id = v.document_id
    WHERE v.urban_parcel_id = ({parcel_expr})
    UNION ALL
    SELECT {columns}, false AS parcel_level
    FROM planning_parameter_values v
    JOIN planning_documents d ON d.id = v.document_id
    WHERE v.urban_parcel_id IS NULL
      AND v.document_id = ({document_expr})
),
values_json AS (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
               'field_key', field_key, 'value_text', value_text, 'value_number', value_number,
               'unit', unit, 'source_page', source_page, 'source_bbox', source_bbox,
               'source_note', source_note, 'document_id', document_id,
               'document_name', document_name, 'registry_url', registry_url,
               'parcel_level', parcel_level)
               ORDER BY parcel_level DESC, field_key ASC), '[]'::jsonb) AS vals
    FROM vals
)"""


# --- zone -----------------------------------------------------------------------------------------

ZONE_SQL = f"""WITH{_VERSION},
zone AS (
    SELECT z.id, z.name, z.general_planning_summary
    FROM zones z
    WHERE z.id = CAST(:id AS bigint) AND z.municipality_id = :municipality_id
),
docs AS (
    SELECT COALESCE(jsonb_agg({_document_ref("d")}
               ORDER BY CASE d.status::text WHEN 'adopted' THEN 0 WHEN 'in_progress' THEN 1
                        ELSE 2 END ASC, d.name ASC, d.id ASC), '[]'::jsonb) AS docs,
           count(*) AS documents,
           count(*) FILTER (WHERE d.status = 'adopted') AS adopted,
           count(*) FILTER (WHERE d.status = 'in_progress') AS in_progress,
           count(*) FILTER (WHERE d.status = 'superseded') AS superseded
    FROM planning_documents d
    WHERE d.zone_id = (SELECT id FROM zone)
)
SELECT
    (SELECT jsonb_build_object('id', id, 'name', name,
                               'general_planning_summary', general_planning_summary)
     FROM zone) AS zone,
    (SELECT docs FROM docs) AS documents,
    (SELECT jsonb_build_object('documents', documents, 'adopted', adopted,
                               'in_progress', in_progress, 'superseded', superseded)
     FROM docs) AS counts,{_VERSION_COLUMNS}
"""

# --- document -------------------------------------------------------------------------------------

DOCUMENT_SQL = f"""WITH{_VERSION},
doc AS (
    SELECT d.id, d.name, d.type, d.status, d.source, d.source_url, d.zone_id,
           d.amends_document_id, d.dataset_version, d.coverage_geom
    FROM planning_documents d
    WHERE d.id = CAST(:id AS bigint) AND d.municipality_id = :municipality_id
),{_AMENDMENTS},
zone_ids AS (
    SELECT zone_id AS id FROM doc WHERE zone_id IS NOT NULL
    UNION
    SELECT z.id
    FROM zones z, doc
    WHERE z.municipality_id = :municipality_id
      AND ST_Intersects(z.geom, doc.coverage_geom)
),
zone_refs AS (
    SELECT COALESCE(jsonb_agg(jsonb_build_object('id', z.id, 'name', z.name)
                              ORDER BY z.name ASC, z.id ASC), '[]'::jsonb) AS zones
    FROM zones z
    WHERE z.id IN (SELECT id FROM zone_ids)
),
cadastral_count AS (
    -- bounding-box pre-filter on the GiST index, then the exact point-in-coverage test
    SELECT count(*) AS n
    FROM cadastral_parcels c, doc
    WHERE c.municipality_id = :municipality_id
      AND c.geom && doc.coverage_geom
      AND ST_Intersects(ST_PointOnSurface(c.geom), doc.coverage_geom)
),
urban_count AS (
    SELECT count(*) AS n FROM urban_parcels u WHERE u.document_id = (SELECT id FROM doc)
),
summary AS (
    SELECT z.general_planning_summary FROM zones z WHERE z.id = (SELECT zone_id FROM doc)
)
SELECT
    (SELECT {_document_ref("doc")} || jsonb_build_object(
                'ingestion_dataset_version', doc.dataset_version)
     FROM doc) AS document,
    (SELECT docs FROM amendments) AS amendments,
    (SELECT zones FROM zone_refs) AS zones,
    (SELECT n FROM cadastral_count) AS cadastral_parcels,
    (SELECT n FROM urban_count) AS urban_parcels,
    (SELECT general_planning_summary FROM summary) AS general_planning_summary,{_VERSION_COLUMNS}
"""

# --- cadastral ------------------------------------------------------------------------------------

_LINK_ORDER = "overlap_m2 DESC, area_m2 ASC, id ASC"

CADASTRAL_SQL = f"""WITH{_VERSION},{_FIELDS},
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
    -- governing document: adopted, contains the anchor, most specific coverage first
    SELECT d.id, d.name, d.type, d.status, d.source, d.source_url, d.zone_id,
           d.amends_document_id
    FROM planning_documents d, anchor a
    WHERE d.municipality_id = :municipality_id
      AND d.status = 'adopted'
      AND ST_Intersects(d.coverage_geom, a.pt)
    ORDER BY ST_Area(d.coverage_geom) ASC, d.id DESC
    LIMIT 1
),
ups AS (
    -- planned urban parcels of adopted documents overlapping the cadastral parcel
    SELECT u.id, u.urban_parcel_number, u.area_m2, u.document_id, u.block_id,
           {_document_ref("d")} AS document,
           CASE WHEN b.id IS NULL THEN NULL
                ELSE jsonb_build_object('id', b.id, 'block_ref', b.block_ref) END AS urban_block,
           ST_Area(ST_Intersection(u.geom, cad.geom)::geography) AS overlap_m2
    FROM urban_parcels u
    JOIN cad ON ST_Intersects(u.geom, cad.geom)
    JOIN planning_documents d ON d.id = u.document_id AND d.status = 'adopted'
    LEFT JOIN urban_blocks b ON b.id = u.block_id
    WHERE u.municipality_id = :municipality_id
      AND EXISTS (SELECT 1 FROM doc)
),
ups_kept AS (
    SELECT *
    FROM ups
    WHERE overlap_m2 >= CAST(:min_overlap_m2 AS double precision)
      AND overlap_m2 >= CAST(:min_overlap_fraction AS double precision)
                        * (SELECT area_m2 FROM cad)
),
primary_up AS (
    SELECT * FROM ups_kept ORDER BY {_LINK_ORDER} LIMIT 1
),
blk AS (
    -- the primary urban parcel's block (by primary key), else the block containing the anchor
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
    -- urban basis: the primary parcel's document; cadastral basis: the governing document
    SELECT COALESCE((SELECT document_id FROM primary_up), (SELECT id FROM doc)) AS document_id
),{_values("SELECT id FROM primary_up", "SELECT document_id FROM basis_doc")},{_MARKET}
SELECT
    (SELECT jsonb_build_object(
        'id', id, 'parcel_number', parcel_number, 'sub_number', sub_number,
        'ko_name', ko_name, 'street_address', street_address, 'area_m2', area_m2,
        'public_ownership', public_ownership,
        'restitution_or_legal_burden', restitution_or_legal_burden,
        'centroid', jsonb_build_object('lat', ST_Y(ST_Centroid(geom)),
                                       'lng', ST_X(ST_Centroid(geom))),
        'geometry', ST_AsGeoJSON(geom, 7)::jsonb)
     FROM cad) AS cadastral,
    (SELECT {_document_ref("doc")} FROM doc) AS governing_document,
    (SELECT jsonb_build_object('id', id, 'block_ref', block_ref) FROM blk) AS urban_block,
    (SELECT jsonb_build_object('id', id, 'name', name) FROM zone) AS zone,
    (SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'id', id, 'urban_parcel_number', urban_parcel_number, 'area_m2', area_m2,
        'overlap_m2', overlap_m2, 'document', document, 'urban_block', urban_block)
        ORDER BY {_LINK_ORDER}), '[]'::jsonb)
     FROM ups_kept) AS urban_parcels,
    (SELECT docs FROM amendments) AS amendments,
    (SELECT fields FROM fields) AS fields,
    (SELECT vals FROM values_json) AS "values",{_MARKET_COLUMN},{_VERSION_COLUMNS}
"""

# --- urban ----------------------------------------------------------------------------------------

URBAN_SQL = f"""WITH{_VERSION},{_FIELDS},
up AS (
    SELECT u.id, u.urban_parcel_number, u.area_m2, u.block_id, u.document_id, u.geom
    FROM urban_parcels u
    WHERE u.id = CAST(:id AS bigint) AND u.municipality_id = :municipality_id
),
doc AS (
    -- the parcel's own document, whatever its status (covered = adopted)
    SELECT d.id, d.name, d.type, d.status, d.source, d.source_url, d.zone_id,
           d.amends_document_id
    FROM planning_documents d
    WHERE d.id = (SELECT document_id FROM up)
),
anchor AS (
    SELECT ST_PointOnSurface(geom) AS pt FROM up
),
blk AS (
    -- the parcel's block (by primary key), else the block containing the anchor
    SELECT id, block_ref, zone_id
    FROM (
        SELECT b.id, b.block_ref, b.zone_id, 0 AS rank, 0.0 AS area
        FROM urban_blocks b
        WHERE b.id = (SELECT block_id FROM up)
        UNION ALL
        SELECT b.id, b.block_ref, b.zone_id, 1 AS rank, ST_Area(b.geom) AS area
        FROM urban_blocks b, anchor a
        WHERE b.municipality_id = :municipality_id
          AND ST_Intersects(b.geom, a.pt)
    ) AS candidates
    ORDER BY rank ASC, area ASC, id ASC
    LIMIT 1
),
zone_pick AS (
    -- document.zone_id, else block.zone_id, else the smallest zone containing the anchor
    SELECT COALESCE(
        (SELECT zone_id FROM doc),
        (SELECT zone_id FROM blk),
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
cads AS (
    -- cadastral parcels overlapping the planned parcel; the link thresholds are relative to the
    -- cadastral area, exactly as for the cadastral panel and locate
    SELECT c.id, c.parcel_number, c.sub_number, c.ko_name, c.street_address, c.area_m2,
           ST_Area(ST_Intersection(c.geom, up.geom)::geography) AS overlap_m2
    FROM cadastral_parcels c
    JOIN up ON ST_Intersects(c.geom, up.geom)
    WHERE c.municipality_id = :municipality_id
),
cads_kept AS (
    SELECT *
    FROM cads
    WHERE overlap_m2 >= CAST(:min_overlap_m2 AS double precision)
      AND overlap_m2 >= CAST(:min_overlap_fraction AS double precision) * area_m2
),{_values("SELECT id FROM up", "SELECT document_id FROM up")},{_MARKET}
SELECT
    (SELECT jsonb_build_object(
        'id', id, 'urban_parcel_number', urban_parcel_number, 'area_m2', area_m2,
        'block_id', block_id, 'document_id', document_id,
        'centroid', jsonb_build_object('lat', ST_Y(ST_Centroid(geom)),
                                       'lng', ST_X(ST_Centroid(geom))),
        'geometry', ST_AsGeoJSON(geom, 7)::jsonb)
     FROM up) AS urban_parcel,
    (SELECT {_document_ref("doc")} FROM doc) AS document,
    (SELECT jsonb_build_object('id', id, 'block_ref', block_ref) FROM blk) AS urban_block,
    (SELECT jsonb_build_object('id', id, 'name', name) FROM zone) AS zone,
    (SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'parcel_id', id, 'parcel_number', parcel_number, 'sub_number', sub_number,
        'ko_name', ko_name, 'street_address', street_address, 'area_m2', area_m2,
        'overlap_m2', overlap_m2)
        ORDER BY {_LINK_ORDER}), '[]'::jsonb)
     FROM cads_kept) AS cadastral_parcels,
    (SELECT docs FROM amendments) AS amendments,
    (SELECT fields FROM fields) AS fields,
    (SELECT vals FROM values_json) AS "values",{_MARKET_COLUMN},{_VERSION_COLUMNS}
"""

PANEL_SQL: dict[str, str] = {
    "zone": ZONE_SQL,
    "document": DOCUMENT_SQL,
    "cadastral": CADASTRAL_SQL,
    "urban": URBAN_SQL,
}
