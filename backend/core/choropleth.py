"""Choropleth cells of a publish version: the planning and market heatmaps (BRD §2.1).

One row per layer and cell in ``choropleth_cells``, one set of classes per layer in
``choropleth_classes``, and one tile source-layer per heatmap (``heat_<layer>``):

- ``coverage``: max site coverage % (IZ) per urban block, the **area-weighted mean** over the
  block's planned parcels that state it (weights: the planned parcel areas);
- ``far``: max floor area ratio (II) per block, area-weighted mean the same way;
- ``height``: floors above ground per block, the **maximum** over its parcels of the floor
  notation parsed with the profile's tokens (``P+4`` = 5, ``P+5+Pk`` = 7); ``label`` keeps the
  notation as the plan prints it;
- ``gfa``: max gross floor area per block, the **sum** of FAR x planned parcel area over its
  parcels with a FAR (the engine's formula);
- ``sale_price``: per zone, the expected sale €/m² of the zone's assumptions version that applies
  today (``core.assumptions``), with its low / high (absolute bounds, else expected x factors).

A parcel's value is its effective published value of the version: parcel -> block -> zone ->
document (the panel's precedence), so a block-level figure the plan states applies to every parcel
of the block. A block or zone without a value gets no cell: absence of data is a fact, and the
tiles draw it as not covered, never as zero.

Classes, stored with the cells so the legend and the tiles agree: quintiles of the version's
values for the planning layers (rounded, de-duplicated), the profile's fixed €/m² bands for the
sale price with 0 as its own "not saleable" class. ``value_band`` is the cell's legend row: the
number of breaks <= value, and for the sale price 0 = not saleable, then 1 + that number.

The publish job computes every layer after the parcel links; ``refresh_heatmaps`` recomputes the
sale price when another assumptions version applies; ``python -m core.choropleth summary |
recompute`` is the QA command (min / max / mean per layer).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.assumptions import live_versions_sql
from core.extraction.normalise import FloorTokens, parse_floors

log = logging.getLogger("urbanview.choropleth")

PLANNING_LAYERS = ("coverage", "far", "height", "gfa")
LAYERS = (*PLANNING_LAYERS, "sale_price")
UNITS = {"coverage": "%", "far": "", "height": "floors", "gfa": "m²", "sale_price": "€/m²"}
DECIMALS = {"coverage": 1, "far": 2, "height": 0, "gfa": 0, "sale_price": 0}
QUANTILES = (0.2, 0.4, 0.6, 0.8)


def source_layer(layer: str) -> str:
    return f"heat_{layer}"


# --- block aggregation (pure) ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParcelInputs:
    area_m2: float
    max_far: float | None = None
    max_site_coverage_pct: float | None = None
    max_floors: str | None = None


@dataclass(frozen=True, slots=True)
class CellValue:
    value: float
    parcel_count: int
    label: str | None = None
    value_low: float | None = None
    value_high: float | None = None


def _weighted(pairs: list[tuple[float, float]]) -> float:
    """Area-weighted mean of (value, area); parcels without an area count equally."""
    weight = sum(a for _, a in pairs if a > 0)
    if weight > 0:
        return sum(v * a for v, a in pairs if a > 0) / weight
    return sum(v for v, _ in pairs) / len(pairs)


def aggregate_block(
    parcels: Iterable[ParcelInputs], tokens: FloorTokens | None
) -> dict[str, CellValue]:
    """The block's value per planning layer (only the layers some parcel states)."""
    coverage: list[tuple[float, float]] = []
    far: list[tuple[float, float]] = []
    floors: list[tuple[int, str]] = []
    gfa = 0.0
    gfa_parcels = 0
    for p in parcels:
        area = float(p.area_m2 or 0.0)
        if p.max_site_coverage_pct is not None:
            coverage.append((float(p.max_site_coverage_pct), area))
        if p.max_far is not None:
            far.append((float(p.max_far), area))
            gfa += float(p.max_far) * area
            gfa_parcels += 1
        if p.max_floors and tokens is not None:
            count = parse_floors(p.max_floors, tokens)
            if count is not None:
                floors.append((count.above_ground, count.notation))
    out: dict[str, CellValue] = {}
    if coverage:
        out["coverage"] = CellValue(_weighted(coverage), len(coverage))
    if far:
        out["far"] = CellValue(_weighted(far), len(far))
        out["gfa"] = CellValue(gfa, gfa_parcels)
    if floors:
        top = max(n for n, _ in floors)
        label = next(notation for n, notation in floors if n == top)
        out["height"] = CellValue(float(top), len(floors), label=label)
    return out


