"""Validity QA of a staged geometry batch: the pilot technical scope's ``staging.geometry_draft``
``qa_status`` / ``qa_issues``. It runs when a producer stages a batch (georeferencing and GIS
drawings, the zone import, the cadastral import) and on demand (``python -m core.geometry_qa
recheck``); the review API shows it and refuses to approve a batch that fails.

The POC plan funds geometry validity checks, not automatic topology QA (overlaps, gaps and area
deviations are the reviewer's eye on the preview). Per layer (``LAYER_CHECKS``, data):

- **validity** (errors): an empty or invalid geometry. A failing batch cannot be approved: the
  producer is fixed and the batch staged again.

The producing dataset's own recorded warnings (georeferencing, zone and cadastral validation:
``georef.*``, ``zones.*``, ``cadastre.*``) are listed with the batches they concern. ``qa_status``:
fail with any error, warn with any warning, else pass. Issues carry a code, a severity, an English
sentence for the console, a count, up to ``MAX_LISTED`` feature keys, and the area concerned when
the producer recorded one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MAX_LISTED = 25

LAYER_CHECKS: dict[str, frozenset[str]] = {
    "urban_parcels": frozenset({"validity"}),
    "urban_blocks": frozenset({"validity"}),
    "document_coverage": frozenset({"validity"}),
    "zones": frozenset({"validity"}),
    "cadastral_parcels": frozenset({"validity"}),
    "cadastral_municipalities": frozenset({"validity"}),
    "land_use": frozenset({"validity"}),
}
LAYER_LABELS: dict[str, str] = {
    "urban_parcels": "Planned urban parcels",
    "urban_blocks": "Urban blocks",
    "document_coverage": "Plan boundary",
    "zones": "Zones",
    "cadastral_parcels": "Cadastral parcels",
    "cadastral_municipalities": "Cadastral municipalities (KO)",
    "land_use": "Land use",
}
# dataset warnings that concern one layer; any other applies to every batch of the dataset
DATASET_WARNING_LAYERS: dict[str, frozenset[str]] = {
    "unnumbered_parcels": frozenset({"urban_parcels"}),
    "repeated_parcel_numbers": frozenset({"urban_parcels"}),
}

# a georeferencing dataset's source -> the pilot scope's origin of its batches
GEOREF_ORIGINS: dict[str, str] = {
    "extraction": "vector_pdf",
    "manual_redraw": "manual_qgis",
    "gis_file": "official_gis",
}

Severity = Literal["error", "warning"]
QaStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    severity: Severity
    message: str
    count: int = 1
    features: tuple[str, ...] = ()  # what the console lists (the feature keys)
    keys: tuple[str, ...] = ()  # the feature keys concerned, for the preview to mark
    area_m2: float | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "count": self.count,
            "features": list(self.features),
            "keys": list(self.keys),
            "area_m2": self.area_m2,
        }


@dataclass(slots=True)
class QaResult:
    status: QaStatus
    issues: list[Issue] = field(default_factory=list)
    checks: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": list(self.checks),
            "issues": [i.as_json() for i in self.issues],
        }


def status_of(issues: Iterable[Issue]) -> QaStatus:
    severities = {i.severity for i in issues}
    if "error" in severities:
        return "fail"
    return "warn" if "warning" in severities else "pass"


# --- SQL ------------------------------------------------------------------------------------------

BATCH_SQL = text(
    """
    SELECT id, municipality_id, layer_id, status, document_id, dataset_version
    FROM geometry_batches WHERE id = :batch
    """
)
VALIDITY_SQL = text(
    """
    SELECT feature_key, ST_IsEmpty(geom) AS empty,
           CASE WHEN NOT ST_IsEmpty(geom) THEN ST_IsValidReason(geom) END AS reason,
           count(*) FILTER (WHERE ST_IsEmpty(geom)) OVER () AS n_empty,
           count(*) FILTER (WHERE NOT ST_IsEmpty(geom)) OVER () AS n_invalid
    FROM staging_geometry
    WHERE batch_id = :batch AND (ST_IsEmpty(geom) OR NOT ST_IsValid(geom))
    ORDER BY feature_key
    LIMIT :limit
    """
)
DATASET_WARNINGS_SQL = text(
    """
    SELECT 'georef' AS kind, g.validation
    FROM georef_datasets g, jsonb_each_text(g.batches) AS layer(layer_id, batch_id)
    WHERE g.municipality_id = :m AND CAST(layer.batch_id AS bigint) = :batch
    UNION ALL
    SELECT 'zones', z.validation FROM zone_datasets z
    WHERE z.municipality_id = :m AND z.zones_batch_id = :batch
    UNION ALL
    SELECT 'cadastre', c.validation FROM cadastral_datasets c
    WHERE c.municipality_id = :m AND :batch IN (c.parcels_batch_id, c.ko_batch_id)
    """
)
STORE_SQL = text(
    """
    UPDATE geometry_batches SET qa_status = :status, qa_issues = CAST(:issues AS jsonb)
    WHERE id = :batch
    """
)
STAGED_SQL = text(
    """
    SELECT id FROM geometry_batches
    WHERE municipality_id = :m AND status = 'staged' ORDER BY id
    """
)


# --- checks ---------------------------------------------------------------------------------------


async def _validity(session: AsyncSession, batch_id: int) -> list[Issue]:
    rows = (
        (await session.execute(VALIDITY_SQL, {"batch": batch_id, "limit": MAX_LISTED}))
        .mappings()
        .all()
    )
    if not rows:
        return []
    n_invalid, n_empty = int(rows[0]["n_invalid"]), int(rows[0]["n_empty"])
    invalid = [r for r in rows if not r["empty"]]
    issues: list[Issue] = []
    if n_invalid:
        first = invalid[0] if invalid else None
        example = f", e.g. {first['feature_key']}: {first['reason']}" if first else ""
        issues.append(
            Issue(
                "invalid_geometry",
                "error",
                f"{n_invalid} feature(s) have an invalid geometry{example}",
                count=n_invalid,
                features=tuple(r["feature_key"] for r in invalid),
                keys=tuple(r["feature_key"] for r in invalid),
            )
        )
    if n_empty:
        issues.append(
            Issue(
                "empty_geometry",
                "error",
                f"{n_empty} feature(s) have no geometry",
                count=n_empty,
                features=tuple(r["feature_key"] for r in rows if r["empty"]),
                keys=tuple(r["feature_key"] for r in rows if r["empty"]),
            )
        )
    return issues


def dataset_issues(kind: str, validation: Mapping[str, Any] | None, layer_id: str) -> list[Issue]:
    """The producing dataset's warnings that concern this batch's layer."""
    if not validation:
        return []
    if kind == "zones":
        warnings = [p for p in validation.get("problems", []) if p.get("severity") == "warning"]
    else:
        warnings = list(validation.get("warnings", []))
    issues: list[Issue] = []
    for w in warnings:
        code = str(w.get("code") or "warning")
        layers = DATASET_WARNING_LAYERS.get(code)
        if layers is not None and layer_id not in layers:
            continue
        if kind == "cadastre" and layer_id != "cadastral_parcels":
            continue
        issues.append(
            Issue(
                f"{kind}.{code}",
                "warning",
                str(w.get("message") or code),
                count=int(w.get("count") or 1),
                area_m2=w.get("area_m2"),
            )
        )
    return issues


async def check_batch(
    session: AsyncSession,
    batch_id: int,
    *,
    municipality_id: str,
) -> QaResult:
    """Every check of the batch's layer; nothing is written."""
    batch = (await session.execute(BATCH_SQL, {"batch": batch_id})).mappings().first()
    if batch is None:
        raise LookupError(f"no geometry batch {batch_id}")
    layer_id = batch["layer_id"]
    checks = LAYER_CHECKS.get(layer_id, frozenset({"validity"}))
    issues: list[Issue] = []
    if "validity" in checks:
        issues += await _validity(session, batch_id)
    for row in (
        (await session.execute(DATASET_WARNINGS_SQL, {"m": municipality_id, "batch": batch_id}))
        .mappings()
        .all()
    ):
        issues += dataset_issues(row["kind"], row["validation"], layer_id)
    ordered = tuple(c for c in ("validity",) if c in checks)
    return QaResult(status_of(issues), issues, ordered)


