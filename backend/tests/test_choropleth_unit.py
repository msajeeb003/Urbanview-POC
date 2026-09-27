"""The heatmap rules of ``core.choropleth`` (pure parts): per-block aggregation of the planning
values, the stored classes and each cell's band, the sale price's range."""

from __future__ import annotations

import pytest

from core.choropleth import (
    LAYERS,
    UNITS,
    CellValue,
    ParcelInputs,
    aggregate_block,
    band,
    layer_classes,
    percentile,
    quantile_breaks,
    sale_range,
    source_layer,
)
from core.extraction.normalise import FloorTokens

TOKENS = FloorTokens.of(["Po", "S", "G"], ["P", "VP"], ["M", "Pk", "Ps"])


def test_block_values_follow_the_rule_of_each_field():
    parcels = [
        ParcelInputs(area_m2=1000, max_far=3.0, max_site_coverage_pct=50, max_floors="P+8"),
        ParcelInputs(area_m2=3000, max_far=1.0, max_site_coverage_pct=30, max_floors="P+5+Pk"),
        ParcelInputs(area_m2=500),  # states nothing: no weight anywhere
    ]
    cells = aggregate_block(parcels, TOKENS)
    # area-weighted means: (3.0 x 1000 + 1.0 x 3000) / 4000, (50 x 1000 + 30 x 3000) / 4000
    assert cells["far"].value == pytest.approx(1.5) and cells["far"].parcel_count == 2
    assert cells["coverage"].value == pytest.approx(35.0)
    # the engine's formula summed: 3.0 x 1000 + 1.0 x 3000
    assert cells["gfa"] == CellValue(6000.0, 2)
    # the tallest parcel: P+8 = 9 floors above ground (P+5+Pk = 7); its notation is the label
    assert cells["height"] == CellValue(9.0, 2, label="P+8")


def test_a_block_level_figure_and_missing_values():
    # the plan's block figure reaches every parcel (effective values): the mean is the figure
    same = [ParcelInputs(area_m2=a, max_far=2.4) for a in (300, 900, 1200)]
    assert aggregate_block(same, TOKENS)["far"].value == pytest.approx(2.4)
    # nothing stated: no cell for any layer (absence of data, not zero)
    assert aggregate_block([ParcelInputs(area_m2=800)], TOKENS) == {}
    assert aggregate_block([], TOKENS) == {}
    # an unreadable notation gives no floors; without the profile's tokens no height at all
    odd = [ParcelInputs(area_m2=100, max_floors="visoko")]
    assert "height" not in aggregate_block(odd, TOKENS)
    assert "height" not in aggregate_block([ParcelInputs(100, max_floors="P+4")], None)
    # parcels without an area count equally in the means
    flat = [ParcelInputs(0, max_far=1.0), ParcelInputs(0, max_far=3.0)]
    assert aggregate_block(flat, TOKENS)["far"].value == pytest.approx(2.0)


def test_quantile_breaks_are_rounded_unique_and_above_the_minimum():
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert percentile([10.0, 20.0], 0.25) == 12.5
    assert quantile_breaks([0.5, 1.0, 1.5, 2.0, 2.5, 3.0], 2) == [1.0, 1.5, 2.0, 2.5]
    assert quantile_breaks([2.4, 2.4, 2.4], 2) == []  # one value: one class
    assert quantile_breaks([], 2) == []
    assert quantile_breaks([0.0, 0.0, 5.0], 0) == [5.0]  # zeros never make a class start


def test_bands_are_legend_rows():
    breaks = [1300.0, 1700.0, 2100.0]
    # the sale price: 0 = not saleable, then the fixed bands of the profile
    assert [band(v, breaks, zero_class=True) for v in (0, 900, 1300, 1650, 1700, 2450)] == [
        0,
        1,
        2,
        2,
        3,
        4,
    ]
    # a planning layer: the number of breaks <= value
    assert [band(v, [1.0, 2.0], zero_class=False) for v in (0.5, 1.0, 1.9, 2.0, 9.0)] == [
        0,
        1,
        1,
        2,
        2,
    ]


def test_classes_per_layer():
    far = layer_classes("far", [0.8, 1.2, 1.6, 2.4, 3.0], 7)
    assert (far.method, far.count, far.null_count, far.zero_class) == ("quantile", 5, 2, False)
    assert (far.min, far.max, far.mean) == (0.8, 3.0, 1.8) and far.breaks == [
        1.12,
        1.44,
        1.92,
        2.52,
    ]
    price = layer_classes("sale_price", [1500.0, 2450.0], 3, price_breaks=[2100, 1300, 1700])
    assert (price.method, price.breaks, price.zero_class) == ("fixed", [1300, 1700, 2100], True)
    assert (price.count, price.null_count, price.unit) == (2, 1, "€/m²")
    assert price.as_json()["source_layer"] == "heat_sale_price"
    empty = layer_classes("gfa", [], 4)
    assert (empty.count, empty.null_count, empty.min, empty.breaks) == (0, 4, None, [])
    assert set(LAYERS) == set(UNITS) and source_layer("far") == "heat_far"


def test_sale_range_uses_bounds_or_factors():
    factors = {
        "sale_rate_eur_m2": 2450,
        "sale_rate_low_eur_m2": None,
        "sale_rate_high_eur_m2": None,
        "range_low_factor": 0.86,
        "range_high_factor": 1.15,
    }
    assert sale_range(factors) == (2107.0, 2817.5)
    bounded = {**factors, "sale_rate_low_eur_m2": 2200, "sale_rate_high_eur_m2": 2700}
    assert sale_range(bounded) == (2200.0, 2700.0)