# --- classes (pure) -------------------------------------------------------------------------------


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """``percentile_cont``: linear interpolation between the closest ranks."""
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo))


def _rounded(values: Iterable[float], decimals: int) -> list[float]:
    out: list[float] = []
    for v in values:
        r = round(float(v), decimals)
        if decimals == 0:
            r = float(int(r))
        if not out or r > out[-1]:
            out.append(r)
    return out


def quantile_breaks(values: Sequence[float], decimals: int) -> list[float]:
    """Class starts after the first class: the quintiles of the positive values, strictly above
    the smallest value, rounded and de-duplicated (fewer distinct values, fewer classes)."""
    positive = sorted(float(v) for v in values if v is not None and v > 0)
    if not positive:
        return []
    floor = min(float(v) for v in values if v is not None)
    candidates = [percentile(positive, q) for q in QUANTILES]
    return _rounded((c for c in candidates if c > floor), decimals)


def band(value: float, breaks: Sequence[float], *, zero_class: bool) -> int:
    """The cell's legend row: the number of breaks <= value; with a zero class, 0 for a value
    <= 0 ("not saleable") and 1 + that number otherwise."""
    rank = sum(1 for b in breaks if b <= value)
    if zero_class:
        return 0 if value <= 0 else 1 + rank
    return rank


@dataclass
class LayerClasses:
    layer: str
    method: str  # quantile | fixed
    breaks: list[float]
    count: int
    null_count: int
    min: float | None
    max: float | None
    mean: float | None
    zero_class: bool
    unit: str = ""
    decimals: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "source_layer": source_layer(self.layer),
            "method": self.method,
            "unit": self.unit,
            "breaks": self.breaks,
            "min": self.min,
            "max": self.max,
            "mean": self.mean,
            "count": self.count,
            "null_count": self.null_count,
            "zero_class": self.zero_class,
            "decimals": self.decimals,
        }


def layer_classes(
    layer: str, values: Sequence[float], cells_total: int, *, price_breaks: Sequence[float] = ()
) -> LayerClasses:
    decimals = DECIMALS[layer]
    stats = [float(v) for v in values]
    lo = round(min(stats), decimals) if stats else None
    hi = round(max(stats), decimals) if stats else None
    mean = round(sum(stats) / len(stats), decimals + 1) if stats else None
    if layer == "sale_price" and price_breaks:
        method, breaks = "fixed", _rounded(sorted(price_breaks), decimals)
    else:
        method, breaks = "quantile", quantile_breaks(stats, decimals)
    return LayerClasses(
        layer=layer,
        method=method,
        breaks=breaks,
        count=len(stats),
        null_count=max(0, cells_total - len(stats)),
        min=lo,
        max=hi,
        mean=mean,
        zero_class=layer == "sale_price",
        unit=UNITS[layer],
        decimals=decimals,
    )


# --- SQL ------------------------------------------------------------------------------------------