async def run_batch_qa(
    session: AsyncSession,
    batch_id: int,
    *,
    municipality_id: str,
) -> QaResult:
    """Check the batch and store the outcome on it (the caller commits)."""
    result = await check_batch(session, batch_id, municipality_id=municipality_id)
    await session.execute(
        STORE_SQL,
        {
            "batch": batch_id,
            "status": result.status,
            "issues": json.dumps([i.as_json() for i in result.issues], ensure_ascii=False),
        },
    )
    return result


async def run_batches_qa(
    session: AsyncSession, batch_ids: Sequence[int], *, municipality_id: str
) -> dict[int, QaResult]:
    return {
        int(b): await run_batch_qa(session, int(b), municipality_id=municipality_id)
        for b in batch_ids
    }


# --- CLI ------------------------------------------------------------------------------------------


async def _cli(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from core.config import get_settings

    url = args.url or get_settings().database_url
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as session:
            if args.batch:
                ids = list(args.batch)
            else:
                ids = [r[0] for r in (await session.execute(STAGED_SQL, {"m": args.municipality}))]
            out: dict[str, Any] = {}
            for batch_id in ids:
                if args.command == "recheck":
                    result = await run_batch_qa(
                        session, batch_id, municipality_id=args.municipality
                    )
                else:
                    result = await check_batch(session, batch_id, municipality_id=args.municipality)
                out[str(batch_id)] = result.as_json()
            if args.command == "recheck":
                await session.commit()
    finally:
        await engine.dispose()
    if args.json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
    else:
        if not out:
            print("no staged geometry batches")
        for batch_id, result in out.items():
            print(f"batch {batch_id}: {result['status']} ({', '.join(result['checks'])})")
            for issue in result["issues"]:
                print(f"  {issue['severity']:7} {issue['code']}: {issue['message']}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.geometry_qa",
        description=(
            "Validity QA of staged geometry batches (invalid or empty features, plus the "
            "producing run's own warnings)."
        ),
    )
    parser.add_argument(
        "command", choices=("check", "recheck"), help="check: print; recheck: store the outcome"
    )
    parser.add_argument("--batch", type=int, action="append", help="a batch id (repeatable)")
    parser.add_argument("--municipality", default="podgorica")
    parser.add_argument("--url", help="SQLAlchemy URL (default: settings)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(_cli(args))


if __name__ == "__main__":
    sys.exit(main())
