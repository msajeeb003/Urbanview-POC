"""Publish pipeline pieces that need no database: the layer catalogue, the cell aggregation and
price bands, version labels, the market-input mapping, and the tippecanoe / tile-join commands
(the binaries themselves are not installed on the API side)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobs.publish_layers import GENERIC_LAYER_IDS, LAYER_IDS, LAYERS, STAGED_LAYERS
from jobs.publish_pipeline import STEPS, next_label
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
    "heat_coverage",
    "heat_far",
    "heat_height",
    "heat_gfa",
    "heat_sale_price",
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
    # a planned parcel names the urban panel's zone: the plan's, else its block's
    assert "COALESCE(d.zone_id, b.zone_id) AS zone_id" in by_id["urban_parcels"].sql
    assert "zone_type" in STAGED_LAYERS["zones"].properties


def test_ownership_layers_depend_on_loaded_flags():
    by_id = {layer.id: layer for layer in LAYERS}
    assert by_id["public_ownership"].requires_flag == "public_ownership"
    assert by_id["legal_burdens"].requires_flag == "restitution_or_legal_burden"
    assert {layer.id for layer in LAYERS if layer.requires_flag} == {
        "public_ownership",
        "legal_burdens",
    }
    for layer_id in ("cadastral_parcels", "public_ownership", "legal_burdens"):
        assert "retired_at IS NULL" in by_id[layer_id].sql, layer_id


def test_heatmaps_are_one_source_layer_each_with_value_and_band():
    by_id = {layer.id: layer for layer in LAYERS}
    for name in ("coverage", "far", "height", "gfa"):
        sql = by_id[f"heat_{name}"].sql
        assert f"c.layer = '{name}'" in sql and "FROM urban_blocks b" in sql
        assert "LEFT JOIN choropleth_cells" in sql  # every block: not covered without a cell
        assert "'value', c.value" in sql and "'band', c.value_band" in sql
    price = by_id["heat_sale_price"].sql
    assert "FROM zones z" in price and "c.layer = 'sale_price'" in price
    assert "'band_low'" in price and "'band_high'" in price and "choropleth_classes k" in price
    assert "block_cells" not in by_id and "zone_cells" not in by_id


def test_layer_catalogue_is_consistent():
    assert len(set(LAYER_IDS)) == len(LAYER_IDS)
    assert BRD_LAYERS <= set(LAYER_IDS)
    for layer in LAYERS:
        assert layer.geometry_type in {"polygon", "line", "point"}
        assert 0 <= layer.min_zoom <= layer.max_zoom <= 22
        assert ":m" in layer.sql and "jsonb_build_object('type', 'Feature'" in layer.sql
    # planned traffic is an MVP layer: never in the POC's catalogue
    assert "traffic_network" not in LAYER_IDS
    versioned = {"urban_parcels", "cadastral_parcels", "land_use"}
    versioned |= {"heat_coverage", "heat_far", "heat_height", "heat_gfa", "heat_sale_price"}
    for layer in LAYERS:
        if layer.id in versioned:
            assert ":v" in layer.sql, layer.id
    assert set(GENERIC_LAYER_IDS) == {"land_use"}
    assert {s.id for s in STAGED_LAYERS.values() if s.kind == "entity"} == {
        "cadastral_parcels",
        "cadastral_municipalities",
        "urban_parcels",
        "urban_blocks",
        "zones",
        "document_coverage",
    }
    assert STEPS[0] == "preflight" and STEPS[-2:] == ("flip", "prune")


def test_next_label_counts_within_the_day():
    today = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
    assert next_label([], today) == "2026-09-25.1"
    assert next_label(["2026-09-25.1", "2026-09-25.2", "other"], today) == "2026-09-25.3"
    assert next_label(["2026-09-25.2"], today) == "2026-09-25.1"


def test_tippecanoe_and_tile_join_commands():
    polygon = LayerFile("cadastral_parcels", Path("c.geojson"), 7, "polygon", 13, 16)
    point = LayerFile("zone_labels", Path("z.geojson"), 2, "point", 8, 16)
    command = tippecanoe_command("tippecanoe", polygon, Path("out/c.pmtiles"))
    assert command[:3] == ["tippecanoe", "--output", str(Path("out/c.pmtiles"))]
    assert "--layer" in command and command[command.index("--layer") + 1] == "cadastral_parcels"
    assert "--minimum-zoom=13" in command and "--maximum-zoom=16" in command
    assert "--no-feature-limit" in command and "--detect-shared-borders" in command
    assert command[-1] == "c.geojson"
    assert "--detect-shared-borders" not in tippecanoe_command("tippecanoe", point, Path("z"))
    assert "--drop-rate=1" not in command
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