# planned parcels of adopted, live, current documents with a block, and their effective values
PARCELS_SQL = """
    SELECT u.id, u.block_id, u.area_m2,
           {far} AS max_far, {coverage} AS max_site_coverage_pct, {floors} AS max_floors
    FROM urban_parcels u
    JOIN planning_documents d ON d.id = u.document_id
    JOIN urban_blocks b ON b.id = u.block_id
    WHERE u.municipality_id = :m AND d.status = 'adopted' AND d.coverage_live
      AND d.is_current_version
    ORDER BY u.id
"""
# the zones' assumptions versions that apply today (a zone without its own has no sale price)
MARKET_SQL = text(
    f"""
    SELECT live.id, live.zone_id, live.version, live.sale_rate_eur_m2, live.sale_rate_low_eur_m2,
           live.sale_rate_high_eur_m2, live.range_low_factor, live.range_high_factor
    FROM ({live_versions_sql()}) live
    JOIN zones z ON z.id = live.zone_id AND z.municipality_id = :m
    ORDER BY live.zone_id
    """
)
COUNT_SQL = {
    "block": text("SELECT count(*) FROM urban_blocks WHERE municipality_id = :m"),
    "zone": text("SELECT count(*) FROM zones WHERE municipality_id = :m"),
}
DELETE_CELLS_SQL = text(
    "DELETE FROM choropleth_cells WHERE publish_version_id = :v AND layer = ANY(:layers)"
)
DELETE_CLASSES_SQL = text(
    "DELETE FROM choropleth_classes WHERE publish_version_id = :v AND layer = ANY(:layers)"
)
INSERT_BLOCK_SQL = text(
    """
    INSERT INTO choropleth_cells (municipality_id, publish_version_id, layer, cell_type, cell_id,
        cell_ref, geom, value, value_band, unit, label, parcel_count, source_kind,
        dataset_version)
    SELECT :m, :v, :layer, 'block', b.id, b.block_ref, b.geom, :value, :band, :unit, :label,
           :parcels, 'planning', (SELECT label FROM publish_versions WHERE id = :v)
    FROM urban_blocks b WHERE b.id = :cell AND b.municipality_id = :m
    """
)
INSERT_ZONE_SQL = text(
    """
    INSERT INTO choropleth_cells (municipality_id, publish_version_id, layer, cell_type, cell_id,
        cell_ref, geom, value, value_low, value_high, value_band, unit, source_kind,
        dataset_version, assumptions_id, assumptions_version)
    SELECT :m, :v, 'sale_price', 'zone', z.id, z.name, z.geom, :value, :low, :high, :band, :unit,
           'assumptions', (SELECT label FROM publish_versions WHERE id = :v), :aid, :aversion
    FROM zones z WHERE z.id = :cell AND z.municipality_id = :m
    """
)
INSERT_CLASSES_SQL = text(
    """
    INSERT INTO choropleth_classes (municipality_id, publish_version_id, layer, method, unit,
        breaks, min, max, mean, count, null_count, zero_class, decimals)
    VALUES (:m, :v, :layer, :method, :unit, CAST(:breaks AS jsonb), :min, :max, :mean, :count,
            :null_count, :zero_class, :decimals)
    """
)
CLASSES_SQL = text(
    """
    SELECT layer, method, unit, breaks, min, max, mean, count, null_count, zero_class, decimals
    FROM choropleth_classes WHERE publish_version_id = :v ORDER BY layer
    """
)
CURRENT_VERSION_SQL = text(
    "SELECT id FROM publish_versions WHERE municipality_id = :m AND is_current"
)
STALE_SQL = text(
    f"""
    SELECT
        COALESCE((SELECT array_agg(assumptions_id ORDER BY assumptions_id)
                  FROM choropleth_cells
                  WHERE publish_version_id = :v AND layer = 'sale_price'), '{{}}') AS cells,
        COALESCE((SELECT array_agg(id ORDER BY id)
                  FROM ({live_versions_sql()}) live WHERE live.zone_id IS NOT NULL
                    AND EXISTS (SELECT 1 FROM zones z WHERE z.id = live.zone_id)),
                 '{{}}') AS live
    """
)


def _effective(key: str, col: str = "value_number") -> str:
    from jobs.publish_layers import effective  # the precedence the tiles and panels share

    return effective(key, col)


def _parcels_sql():
    return text(
        PARCELS_SQL.format(
            far=_effective("max_far"),
            coverage=_effective("max_site_coverage_pct"),
            floors=_effective("max_floors", "value_text"),
        )
    )


def floor_tokens(municipality_id: str) -> FloorTokens | None:
    from core.municipality import load_extraction_profile

    extraction = load_extraction_profile(municipality_id)
    if extraction is None:
        return None
    notation = extraction.floor_notation
    return FloorTokens.of(notation.below_ground, notation.ground, notation.attic)


def sale_range(row: Mapping[str, Any]) -> tuple[float, float]:
    """Low / high of a zone's sale rate: its absolute bounds, else expected x the range factors
    (the bounds the feasibility engine uses)."""
    rate = float(row["sale_rate_eur_m2"])
    if row["sale_rate_low_eur_m2"] is not None and row["sale_rate_high_eur_m2"] is not None:
        return float(row["sale_rate_low_eur_m2"]), float(row["sale_rate_high_eur_m2"])
    return (
        round(rate * float(row["range_low_factor"]), 2),
        round(rate * float(row["range_high_factor"]), 2),
    )


# --- computation ----------------------------------------------------------------------------------


