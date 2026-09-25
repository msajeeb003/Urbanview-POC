"""Classification of the choropleth cells of a publish version, served with the tile pointer.

The public map colours the parameter choropleth (block cells: FAR, site coverage, height, GFA)
and the sale-price choropleth (zone cells: low / expected / high sale rate) by classes, and draws
its legend from the same classes, so legend and colours always match. The classes belong to the
version's cells, which never change after the publish, so they are computed on read in one
statement (no stored copy that could drift):

- parameter metrics: **quantile** breaks (quintiles) over the version's non-null values, rounded
  and de-duplicated; fewer distinct values give fewer classes;
- sale rates: the municipality profile's **fixed** €/m² bands (`price_band_breaks_eur_m2`, place
  data, not code), with 0 as its own "not saleable" class; without configured bands, quantiles.

Null values (no stated parameter, no market row) are counted in ``null_count`` and drawn with the
map's "no data" pattern, never as the lowest class.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import text

# (cell type, metric column, unit, decimals for the breaks)
METRICS: tuple[tuple[str, str, str | None, int], ...] = (
    ("block", "max_far", None, 2),
    ("block", "max_site_coverage_pct", "%", 1),
    ("block", "max_height_m", "m", 1),
    ("block", "max_gfa_m2", "m²", 0),
    ("zone", "sale_rate_eur_m2", "€/m²", 0),
    ("zone", "sale_rate_low_eur_m2", "€/m²", 0),
    ("zone", "sale_rate_high_eur_m2", "€/m²", 0),
)
SALE_METRICS = frozenset({"sale_rate_eur_m2", "sale_rate_low_eur_m2", "sale_rate_high_eur_m2"})
QUANTILES = (0.2, 0.4, 0.6, 0.8)
LAYER_OF = {"block": "block_cells", "zone": "zone_cells"}

_ONE = """
    SELECT '{cell_type}' AS cell_type, '{metric}' AS metric,
           count(*) AS cells, count({metric}) AS stated,
           min({metric}) AS min, max({metric}) AS max,
           percentile_cont(ARRAY[{quantiles}]) WITHIN GROUP (ORDER BY {metric})
               FILTER (WHERE {metric} IS NOT NULL AND {metric} > 0) AS quantiles
    FROM heatmap_cells
    WHERE publish_version_id = :v AND cell_type = '{cell_type}'
"""
CLASS_STATS_SQL = text(
    " UNION ALL ".join(
        _ONE.format(
            cell_type=cell_type, metric=metric, quantiles=", ".join(str(q) for q in QUANTILES)
        )
        for cell_type, metric, _, _ in METRICS
    )
)


def _rounded(values: Iterable[float], decimals: int) -> list[float]:
    out: list[float] = []
    for v in values:
        r = round(float(v), decimals)
        if decimals == 0:
            r = float(int(r))
        if not out or r > out[-1]:
            out.append(r)
    return out


def build_classes(
    rows: Iterable[Mapping[str, Any]], *, price_breaks: Sequence[float] = ()
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{layer: {metric: {method, unit, breaks, min, max, count, null_count, zero_class}}}``.

    ``breaks`` are ascending class starts after the first class: a value ``v`` is in class ``i``
    = the number of breaks ``<= v`` (the map's ``step`` expression). ``zero_class``: 0 is its own
    class ("not saleable") before the others.
    """
    units = {(c, m): (u, d) for c, m, u, d in METRICS}
    out: dict[str, dict[str, dict[str, Any]]] = {"block_cells": {}, "zone_cells": {}}
    for row in rows:
        cell_type, metric = row["cell_type"], row["metric"]
        unit, decimals = units[(cell_type, metric)]
        lo, hi = row["min"], row["max"]
        entry: dict[str, Any] = {
            "unit": unit,
            "min": None if lo is None else round(float(lo), decimals),
            "max": None if hi is None else round(float(hi), decimals),
            "count": int(row["stated"] or 0),
            "null_count": int((row["cells"] or 0) - (row["stated"] or 0)),
            "zero_class": metric in SALE_METRICS,
        }
        if metric in SALE_METRICS and price_breaks:
            entry["method"] = "fixed"
            entry["breaks"] = _rounded(sorted(price_breaks), decimals)
        else:
            entry["method"] = "quantile"
            quantiles = row["quantiles"] or []
            # class starts strictly above the smallest value: the first class starts at the minimum
            floor = float(lo) if lo is not None else None
            candidates = [q for q in quantiles if q is not None and (floor is None or q > floor)]
            entry["breaks"] = _rounded(candidates, decimals)
        out[LAYER_OF[cell_type]][metric] = entry
    return out


async def cell_classes(session: Any, version_id: int, *, price_breaks: Sequence[float] = ()):
    rows = (await session.execute(CLASS_STATS_SQL, {"v": version_id})).mappings().all()
    return build_classes(rows, price_breaks=price_breaks)
