"""The map layer catalogue: what the publish job exports into the PMTiles archive and how staged
geometry lands in the serving tables.

Every entry is data: a source-layer name, the geometry type, the zoom range and the SQL that
yields one GeoJSON feature per row for a given ``publish_version_id`` (``:v``) and
``municipality_id`` (``:m``). The public map toggles each layer independently (BRD §2.1); the
heatmap layers (``block_cells``, ``zone_cells``) carry the choropleth values.

Staged layers (``STAGED_LAYERS``): the ``properties`` keys the GIS ingestion job must write for
each ``layer_id`` in ``staging_geometry``; the publish job upserts entity layers by natural key so
UrbanView ids stay stable, and copies generic layers into ``layer_features`` per version.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- exported layers -----------------------------------------------------------------------------

_FEATURE = """jsonb_build_object('type', 'Feature', 'id', {id}, 'geometry',
    ST_AsGeoJSON({geom}, 7)::jsonb, 'properties', {properties})"""


def _feature(id_expr: str, geom_expr: str, properties: str) -> str:
    return _FEATURE.format(id=id_expr, geom=geom_expr, properties=properties)


# Effective numeric / text planning values per planned parcel for the current serving set:
# parcel-level row -> block-level -> zone-level -> document-level (the panel's precedence plus
# the block / zone scopes the publish job introduced).
EFFECTIVE_VALUE = """COALESCE(
    (SELECT v.{col} FROM planning_parameter_values v
     WHERE v.publish_version_id = :v AND v.urban_parcel_id = u.id AND v.field_key = '{key}'),
    (SELECT v.{col} FROM planning_parameter_values v
     WHERE v.publish_version_id = :v AND v.block_id = u.block_id AND v.field_key = '{key}'),
    (SELECT v.{col} FROM planning_parameter_values v
     WHERE v.publish_version_id = :v AND v.zone_id = b.zone_id AND v.field_key = '{key}'),
    (SELECT v.{col} FROM planning_parameter_values v
     WHERE v.publish_version_id = :v AND v.document_id = u.document_id
       AND v.urban_parcel_id IS NULL AND v.block_id IS NULL AND v.zone_id IS NULL
       AND v.field_key = '{key}'))"""


def effective(key: str, col: str = "value_number") -> str:
    return EFFECTIVE_VALUE.format(key=key, col=col)


# A zone is covered when at least one of its planning documents is adopted, live and current
# with a coverage geometry (the same documents location resolution uses). The map colours covered
# zones by type and draws the others muted ("no data yet"), never as if they had values.
ZONE_COVERED = """EXISTS (
    SELECT 1 FROM planning_documents d
    WHERE d.zone_id = z.id AND d.municipality_id = z.municipality_id AND d.status = 'adopted'
      AND d.coverage_live AND d.is_current_version AND d.coverage_geom IS NOT NULL)"""

# Planned parcels of adopted, live, current document versions with their effective parameters.
URBAN_PARCEL_INPUTS_SQL = f"""
    SELECT u.id, u.urban_parcel_number, u.area_m2, u.block_id, b.block_ref, b.zone_id,
           u.document_id, d.name AS document_name,
           {effective("max_far")} AS max_far,
           {effective("max_site_coverage_pct")} AS max_site_coverage_pct,
           {effective("max_height_m")} AS max_height_m,
           {effective("max_floors", "value_text")} AS max_floors,
           {effective("land_use", "value_text")} AS land_use
    FROM urban_parcels u
    JOIN planning_documents d ON d.id = u.document_id
    LEFT JOIN urban_blocks b ON b.id = u.block_id
    WHERE u.municipality_id = :m AND d.status = 'adopted' AND d.coverage_live
      AND d.is_current_version
    ORDER BY u.id