async def _store_classes(
    session: AsyncSession, m: str, version_id: int, classes: LayerClasses
) -> None:
    await session.execute(
        INSERT_CLASSES_SQL,
        {
            "m": m,
            "v": version_id,
            "layer": classes.layer,
            "method": classes.method,
            "unit": classes.unit,
            "breaks": json.dumps(classes.breaks),
            "min": classes.min,
            "max": classes.max,
            "mean": classes.mean,
            "count": classes.count,
            "null_count": classes.null_count,
            "zero_class": classes.zero_class,
            "decimals": classes.decimals,
        },
    )


async def compute_planning(
    session: AsyncSession, *, municipality_id: str, version_id: int, tokens: FloorTokens | None
) -> dict[str, LayerClasses]:
    m = municipality_id
    rows = (await session.execute(_parcels_sql(), {"m": m, "v": version_id})).mappings().all()
    by_block: dict[int, list[ParcelInputs]] = {}
    for r in rows:
        by_block.setdefault(int(r["block_id"]), []).append(
            ParcelInputs(
                area_m2=float(r["area_m2"] or 0.0),
                max_far=None if r["max_far"] is None else float(r["max_far"]),
                max_site_coverage_pct=None
                if r["max_site_coverage_pct"] is None
                else float(r["max_site_coverage_pct"]),
                max_floors=r["max_floors"],
            )
        )
    values: dict[str, dict[int, CellValue]] = {layer: {} for layer in PLANNING_LAYERS}
    for block_id, parcels in by_block.items():
        for layer, cell in aggregate_block(parcels, tokens).items():
            values[layer][block_id] = cell
    blocks = int((await session.execute(COUNT_SQL["block"], {"m": m})).scalar_one())
    classes: dict[str, LayerClasses] = {}
    for layer in PLANNING_LAYERS:
        # the stored value is what the tiles carry: classes and bands are computed on it
        cells = {k: round(c.value, DECIMALS[layer] + 2) for k, c in values[layer].items()}
        cls = layer_classes(layer, list(cells.values()), blocks)
        classes[layer] = cls
        if cells:
            await session.execute(
                INSERT_BLOCK_SQL,
                [
                    {
                        "m": m,
                        "v": version_id,
                        "layer": layer,
                        "cell": block_id,
                        "value": value,
                        "band": band(value, cls.breaks, zero_class=False),
                        "unit": UNITS[layer],
                        "label": values[layer][block_id].label,
                        "parcels": values[layer][block_id].parcel_count,
                    }
                    for block_id, value in sorted(cells.items())
                ],
            )
        await _store_classes(session, m, version_id, cls)
    return classes


async def compute_sale_price(
    session: AsyncSession,
    *,
    municipality_id: str,
    version_id: int,
    timezone: str,
    price_breaks: Sequence[float],
) -> LayerClasses:
    m = municipality_id
    rows = (await session.execute(MARKET_SQL, {"m": m, "tz": timezone})).mappings().all()
    zones = int((await session.execute(COUNT_SQL["zone"], {"m": m})).scalar_one())
    cls = layer_classes(
        "sale_price", [float(r["sale_rate_eur_m2"]) for r in rows], zones, price_breaks=price_breaks
    )
    if rows:
        params = []
        for r in rows:
            low, high = sale_range(r)
            value = float(r["sale_rate_eur_m2"])
            params.append(
                {
                    "m": m,
                    "v": version_id,
                    "cell": int(r["zone_id"]),
                    "value": value,
                    "low": low,
                    "high": high,
                    "band": band(value, cls.breaks, zero_class=True),
                    "unit": UNITS["sale_price"],
                    "aid": int(r["id"]),
                    "aversion": str(r["version"]),
                }
            )
        await session.execute(INSERT_ZONE_SQL, params)
    await _store_classes(session, m, version_id, cls)
    return cls


