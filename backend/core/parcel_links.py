"""Cadastral <-> planned urban parcel links of a publish version (``parcel_links``, BRD §2.2).

For every served cadastral parcel: which planned urban parcels (of adopted, live, current-version
documents) cover it and by how much, and the reverse. One statement per version:

- candidates from the GiST ``&&`` / ``ST_Intersects`` join, intersections and areas in the
  municipality's metric CRS (the cadastre profile's ``area_crs_epsg``, EPSG:25834 for Podgorica);
- digitising slivers dropped: a link needs ``min_overlap_m2`` and ``min_overlap_fraction`` of the
  cadastral parcel (location resolution's thresholds);
- ranked per cadastral parcel like the panel's primary link: largest overlap, then smallest planned
  area, then lowest id (rank 1 = the calculation basis);
- one ``none`` row (no urban parcel, rank 1) for each cadastral parcel without a link: the panel's
  "Not defined" and the map's ``no_urban_parcel``.

The relation of a cadastral parcel (on each of its rows), first match:

- ``none``: no planned parcel over it;
- ``split``: two or more planned parcels each cover at least ``split_min_fraction`` (10 %) of it;
- ``merged``: its planned parcel covers at least that share of two or more cadastral parcels;
- ``reduced``: its planned parcel covers less than that share of it (mostly roads / public space);
- ``same``: each covers all but ``same_tolerance`` (2 %) of the other;
- ``enlarged``: the planned parcel is larger than it by more than the tolerance;
- ``reduced``: otherwise (smaller, or shifted off it).

``reduction_pct`` is the share of the cadastral parcel in no planned parcel (taken for roads /
public space), on every linked row: the client's example, a cadastral parcel of 100 whose planned
parcel is 75, is ``reduced`` by 25 %. The publish job recomputes the links of every new version;
``python -m core.parcel_links summary | recompute`` does it standalone for QA.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("urbanview.parcel_links")

RELATIONS = ("same", "reduced", "enlarged", "split", "merged", "none")
OVERCOVERED = 1.01  # shares of a cadastral parcel summing above this: planned parcels overlap


@dataclass(frozen=True, slots=True)
class LinkRules:
    min_overlap_m2: float = 1.0
    min_overlap_fraction: float = 0.02
    split_min_fraction: float = 0.10
    same_tolerance: float = 0.02

    @classmethod
    def from_settings(cls, settings: Any) -> LinkRules:
        return cls(
            min_overlap_m2=settings.locate_min_overlap_m2,
            min_overlap_fraction=settings.locate_min_overlap_fraction,
            split_min_fraction=settings.link_split_min_fraction,
            same_tolerance=settings.link_same_tolerance,
        )


LINKS_SQL = text(
    """
    WITH measured AS (
        SELECT c.id AS cadastral_parcel_id, u.id AS urban_parcel_id,
               c.area_m2 AS cadastral_area_m2, u.area_m2 AS urban_area_m2,
               ST_Area(ST_Transform(c.geom, CAST(:srid AS integer))) AS cad_m2,
               ST_Area(ST_Transform(u.geom, CAST(:srid AS integer))) AS up_m2,
               ST_Area(ST_Intersection(ST_Transform(c.geom, CAST(:srid AS integer)),
                                       ST_Transform(u.geom, CAST(:srid AS integer)))) AS overlap_m2
        FROM urban_parcels u
        JOIN planning_documents d ON d.id = u.document_id AND d.status = 'adopted'
             AND d.coverage_live AND d.is_current_version
        JOIN cadastral_parcels c ON c.municipality_id = u.municipality_id
             AND c.retired_at IS NULL AND c.geom && u.geom AND ST_Intersects(c.geom, u.geom)
        WHERE u.municipality_id = :m
    ),
    kept AS (
        SELECT m.*,
               CASE WHEN cad_m2 > 0 THEN overlap_m2 / cad_m2 ELSE 0 END AS ratio_c,
               CASE WHEN up_m2 > 0 THEN overlap_m2 / up_m2 ELSE 0 END AS ratio_u,
               row_number() OVER (PARTITION BY cadastral_parcel_id
                                  ORDER BY overlap_m2 DESC, urban_area_m2 ASC,
                                           urban_parcel_id ASC) AS rank
        FROM measured m
        WHERE overlap_m2 >= CAST(:min_m2 AS double precision)
          AND overlap_m2 >= CAST(:min_fraction AS double precision) * cad_m2
    ),
    per_cad AS (
        SELECT cadastral_parcel_id,
               count(*) FILTER (WHERE ratio_c >= CAST(:split AS double precision)) AS significant,
               sum(ratio_c) AS covered
        FROM kept GROUP BY cadastral_parcel_id
    ),
    per_up AS (
        SELECT urban_parcel_id,
               count(*) FILTER (WHERE ratio_c >= CAST(:split AS double precision)) AS significant
        FROM kept GROUP BY urban_parcel_id
    ),
    cases AS (
        SELECT k.cadastral_parcel_id,
               CASE WHEN pc.significant >= 2 THEN 'split'
                    WHEN k.ratio_c >= CAST(:split AS double precision) AND pu.significant >= 2
                        THEN 'merged'
                    WHEN k.ratio_c < CAST(:split AS double precision) THEN 'reduced'
                    WHEN k.ratio_c >= 1 - CAST(:same AS double precision)
                         AND k.ratio_u >= 1 - CAST(:same AS double precision) THEN 'same'
                    WHEN k.up_m2 > k.cad_m2 * (1 + CAST(:same AS double precision)) THEN 'enlarged'
                    ELSE 'reduced' END AS relation,
               round(CAST(greatest(0, 1 - pc.covered) * 100 AS numeric), 2) AS reduction_pct
        FROM kept k
        JOIN per_cad pc ON pc.cadastral_parcel_id = k.cadastral_parcel_id
        JOIN per_up pu ON pu.urban_parcel_id = k.urban_parcel_id
        WHERE k.rank = 1
    ),
    version AS (SELECT label FROM publish_versions WHERE id = :v),
    linked AS (
        INSERT INTO parcel_links (municipality_id, publish_version_id, dataset_version,
            cadastral_parcel_id, urban_parcel_id, cadastral_area_m2, urban_area_m2,
            overlap_area_m2, overlap_ratio_of_cadastral, overlap_ratio_of_urban, area_delta_m2,
            relation, reduction_pct, rank)
        SELECT :m, :v, (SELECT label FROM version), k.cadastral_parcel_id, k.urban_parcel_id,
               k.cadastral_area_m2, k.urban_area_m2, k.overlap_m2, k.ratio_c, k.ratio_u,
               k.urban_area_m2 - k.cadastral_area_m2, cs.relation, cs.reduction_pct, k.rank
        FROM kept k JOIN cases cs ON cs.cadastral_parcel_id = k.cadastral_parcel_id
        RETURNING cadastral_parcel_id
    )
    INSERT INTO parcel_links (municipality_id, publish_version_id, dataset_version,
        cadastral_parcel_id, urban_parcel_id, cadastral_area_m2, overlap_area_m2,
        overlap_ratio_of_cadastral, relation, rank)
    SELECT :m, :v, (SELECT label FROM version), c.id, NULL, c.area_m2, 0, 0, 'none', 1
    FROM cadastral_parcels c
    WHERE c.municipality_id = :m AND c.retired_at IS NULL
      AND NOT EXISTS (SELECT 1 FROM kept k WHERE k.cadastral_parcel_id = c.id)
    """
)
DELETE_SQL = text("DELETE FROM parcel_links WHERE publish_version_id = :v")
SUMMARY_SQL = text(
    """
    WITH parcels AS (
        SELECT cadastral_parcel_id, min(relation) AS relation, max(reduction_pct) AS reduction,
               count(urban_parcel_id) AS links, sum(overlap_ratio_of_cadastral) AS covered
        FROM parcel_links WHERE publish_version_id = :v
        GROUP BY cadastral_parcel_id
    )
    SELECT relation, count(*) AS parcels, sum(links) AS links,
           avg(reduction) AS average_reduction,
           count(*) FILTER (WHERE covered > CAST(:over AS double precision)) AS overcovered
    FROM parcels GROUP BY relation
    """
)
MERGED_SQL = text(
    """
    SELECT count(DISTINCT urban_parcel_id) FROM parcel_links
    WHERE publish_version_id = :v AND relation = 'merged'
    """
)
VERSION_SQL = text(
    "SELECT id, label, links_summary FROM publish_versions WHERE id = :v AND municipality_id = :m"
)
CURRENT_VERSION_SQL = text(
    "SELECT id FROM publish_versions WHERE municipality_id = :m AND is_current"
)
STORE_SUMMARY_SQL = text(
    "UPDATE publish_versions SET links_summary = CAST(:summary AS jsonb) WHERE id = :v"
)


def metric_srid(municipality_id: str) -> int:
    """The municipality's metric CRS for areas (the cadastre profile's ``area_crs_epsg``)."""
    from core.cadastre.config import load_cadastre_profile

    return int(load_cadastre_profile(municipality_id).area_crs_epsg)


async def summarize(session: AsyncSession, *, version_id: int) -> dict[str, Any]:
    """Parcels per relation, links, parcels without a link, the average reduction of the
    ``reduced`` parcels, merged planned parcels, over-covered cadastral parcels."""
    rows = (
        (await session.execute(SUMMARY_SQL, {"v": version_id, "over": OVERCOVERED}))
        .mappings()
        .all()
    )
    relations = dict.fromkeys(RELATIONS, 0)
    links = overcovered = 0
    average = None
    for r in rows:
        relations[r["relation"]] = int(r["parcels"])
        links += int(r["links"] or 0)
        overcovered += int(r["overcovered"] or 0)
        if r["relation"] == "reduced" and r["average_reduction"] is not None:
            average = round(float(r["average_reduction"]), 2)
    merged = (await session.execute(MERGED_SQL, {"v": version_id})).scalar_one()
    return {
        "cadastral_parcels": sum(relations.values()),
        "links": links,
        "relations": relations,
        "no_urban_parcel": relations["none"],
        "average_reduction_pct": average,
        "merged_urban_parcels": int(merged or 0),
        "overcovered_parcels": overcovered,
    }


async def recompute_parcel_links(
    session: AsyncSession,
    *,
    municipality_id: str,
    version_id: int,
    rules: LinkRules | None = None,
    srid: int | None = None,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    """Replace the links of ``version_id`` inside the caller's transaction (the caller commits)
    and store their summary on the version. Returns the summary with ``parcel_links`` (linked
    pairs), ``cadastral_unmatched`` (parcels without a planned parcel) and ``duration_ms``; a
    recompute slower than ``max_seconds`` (``PARCEL_LINKS_MAX_SECONDS``) is logged as a warning."""
    rules = rules or LinkRules()
    srid = srid or metric_srid(municipality_id)
    started = perf_counter()
    await session.execute(DELETE_SQL, {"v": version_id})
    await session.execute(
        LINKS_SQL,
        {
            "m": municipality_id,
            "v": version_id,
            "srid": srid,
            "min_m2": float(rules.min_overlap_m2),
            "min_fraction": float(rules.min_overlap_fraction),
            "split": float(rules.split_min_fraction),
            "same": float(rules.same_tolerance),
        },
    )
    summary = await summarize(session, version_id=version_id)
    duration_ms = round((perf_counter() - started) * 1000)
    summary.update(
        rules=asdict(rules),
        metric_srid=srid,
        duration_ms=duration_ms,
        computed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    await session.execute(STORE_SUMMARY_SQL, {"v": version_id, "summary": json.dumps(summary)})
    if max_seconds is None:
        from core.config import get_settings

        max_seconds = get_settings().parcel_links_max_seconds
    message = (
        "parcel links of version %s: %s cadastral parcels, %s links, %s without a planned parcel,"
        " %s in %s ms"
    )
    args = (
        version_id,
        summary["cadastral_parcels"],
        summary["links"],
        summary["no_urban_parcel"],
        summary["relations"],
        duration_ms,
    )
    if duration_ms > max_seconds * 1000:
        log.warning(message + " (over the %s s limit)", *args, max_seconds)
    else:
        log.info(message, *args)
    return {
        **summary,
        "parcel_links": summary["links"],
        "cadastral_unmatched": summary["no_urban_parcel"],
    }


async def recompute_current_links(
    session: AsyncSession,
    *,
    municipality_id: str,
    rules: LinkRules | None = None,
    srid: int | None = None,
) -> dict[str, Any] | None:
    """The same for the current version; ``None`` when nothing is published."""
    version_id = (
        await session.execute(CURRENT_VERSION_SQL, {"m": municipality_id})
    ).scalar_one_or_none()
    if version_id is None:
        return None
    return await recompute_parcel_links(
        session,
        municipality_id=municipality_id,
        version_id=int(version_id),
        rules=rules,
        srid=srid,
    )


# --- command line (QA) ----------------------------------------------------------------------------


def _print(summary: dict[str, Any], label: str) -> None:
    relations = summary["relations"]
    print(
        f"version {label}: {summary['cadastral_parcels']} cadastral parcels, "
        f"{summary['links']} links, {summary['no_urban_parcel']} without a planned parcel"
    )
    print("  " + "  ".join(f"{k} {relations.get(k, 0)}" for k in RELATIONS))
    average = summary.get("average_reduction_pct")
    print(
        f"  average reduction of the reduced parcels: "
        f"{'-' if average is None else f'{average} %'}; merged planned parcels "
        f"{summary['merged_urban_parcels']}; cadastral parcels covered more than once "
        f"{summary['overcovered_parcels']}"
    )
    if "duration_ms" in summary:
        print(f"  computed in {summary['duration_ms']} ms at {summary.get('computed_at')}")


def _engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    return create_async_engine(get_settings().database_url)


async def _run(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from core.config import get_settings

    settings = get_settings()
    m = args.municipality or settings.municipality_id
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
            row = (await session.execute(VERSION_SQL, {"v": version_id, "m": m})).mappings().first()
            if row is None:
                print(f"no publish version {version_id} in {m}", file=sys.stderr)
                return 1
            if args.command == "recompute":
                summary = await recompute_parcel_links(
                    session,
                    municipality_id=m,
                    version_id=int(version_id),
                    rules=LinkRules.from_settings(settings),
                )
                await session.commit()
            else:
                summary = {**(row["links_summary"] or {})}
                summary.update(await summarize(session, version_id=int(version_id)))
    finally:
        await engine.dispose()
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        _print(summary, row["label"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.parcel_links",
        description="Cadastral <-> planned parcel links of a publish version: QA summary "
        "(parcels per relation, parcels without a planned parcel, average reduction) or a "
        "recompute of the version's links (default: the current version).",
    )
    parser.add_argument("command", choices=("summary", "recompute"))
    parser.add_argument("--municipality")
    parser.add_argument("--version", type=int, help="publish version id (default: current)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