"""


@dataclass(frozen=True, slots=True)
class LayerSpec:
    id: str
    geometry_type: str  # polygon | line
    min_zoom: int
    max_zoom: int
    sql: str  # one column: the GeoJSON feature (jsonb); params :m, :v
    description: str


LAYERS: tuple[LayerSpec, ...] = (
    LayerSpec(
        "zones",
        "polygon",
        8,
        16,
        f"""
        SELECT {
            _feature(
                "z.id",
                "z.geom",
                "jsonb_build_object('id', z.id, 'name', z.name, 'zone_type', z.zone_type,"
                f" 'covered', {ZONE_COVERED})",
            )
        }
        FROM zones z WHERE z.municipality_id = :m ORDER BY z.id
        """,
        "Urban planning zones (core layer, default view) with type and coverage",
    ),
    LayerSpec(
        "zone_labels",
        "point",
        8,
        16,
        f"""
        SELECT {
            _feature(
                "z.id",
                "ST_PointOnSurface(z.geom)",
                "jsonb_build_object('id', z.id, 'name', z.name, 'zone_type', z.zone_type,"
                f" 'covered', {ZONE_COVERED})",
            )
        }
        FROM zones z WHERE z.municipality_id = :m ORDER BY z.id
        """,
        "One label point per zone (a polygon label would repeat in every tile)",
    ),
    LayerSpec(
        "document_coverage",
        "polygon",
        9,
        16,
        f"""
        SELECT {
            _feature(
                "d.id",
                "d.coverage_geom",
                "jsonb_build_object('id', d.id, 'name', d.name,"
                " 'type', d.type, 'status', d.status::text, 'version', d.version,"
                " 'zone_id', d.zone_id)",
            )
        }
        FROM planning_documents d
        WHERE d.municipality_id = :m AND d.coverage_geom IS NOT NULL AND d.coverage_live
          AND d.status = 'adopted' AND d.is_current_version
        ORDER BY d.id
        """,
        "Coverage areas of the adopted, live planning documents",
    ),
    LayerSpec(
        "urban_blocks",
        "polygon",
        11,
        16,
        f"""
        SELECT {
            _feature(
                "b.id",
                "b.geom",
                "jsonb_build_object('id', b.id, 'block_ref', b.block_ref, 'zone_id', b.zone_id)",
            )
        }
        FROM urban_blocks b WHERE b.municipality_id = :m ORDER BY b.id
        """,
        "Urban blocks",
    ),
    LayerSpec(
        "urban_parcels",
        "polygon",
        13,
        16,
        f"""
        WITH inputs AS ({URBAN_PARCEL_INPUTS_SQL})
        SELECT {
            _feature(
                "i.id",
                "u.geom",
                "jsonb_build_object('id', i.id,"
                " 'urban_parcel_number', i.urban_parcel_number, 'area_m2', i.area_m2,"
                " 'block_id', i.block_id, 'block_ref', i.block_ref, 'zone_id', i.zone_id,"
                " 'document_id', i.document_id, 'document_name', i.document_name,"
                " 'max_far', i.max_far, 'max_site_coverage_pct', i.max_site_coverage_pct,"
                " 'max_height_m', i.max_height_m, 'max_floors', i.max_floors,"
                " 'land_use', i.land_use,"
                " 'max_gfa_m2', CASE WHEN i.max_far IS NOT NULL"
                " THEN round(CAST(i.max_far * i.area_m2 AS numeric), 2) END)",
            )
        }
        FROM inputs i JOIN urban_parcels u ON u.id = i.id ORDER BY i.id
        """,
        "Planned urban parcels with their published parameters",
    ),
    LayerSpec(
        "cadastral_parcels",
        "polygon",
        13,
        16,
        f"""
        SELECT {
            _feature(
                "c.id",
                "c.geom",
                "jsonb_build_object('id', c.id,"
                " 'parcel_number', c.parcel_number, 'sub_number', c.sub_number,"
                " 'ko_name', c.ko_name, 'street_address', c.street_address,"
                " 'area_m2', c.area_m2, 'public_ownership', c.public_ownership,"
                " 'restitution_or_legal_burden', c.restitution_or_legal_burden,"
                " 'has_urban_parcel', l.urban_parcel_id IS NOT NULL,"
                " 'primary_urban_parcel_id', l.urban_parcel_id,"
                " 'overlap_fraction', l.overlap_fraction, 'area_delta_m2', l.area_delta_m2,"
                " 'zone_id', zc.id, 'zone_type', zc.zone_type)",
            )
        }
        FROM cadastral_parcels c
        LEFT JOIN parcel_links l ON l.publish_version_id = :v AND l.cadastral_parcel_id = c.id
             AND l.rank = 1
        LEFT JOIN LATERAL (
            SELECT z.id, z.zone_type FROM zones z
            WHERE z.municipality_id = c.municipality_id
              AND ST_Intersects(z.geom, ST_PointOnSurface(c.geom))
            ORDER BY ST_Area(z.geom), z.id LIMIT 1
        ) zc ON true
        WHERE c.municipality_id = :m ORDER BY c.id
        """,
        "Cadastral parcels with their primary planned-parcel link",
    ),
    LayerSpec(
        "public_ownership",
        "polygon",
        13,
        16,
        f"""
        SELECT {
            _feature(
                "c.id",
                "c.geom",
                "jsonb_build_object('id', c.id,"
                " 'parcel_number', c.parcel_number, 'ko_name', c.ko_name)",
            )
        }
        FROM cadastral_parcels c WHERE c.municipality_id = :m AND c.public_ownership ORDER BY c.id
        """,
        "Cadastral parcels in public ownership",
    ),
    LayerSpec(
        "legal_burdens",
        "polygon",
        13,
        16,
        f"""
        SELECT {
            _feature(
                "c.id",
                "c.geom",
                "jsonb_build_object('id', c.id,"
                " 'parcel_number', c.parcel_number, 'ko_name', c.ko_name)",
            )
        }
        FROM cadastral_parcels c
        WHERE c.municipality_id = :m AND c.restitution_or_legal_burden ORDER BY c.id
        """,
        "Cadastral parcels under restitution or legal burden",
    ),
    LayerSpec(
        "land_use",
        "polygon",
        10,
        16,
        f"""
        SELECT {
            _feature(
                "f.id",
                "f.geom",
                "f.properties || jsonb_build_object('id', f.id, 'feature_key', f.feature_key)",
            )
        }
        FROM layer_features f
        WHERE f.municipality_id = :m AND f.publish_version_id = :v AND f.layer_id = 'land_use'
        ORDER BY f.id
        """,
        "Planned land use",
    ),
    LayerSpec(
        "traffic_network",
        "line",
        10,
        16,
        f"""
        SELECT {
            _feature(
                "f.id",
                "f.geom",
                "f.properties || jsonb_build_object('id', f.id, 'feature_key', f.feature_key)",
            )
        }
        FROM layer_features f
        WHERE f.municipality_id = :m AND f.publish_version_id = :v
          AND f.layer_id = 'traffic_network'
        ORDER BY f.id
        """,
        "Planned traffic network",
    ),
    LayerSpec(
        "block_cells",
        "polygon",
        10,
        16,
        f"""
        SELECT {
            _feature(
                "h.cell_id",
                "h.geom",
                "jsonb_build_object('id', h.cell_id,"
                " 'ref', h.cell_ref, 'parcel_count', h.parcel_count,"
                " 'stated_count', h.stated_count,"
                " 'max_site_coverage_pct', h.max_site_coverage_pct,"
                " 'max_height_m', h.max_height_m, 'max_far', h.max_far,"
                " 'max_gfa_m2', h.max_gfa_m2, 'saleable_area_m2', h.saleable_area_m2,"
                " 'sale_rate_eur_m2', h.sale_rate_eur_m2,"
                " 'sale_rate_low_eur_m2', h.sale_rate_low_eur_m2,"
                " 'sale_rate_high_eur_m2', h.sale_rate_high_eur_m2,"
                " 'market_value_eur', h.market_value_eur, 'price_band', h.price_band)",
            )
        }
        FROM heatmap_cells h
        WHERE h.municipality_id = :m AND h.publish_version_id = :v AND h.cell_type = 'block'
        ORDER BY h.cell_id
        """,
        "Heatmap cells per urban block (coverage, height, FAR, GFA, saleable area, price band)",
    ),
    LayerSpec(
        "zone_cells",
        "polygon",
        8,
        16,
        f"""
        SELECT {
            _feature(
                "h.cell_id",
                "h.geom",
                "jsonb_build_object('id', h.cell_id,"
                " 'ref', h.cell_ref, 'parcel_count', h.parcel_count,"
                " 'stated_count', h.stated_count,"
                " 'max_site_coverage_pct', h.max_site_coverage_pct,"
                " 'max_height_m', h.max_height_m, 'max_far', h.max_far,"
                " 'max_gfa_m2', h.max_gfa_m2, 'saleable_area_m2', h.saleable_area_m2,"
                " 'sale_rate_eur_m2', h.sale_rate_eur_m2,"
                " 'sale_rate_low_eur_m2', h.sale_rate_low_eur_m2,"
                " 'sale_rate_high_eur_m2', h.sale_rate_high_eur_m2,"
                " 'market_value_eur', h.market_value_eur, 'price_band', h.price_band)",
            )
        }
        FROM heatmap_cells h
        WHERE h.municipality_id = :m AND h.publish_version_id = :v AND h.cell_type = 'zone'
        ORDER BY h.cell_id
        """,
        "Heatmap cells per zone",
    ),
)

LAYER_IDS: tuple[str, ...] = tuple(layer.id for layer in LAYERS)


# --- staged layers (the GIS ingestion contract) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class StagedLayer:
    id: str
    kind: str  # entity | generic
    natural_key: str  # human description of feature_key
    properties: tuple[str, ...]  # keys the ingestion job writes


STAGED_LAYERS: dict[str, StagedLayer] = {
    layer.id: layer
    for layer in (
        StagedLayer(
            "cadastral_parcels",
            "entity",
            "ko_name|parcel_number|sub_number ('' when none)",
            (
                "ko_name",
                "parcel_number",
                "sub_number",
                "street_address",
                "area_m2",
                "public_ownership",
                "restitution_or_legal_burden",
            ),
        ),
        StagedLayer(
            "urban_parcels",
            "entity",
            "document_id|urban_parcel_number",
            ("document_id", "urban_parcel_number", "block_ref", "area_m2"),
        ),
        StagedLayer("urban_blocks", "entity", "block_ref", ("block_ref", "zone_name")),
        StagedLayer("zones", "entity", "name", ("name", "general_planning_summary", "zone_type")),
        StagedLayer("document_coverage", "entity", "document_id", ("document_id",)),
        StagedLayer("land_use", "generic", "any stable id", ("code", "name", "category")),
        StagedLayer("traffic_network", "generic", "any stable id", ("road_class", "name")),
    )
}
GENERIC_LAYER_IDS: tuple[str, ...] = tuple(
    layer.id for layer in STAGED_LAYERS.values() if layer.kind == "generic"
)
