"""Publish pipeline pieces that need no database: the layer catalogue, the cell aggregation and
price bands, version labels, the market-input mapping, and the tippecanoe / tile-join commands
(the binaries themselves are not installed on the API side)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobs.publish_layers import GENERIC_LAYER_IDS, LAYER_IDS, LAYERS, STAGED_LAYERS
from jobs.publish_pipeline import (
    STEPS,
    ParcelFigures,
    aggregate_cell,
    market_inputs_from_row,
    next_label,
    price_bands,
)
from jobs.tiles import (
    LayerFile,
    TileBuildError,
    TippecanoeTileBuilder,
    tile_join_command,
    tippecanoe_command,
)

BRD_LAYERS = {
    "zones",
    "cadastral_parcels",
    "urban_parcels",
    "land_use",
    "public_ownership",
    "legal_burdens",
    "traffic_network",
    "block_cells",
    "zone_cells",
}


def test_map_layers_carry_what_the_public_map_styles_by():
    by_id = {layer.id: layer for layer in LAYERS}
    for layer_id in ("zones", "zone_labels"):
        sql = by_id[layer_id].sql
        assert "'zone_type', z.zone_type" in sql and "'covered'" in sql, layer_id
        assert "coverage_live" in sql and "is_current_version" in sql, layer_id
    assert by_id["zone_labels"].geometry_type == "point"
    assert "ST_PointOnSurface(z.geom)" in by_id["zone_labels"].sql
    assert "'zone_id', zc.id" in by_id["cadastral_parcels"].sql
    assert "zone_type" in STAGED_LAYERS["zones"].properties


def test_layer_catalogue_is_consistent():
    assert len(set(LAYER_IDS)) == len(LAYER_IDS)
    assert BRD_LAYERS <= set(LAYER_IDS)
    for layer in LAYERS:
        assert layer.geometry_type in {"polygon", "line", "point"}
        assert 0 <= layer.min_zoom <= layer.max_zoom <= 22
        assert ":m" in layer.sql and "jsonb_build_object('type', 'Feature'" in layer.sql
    versioned = {"urban_parcels", "cadastral_parcels", "land_use", "traffic_network"}
    versioned |= {"block_cells", "zone_cells"}
    for layer in LAYERS:
        if layer.id in versioned:
            assert ":v" in layer.sql, layer.id
    assert set(GENERIC_LAYER_IDS) == {"land_use", "traffic_network"}
    assert {s.id for s in STAGED_LAYERS.values() if s.kind == "entity"} == {
        "cadastral_parcels",
        "urban_parcels",
        "urban_blocks",
        "zones",
        "document_coverage",
    }
    assert STEPS[0] == "preflight" and STEPS[-2:] == ("flip", "prune")


def test_cells_aggregate_area_weighted_means_and_sums():
    parcels = [
        ParcelFigures(1000, 3.0, 50, 27.5, 3000, 2100, 5_145_000),
        ParcelFigures(3000, 1.0, 30, 12, 3000, 2100, 3_465_000),
        ParcelFigures(500, None, None, None, None, None, None),  # nothing stated
    ]
    cell = aggregate_cell(parcels)
    assert (cell.parcel_count, cell.stated_count) == (3, 2)
    assert cell.max_far == pytest.approx(1.5)  # (3.0*1000 + 1.0*3000) / 4000
    assert cell.max_site_coverage_pct == pytest.approx(35.0)
    assert cell.max_height_m == 27.5
    assert cell.max_gfa_m2 == 6000 and cell.saleable_area_m2 == 4200
    assert cell.market_value_eur == 8_610_000
    empty = aggregate_cell([])
    assert empty.parcel_count == 0 and empty.max_far is None and empty.max_gfa_m2 is None


def test_price_bands_are_terciles_of_the_zone_rates():
    assert price_bands({1: 2450.0, 2: 1650.0}) == {1: 2, 2: 1}
    assert price_bands({1: 1000.0, 2: 2000.0, 3: 3000.0}) == {1: 1, 2: 2, 3: 3}
    assert price_bands({1: 1000.0, 2: 1000.0, 3: 3000.0}) == {1: 1, 2: 1, 3: 2}
    assert price_bands({}) == {}


def test_next_label_counts_within_the_day():
    today = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
    assert next_label([], today) == "2026-09-25.1"
    assert next_label(["2026-09-25.1", "2026-09-25.2", "other"], today) == "2026-09-25.3"
    assert next_label(["2026-09-25.2"], today) == "2026-09-25.1"


def test_market_inputs_from_row_keeps_absolute_bounds():
    row = {
        "land_rate_eur_m2": 1350,
        "build_rate_eur_m2": 860,
        "design_rate_eur_m2": 90,
        "sale_rate_eur_m2": 2450,
        "range_low_factor": 0.86,
        "range_high_factor": 1.15,
        "land_rate_low_eur_m2": None,
        "land_rate_high_eur_m2": None,
        "sale_rate_low_eur_m2": 2200,
        "sale_rate_high_eur_m2": 2700,
    }
    market = market_inputs_from_row(row)
    assert market.sale_bounds == (2200.0, 2700.0) and market.land_bounds is None
    assert market.build_bounds is None and market.range_high_factor == 1.15


def test_tippecanoe_and_tile_join_commands():
    polygon = LayerFile("cadastral_parcels", Path("c.geojson"), 7, "polygon", 13, 16)
    line = LayerFile("traffic_network", Path("t.geojson"), 2, "line", 10, 16)
    command = tippecanoe_command("tippecanoe", polygon, Path("out/c.pmtiles"))
    assert command[:3] == ["tippecanoe", "--output", str(Path("out/c.pmtiles"))]
    assert "--layer" in command and command[command.index("--layer") + 1] == "cadastral_parcels"
    assert "--minimum-zoom=13" in command and "--maximum-zoom=16" in command
    assert "--no-feature-limit" in command and "--detect-shared-borders" in command
    assert command[-1] == "c.geojson"
    assert "--detect-shared-borders" not in tippecanoe_command("tippecanoe", line, Path("t"))
    assert "--drop-rate=1" not in command
    point = LayerFile("zone_labels", Path("z.geojson"), 2, "point", 8, 16)
    assert "--drop-rate=1" in tippecanoe_command("tippecanoe", point, Path("z"))
    join = tile_join_command(
        "tile-join", [Path("a.pmtiles"), Path("b.pmtiles")], Path("all.pmtiles")
    )
    assert join[:3] == ["tile-join", "--output", "all.pmtiles"]
    assert join[-2:] == ["a.pmtiles", "b.pmtiles"] and "--force" in join


def test_tile_builder_fails_clearly_without_the_binaries(tmp_path):
    layer = tmp_path / "zones.geojson"
    layer.write_text('{"type":"Feature","id":1,"geometry":null,"properties":{}}\n')
    builder = TippecanoeTileBuilder("definitely-not-installed-tippecanoe", "no-tile-join")
    with pytest.raises(TileBuildError, match="not installed"):
        builder.build([LayerFile("zones", layer, 1, "polygon", 8, 16)], tmp_path / "a.pmtiles")
    with pytest.raises(TileBuildError, match="no layers"):
        builder.build([], tmp_path / "b.pmtiles")
