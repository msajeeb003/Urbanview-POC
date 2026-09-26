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
        'registry_url', {alias}.source_url, 'amends_document_id', {alias}.amends_document_id,
        'coverage_live', {alias}.coverage_live, 'adopted_on', {alias}.adopted_on)"""


# A document is covered (it resolves locations) when it is adopted, live, the current version and
# has a coverage geometry: the rule of location resolution and of the map's tiles.
def _covered(alias: str) -> str:
    return (
        f"({alias}.status = 'adopted' AND {alias}.coverage_live AND {alias}.is_current_version"
        f" AND {alias}.coverage_geom IS NOT NULL)"
    )


# Cadastral parcels whose point on surface lies in a coverage: GiST bounding-box pre-filter first.
def _parcels_in(coverage: str) -> str:
    return f"""(SELECT count(*) FROM cadastral_parcels c
        WHERE c.municipality_id = :municipality_id AND c.geom && {coverage}
          AND ST_Intersects(ST_PointOnSurface(c.geom), {coverage}))"""


_VERSION = """
version AS (
    SELECT id, label, published_at
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

# Current market inputs: the zone's own current row, nothing else. A zone without one has no
# market figures (the panel says "no market data for this zone"); the municipality-wide row
# (zone_id null) only holds the range factors single-figure market imports are widened with and
# never stands in for a zone (core.market, docs/specs/market-data.md). Index-backed by the partial
# unique index on the current zone rows. effective_from: the version's stated effective date,
# else its creation time.
_MARKET = """
market AS (
    SELECT f.id, f.zone_id, f.version, f.land_rate_eur_m2, f.build_rate_eur_m2,
           f.design_rate_eur_m2, f.sale_rate_eur_m2, f.range_low_factor, f.range_high_factor,
           f.source, f.source_date, f.rate_sources,
           f.land_rate_low_eur_m2, f.land_rate_high_eur_m2, f.build_rate_low_eur_m2,
           f.build_rate_high_eur_m2, f.design_rate_low_eur_m2, f.design_rate_high_eur_m2,
           f.sale_rate_low_eur_m2, f.sale_rate_high_eur_m2,
           COALESCE(f.effective_from::timestamp AT TIME ZONE 'UTC', f.created_at)
               AS effective_from
    FROM financial_assumptions f
    WHERE f.municipality_id = :municipality_id
      AND f.is_current
      AND f.zone_id = (SELECT zone_id FROM zone_pick)
    ORDER BY f.id DESC
    LIMIT 1
)"""

_MARKET_COLUMN = """
    (SELECT jsonb_build_object(
        'id', id, 'zone_id', zone_id, 'land_rate_eur_m2', land_rate_eur_m2,
        'build_rate_eur_m2', build_rate_eur_m2, 'design_rate_eur_m2', design_rate_eur_m2,
        'sale_rate_eur_m2', sale_rate_eur_m2, 'range_low_factor', range_low_factor,
        'range_high_factor', range_high_factor, 'source', source, 'source_date', source_date,
        'effective_from', effective_from, 'version', version, 'rate_sources', rate_sources,
        'bounds', jsonb_build_object(
            'land', jsonb_build_array(land_rate_low_eur_m2, land_rate_high_eur_m2),
            'build', jsonb_build_array(build_rate_low_eur_m2, build_rate_high_eur_m2),
            'design', jsonb_build_array(design_rate_low_eur_m2, design_rate_high_eur_m2),
            'sale', jsonb_build_array(sale_rate_low_eur_m2, sale_rate_high_eur_m2)))
     FROM market) AS market"""


def _values(parcel_expr: str, document_expr: str) -> str:
    """Serving values of the current publish version for one planning panel: the parcel-level
    rows of ``parcel_expr`` plus the document-level rows (no parcel / block / zone scope) of
    ``document_expr``. Each branch hits one of the partial unique indexes (version first).
    With no current version nothing is served (the panel says ``unpublished``)."""
    columns = """v.id AS value_id, v.field_key, v.value_text, v.value_number, v.unit, v.source_page,
               v.source_bbox, v.source_note, v.document_id, d.name AS document_name,
               d.source_url AS registry_url"""
    return f"""
vals AS (
    SELECT {columns}, true AS parcel_level
    FROM planning_parameter_values v
    JOIN planning_documents d ON d.id = v.document_id
    WHERE v.publish_version_id = (SELECT id FROM version)
      AND v.urban_parcel_id = ({parcel_expr})
    UNION ALL
    SELECT {columns}, false AS parcel_level
    FROM planning_parameter_values v
    JOIN planning_documents d ON d.id = v.document_id
    WHERE v.publish_version_id = (SELECT id FROM version)
      AND v.urban_parcel_id IS NULL AND v.block_id IS NULL AND v.zone_id IS NULL
      AND v.document_id = ({document_expr})
),
values_json AS (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
               'value_id', value_id,
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
    SELECT z.id, z.name, z.zone_type, z.general_planning_summary
    FROM zones z
    WHERE z.id = CAST(:id AS bigint) AND z.municipality_id = :municipality_id
),
-- the zone's current document versions; a covered one says how many cadastral parcels it covers
docs AS (
    SELECT COALESCE(jsonb_agg({_document_ref("d")} || jsonb_build_object(
                   'covered', {_covered("d")},
                   'file_available', d.file_key IS NOT NULL,
                   'parcel_count', CASE WHEN {_covered("d")}
                                        THEN {_parcels_in("d.coverage_geom")} END)
               ORDER BY CASE d.status::text WHEN 'adopted' THEN 0 WHEN 'in_progress' THEN 1
                        ELSE 2 END ASC, d.name ASC, d.id ASC), '[]'::jsonb) AS docs,
           count(*) AS documents,
           count(*) FILTER (WHERE d.status = 'adopted') AS adopted,
           count(*) FILTER (WHERE d.status = 'in_progress') AS in_progress,
           count(*) FILTER (WHERE d.status = 'superseded') AS superseded,
           count(*) FILTER (WHERE {_covered("d")}) AS covered
    FROM planning_documents d
    WHERE d.zone_id = CAST(:id AS bigint) AND d.municipality_id = :municipality_id
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
    (SELECT jsonb_build_object('id', id, 'name', name, 'zone_type', zone_type,
                               'general_planning_summary', general_planning_summary)
     FROM zone) AS zone,
    (SELECT docs FROM docs) AS documents,
    (SELECT jsonb_build_object('documents', documents, 'adopted', adopted,
                               'in_progress', in_progress, 'superseded', superseded,
                               'covered', covered)
     FROM docs) AS counts,
    (SELECT jsonb_build_object(
        'id', id, 'version', version, 'land_use', land_use, 'max_far', max_far,
        'max_site_coverage_pct', max_site_coverage_pct, 'max_height_m', max_height_m,
        'max_floors', max_floors, 'notes', notes, 'verified_on', verified_on,
        'verified_by', verified_by, 'source_document_id', source_document_id,
        'document_name', document_name, 'registry_url', registry_url,
        'source_page', source_page, 'source_note', source_note)
     FROM typical) AS typical_parameters,{_VERSION_COLUMNS}
"""

# --- document -------------------------------------------------------------------------------------

DOCUMENT_SQL = f"""WITH{_VERSION},
doc AS (
    SELECT d.id, d.name, d.type, d.status, d.source, d.source_url, d.zone_id,
           d.amends_document_id, d.coverage_live, d.dataset_version, d.coverage_geom,
           d.adopted_on, d.file_key
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
-- each zone spanned, with its type and the typical values of its current parameter set
zone_refs AS (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
               'id', z.id, 'name', z.name, 'zone_type', z.zone_type,
               'typical', CASE WHEN p.id IS NULL THEN NULL ELSE jsonb_build_object(
                   'land_use', p.land_use, 'max_far', p.max_far,
                   'max_site_coverage_pct', p.max_site_coverage_pct,
                   'max_height_m', p.max_height_m, 'max_floors', p.max_floors) END)
               ORDER BY z.name ASC, z.id ASC), '[]'::jsonb) AS zones
    FROM zones z
    LEFT JOIN zone_parameter_sets p ON p.zone_id = z.id AND p.is_current
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
                'ingestion_dataset_version', doc.dataset_version,
                'file_available', doc.file_key IS NOT NULL)
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
           d.amends_document_id, d.coverage_live, d.adopted_on
    FROM planning_documents d, anchor a
    WHERE d.municipality_id = :municipality_id
      AND d.status = 'adopted'
      AND d.coverage_live
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
    JOIN planning_documents d ON d.id = u.document_id AND d.status = 'adopted' AND d.coverage_live
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
    -- the parcel's own document, whatever its status (covered = adopted AND coverage live)
    SELECT d.id, d.name, d.type, d.status, d.source, d.source_url, d.zone_id,
           d.amends_document_id, d.coverage_live, d.adopted_on
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