async def compute_choropleth(
    session: AsyncSession,
    *,
    municipality_id: str,
    version_id: int,
    timezone: str,
    price_breaks: Sequence[float],
    layers: Sequence[str] = LAYERS,
    tokens: FloorTokens | None = None,
) -> dict[str, Any]:
    """Replace the cells and classes of ``layers`` for ``version_id`` inside the caller's
    transaction (the caller commits). Returns the summary per layer."""
    unknown = sorted(set(layers) - set(LAYERS))
    if unknown:
        raise ValueError(f"unknown choropleth layers {unknown}; use {list(LAYERS)}")
    # the planning layers come from one pass over the parcels: they are recomputed together
    planning = any(layer in PLANNING_LAYERS for layer in layers)
    targets = [
        *(PLANNING_LAYERS if planning else ()),
        *(("sale_price",) if "sale_price" in layers else ()),
    ]
    await session.execute(DELETE_CELLS_SQL, {"v": version_id, "layers": targets})
    await session.execute(DELETE_CLASSES_SQL, {"v": version_id, "layers": targets})
    classes: dict[str, LayerClasses] = {}
    if planning:
        tokens = tokens if tokens is not None else floor_tokens(municipality_id)
        classes.update(
            await compute_planning(
                session, municipality_id=municipality_id, version_id=version_id, tokens=tokens
            )
        )
    if "sale_price" in layers:
        classes["sale_price"] = await compute_sale_price(
            session,
            municipality_id=municipality_id,
            version_id=version_id,
            timezone=timezone,
            price_breaks=price_breaks,
        )
    summary = {layer: c.as_json() for layer, c in classes.items()}
    log.info(
        "choropleth of version %s: %s",
        version_id,
        {k: (v["count"], v["null_count"]) for k, v in summary.items()},
    )
    return summary


async def stored_classes(session: AsyncSession, version_id: int) -> dict[str, dict[str, Any]]:
    """The version's stored classes per layer (what its cells and tiles were built with)."""
    out: dict[str, dict[str, Any]] = {}
    for r in (await session.execute(CLASSES_SQL, {"v": version_id})).mappings():
        out[r["layer"]] = {
            "layer": r["layer"],
            "source_layer": source_layer(r["layer"]),
            "method": r["method"],
            "unit": r["unit"],
            "breaks": list(r["breaks"] or []),
            "min": r["min"],
            "max": r["max"],
            "mean": r["mean"],
            "count": r["count"],
            "null_count": r["null_count"],
            "zero_class": r["zero_class"],
            "decimals": r["decimals"],
        }
    return out


async def sale_price_stale(
    session: AsyncSession, *, version_id: int, timezone: str, m: str
) -> bool:
    """Whether the version's sale-price cells come from other assumptions versions than the ones
    that apply today (a version saved or scheduled since): then they need a refresh."""
    row = (
        (await session.execute(STALE_SQL, {"v": version_id, "m": m, "tz": timezone}))
        .mappings()
        .one()
    )
    return list(row["cells"] or []) != list(row["live"] or [])


# --- command line (QA) ----------------------------------------------------------------------------


def _engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    return create_async_engine(get_settings().database_url)


def _print(classes: Mapping[str, Mapping[str, Any]]) -> None:
    for layer in LAYERS:
        c = classes.get(layer)
        if c is None:
            print(f"{layer:11} no classes stored")
            continue
        unit = f" {c['unit']}" if c["unit"] else ""
        print(
            f"{layer:11} {c['count']:>6} cells  {c['null_count']:>6} without a value  "
            f"min {c['min']}  max {c['max']}  mean {c['mean']}{unit}  "
            f"{c['method']} breaks {c['breaks']}"
        )


async def _run(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from core.config import get_settings
    from core.municipality import load_profile

    settings = get_settings()
    m = args.municipality or settings.municipality_id
    profile = load_profile(m)
    engine = _engine()
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            version_id = (
                args.version
                or (await session.execute(CURRENT_VERSION_SQL, {"m": m})).scalar_one_or_none()
            )
            if version_id is None:
                print("nothing is published yet", file=sys.stderr)
                return 1
            if args.command == "recompute":
                await compute_choropleth(
                    session,
                    municipality_id=m,
                    version_id=int(version_id),
                    timezone=profile.timezone,
                    price_breaks=profile.price_band_breaks_eur_m2,
                    layers=args.layer or LAYERS,
                )
                await session.commit()
            classes = await stored_classes(session, int(version_id))
    finally:
        await engine.dispose()
    if args.json:
        print(json.dumps(classes, indent=2, ensure_ascii=False))
    else:
        print(f"version {version_id}")
        _print(classes)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.choropleth",
        description="Heatmap cells of a publish version: min / max / mean per layer and the "
        "stored classes (summary), or a recompute (default: the current version). A recompute "
        "does not rebuild the tiles: publish, or run the refresh_heatmaps job, for that.",
    )
    parser.add_argument("command", choices=("summary", "recompute"))
    parser.add_argument("--municipality")
    parser.add_argument("--version", type=int, help="publish version id (default: current)")
    parser.add_argument("--layer", action="append", choices=LAYERS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
