"""Choropleth classes served with the tile pointer (`api.services.cell_classes`)."""

from api.schemas.publish import CellClasses
from api.services.cell_classes import CLASS_STATS_SQL, METRICS, build_classes
from jobs.publish_pipeline import MarketInputs, sale_rate_range


def row(cell_type, metric, cells, stated, lo, hi, quantiles):
    return {
        "cell_type": cell_type,
        "metric": metric,
        "cells": cells,
        "stated": stated,
        "min": lo,
        "max": hi,
        "quantiles": quantiles,
    }


def test_quantile_breaks_are_rounded_unique_and_above_the_minimum():
    classes = build_classes(
        [
            row("block", "max_far", 12, 10, 0.8, 3.2, [0.8, 1.234, 1.236, 2.5]),
            row("block", "max_gfa_m2", 12, 12, 100.0, 9000.0, [800.4, 1500.6, 3000.2, 6000.9]),
        ]
    )
    far = classes["block_cells"]["max_far"]
    assert far["method"] == "quantile"
    assert far["breaks"] == [1.23, 1.24, 2.5]  # 0.8 is the minimum: the first class starts there
    assert (far["count"], far["null_count"], far["min"], far["max"]) == (10, 2, 0.8, 3.2)
    assert far["zero_class"] is False and far["unit"] is None
    assert classes["block_cells"]["max_gfa_m2"]["breaks"] == [800.0, 1501.0, 3000.0, 6001.0]


def test_sale_rates_use_the_profile_bands_with_a_not_saleable_class():
    classes = build_classes(
        [row("zone", "sale_rate_eur_m2", 7, 6, 0.0, 2450.0, [1180, 1420, 1650, 1980])],
        price_breaks=[2100, 1300, 1700],
    )
    sale = classes["zone_cells"]["sale_rate_eur_m2"]
    assert sale == {
        "unit": "€/m²",
        "min": 0.0,
        "max": 2450.0,
        "count": 6,
        "null_count": 1,
        "zero_class": True,
        "method": "fixed",
        "breaks": [1300.0, 1700.0, 2100.0],
    }
    without_bands = build_classes([row("zone", "sale_rate_eur_m2", 3, 3, 0.0, 10.0, [2, 4, 6, 8])])
    assert without_bands["zone_cells"]["sale_rate_eur_m2"]["method"] == "quantile"


def test_empty_versions_and_the_schema():
    classes = build_classes([row("block", "max_height_m", 0, 0, None, None, None)])
    height = classes["block_cells"]["max_height_m"]
    assert height["breaks"] == [] and height["count"] == 0 and height["min"] is None
    CellClasses.model_validate(classes)  # the served shape


def test_one_statement_covers_every_metric():
    sql = str(CLASS_STATS_SQL)
    assert sql.count("UNION ALL") == len(METRICS) - 1
    for _, metric, _, _ in METRICS:
        assert f"ORDER BY {metric})" in sql


def test_sale_rate_range_uses_bounds_or_factors():
    base = dict(
        land_rate_eur_m2=500.0,
        build_rate_eur_m2=800.0,
        design_rate_eur_m2=60.0,
        sale_rate_eur_m2=2000.0,
        range_low_factor=0.85,
        range_high_factor=1.15,
    )
    assert sale_rate_range(MarketInputs(**base)) == (1700.0, 2300.0)
    assert sale_rate_range(MarketInputs(**base, sale_bounds=(1500.0, 2600.0))) == (1500.0, 2600.0)
    assert sale_rate_range(None) == (None, None)
