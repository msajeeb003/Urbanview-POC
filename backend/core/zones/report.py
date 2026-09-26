"""The zone and planning-document list with parcel counts: the BRD's last gate before estimation.

For one zone dataset (the latest staged or published one by default): per zone its type, area,
documents (all, adopted, in progress, superseded, POC coverage, matched to registered documents vs
new) and the cadastral and planned urban parcels whose point on surface falls inside it (the rule
the zone panel counts with), all computed in PostGIS from the staged zones (EPSG:4326). Totals add
the parcels outside every zone, zones already in the database that the dataset does not name, and
registered documents the list does not mention, so the list the client signs is complete.

Written as ``zones.csv`` and ``documents.csv`` (UTF-8 with BOM, for Excel), ``report.md`` (for
the client's sign-off) and ``report.json`` into
``data/zones/<municipality>/reports/<dataset_version>/``.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.zones.schema import STATUS_LABELS, ZONE_TYPE_BY_CODE


@dataclass(slots=True)
class ZoneRow:
    zone_id: str
    name: str
    zone_type: str  # label, "" when not classified
    no_adopted_plan: bool
    area_km2: float
    documents: int = 0
    adopted: int = 0
    in_progress: int = 0
    superseded: int = 0
    poc_documents: int = 0
    registered_matches: int = 0
    new_documents: int = 0
    cadastral_parcels: int = 0
    urban_parcels: int = 0


@dataclass(slots=True)
class ZoneReport:
    municipality_id: str
    dataset_version: str
    status: str
    imported_by: str | None
    created_at: datetime | None
    published_at: datetime | None
    zones: list[ZoneRow]
    documents: list[dict[str, Any]]
    totals: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "municipality_id": self.municipality_id,
            "dataset_version": self.dataset_version,
            "status": self.status,
            "imported_by": self.imported_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "zones": [asdict(z) for z in self.zones],
            "documents": self.documents,
            "totals": self.totals,
            "warnings": self.warnings,
        }


DATASET_SQL = text(
    """
    SELECT id, dataset_version, status, zones_batch_id, imported_by, created_at, published_at,
           validation
    FROM zone_datasets
    WHERE municipality_id = :m AND (CAST(:label AS text) IS NULL OR dataset_version = :label)
      AND status IN ('staged', 'published')
    ORDER BY (CAST(:label AS text) IS NOT NULL) DESC, id DESC
    LIMIT 1
    """
)
ZONES_SQL = text(
    """
    SELECT s.feature_key AS zone_id, s.properties->>'name' AS name,
           s.properties->>'zone_type' AS zone_type,
           COALESCE(CAST(s.properties->>'no_adopted_plan' AS boolean), false) AS no_adopted_plan,
           round(CAST(ST_Area(CAST(s.geom AS geography)) / 1e6 AS numeric), 3) AS area_km2,
           (SELECT count(*) FROM cadastral_parcels c
            WHERE c.municipality_id = s.municipality_id AND c.geom && s.geom
              AND ST_Contains(s.geom, ST_PointOnSurface(c.geom))) AS cadastral_parcels,
           (SELECT count(*) FROM urban_parcels u
            WHERE u.municipality_id = s.municipality_id AND u.geom && s.geom
              AND ST_Contains(s.geom, ST_PointOnSurface(u.geom))) AS urban_parcels
    FROM staging_geometry s WHERE s.batch_id = :batch ORDER BY s.properties->>'name'
    """
)
PARCEL_TOTALS_SQL = text(
    """
    SELECT
      (SELECT count(*) FROM cadastral_parcels c WHERE c.municipality_id = :m) AS cadastral_total,
      (SELECT count(*) FROM cadastral_parcels c WHERE c.municipality_id = :m AND NOT EXISTS (
          SELECT 1 FROM staging_geometry s WHERE s.batch_id = :batch AND s.geom && c.geom
            AND ST_Contains(s.geom, ST_PointOnSurface(c.geom)))) AS cadastral_outside,
      (SELECT count(*) FROM urban_parcels u WHERE u.municipality_id = :m) AS urban_total,
      (SELECT count(*) FROM urban_parcels u WHERE u.municipality_id = :m AND NOT EXISTS (
          SELECT 1 FROM staging_geometry s WHERE s.batch_id = :batch AND s.geom && u.geom
            AND ST_Contains(s.geom, ST_PointOnSurface(u.geom)))) AS urban_outside
    """
)
DOCUMENTS_SQL = text(
    """
    SELECT d.row_number, d.zone_key, d.document_name, d.document_type, d.status,
           d.eregistri_reference, d.source_url, d.adoption_date, d.poc_coverage, d.confirmed,
           d.notes, d.match_method, d.matched_document_id, d.applied_document_id,
           p.name AS registered_name
    FROM staging_zone_documents d
    LEFT JOIN planning_documents p ON p.id = COALESCE(d.applied_document_id, d.matched_document_id)
    WHERE d.dataset_id = :d ORDER BY d.zone_key, d.row_number
    """
)
OTHER_ZONES_SQL = text(
    """
    SELECT z.id, z.name, z.zone_key FROM zones z
    WHERE z.municipality_id = :m
      AND NOT EXISTS (SELECT 1 FROM staging_geometry s WHERE s.batch_id = :batch
                        AND (s.feature_key = z.zone_key
                             OR (z.zone_key IS NULL AND s.properties->>'name' = z.name)))
    ORDER BY z.name
    """
)
UNLISTED_DOCUMENTS_SQL = text(
    """
    SELECT p.id, p.name, p.type, p.status FROM planning_documents p
    WHERE p.municipality_id = :m AND p.is_current_version
      AND NOT EXISTS (SELECT 1 FROM staging_zone_documents d WHERE d.dataset_id = :d
                        AND p.id IN (d.matched_document_id, d.applied_document_id))
    ORDER BY p.name
    """
)


def _type_label(code: str | None) -> str:
    t = ZONE_TYPE_BY_CODE.get(code or "")
    return t.label_en if t else ""


async def build_report(
    session: AsyncSession, municipality_id: str, *, dataset_version: str | None = None
) -> ZoneReport:
    ds = (
        (await session.execute(DATASET_SQL, {"m": municipality_id, "label": dataset_version}))
        .mappings()
        .first()
    )
    if ds is None:
        wanted = f" {dataset_version!r}" if dataset_version else ""
        raise LookupError(f"no staged or published zone dataset{wanted} for {municipality_id}")
    batch = ds["zones_batch_id"]
    zones = {
        r["zone_id"]: ZoneRow(
            zone_id=r["zone_id"],
            name=r["name"],
            zone_type=_type_label(r["zone_type"]),
            no_adopted_plan=bool(r["no_adopted_plan"]),
            area_km2=float(r["area_km2"] or 0),
            cadastral_parcels=int(r["cadastral_parcels"]),
            urban_parcels=int(r["urban_parcels"]),
        )
        for r in (await session.execute(ZONES_SQL, {"batch": batch})).mappings()
    }
    documents: list[dict[str, Any]] = []
    for r in (await session.execute(DOCUMENTS_SQL, {"d": ds["id"]})).mappings():
        row = dict(r)
        zone = zones.get(row["zone_key"])
        if zone is not None:
            zone.documents += 1
            status = row["status"]
            if status == "adopted":
                zone.adopted += 1
            elif status == "in_progress":
                zone.in_progress += 1
            elif status == "superseded":
                zone.superseded += 1
            zone.poc_documents += bool(row["poc_coverage"])
            if row["match_method"] in ("eregistri", "source_url", "name"):
                zone.registered_matches += 1
            else:
                zone.new_documents += 1
        row["zone_name"] = zone.name if zone else ""
        row["adoption_date"] = row["adoption_date"].isoformat() if row["adoption_date"] else ""
        documents.append(row)
    parcels = (
        (await session.execute(PARCEL_TOTALS_SQL, {"m": municipality_id, "batch": batch}))
        .mappings()
        .one()
    )
    other_zones = (
        (await session.execute(OTHER_ZONES_SQL, {"m": municipality_id, "batch": batch}))
        .mappings()
        .all()
    )
    unlisted = (
        (await session.execute(UNLISTED_DOCUMENTS_SQL, {"m": municipality_id, "d": ds["id"]}))
        .mappings()
        .all()
    )
    rows = sorted(zones.values(), key=lambda z: z.name.lower())
    by_status: dict[str, int] = defaultdict(int)
    for d in documents:
        by_status[d["status"]] += 1
    totals = {
        "zones": len(rows),
        "documents": len(documents),
        "documents_by_status": dict(by_status),
        "poc_documents": sum(z.poc_documents for z in rows),
        "confirmed_documents": sum(1 for d in documents if d["confirmed"]),
        "registered_matches": sum(z.registered_matches for z in rows),
        "new_documents": sum(z.new_documents for z in rows),
        "area_km2": round(sum(z.area_km2 for z in rows), 3),
        **{k: int(v) for k, v in parcels.items()},
    }
    warnings = []
    if parcels["cadastral_outside"]:
        warnings.append(
            f"{parcels['cadastral_outside']} cadastral parcel(s) lie outside every zone"
        )
    for z in rows:
        if z.adopted == 0 and not z.no_adopted_plan:
            warnings.append(f"zone {z.zone_id} has no adopted document")
    if other_zones:
        names = ", ".join(r["name"] for r in other_zones)
        warnings.append(
            f"{len(other_zones)} zone(s) in the database are not in this dataset and stay until "
            f"retired: {names}"
        )
    if unlisted:
        names = "; ".join(f"{r['name']} ({r['type']}, {r['status']})" for r in unlisted[:20])
        more = f" and {len(unlisted) - 20} more" if len(unlisted) > 20 else ""
        warnings.append(
            f"{len(unlisted)} registered planning document(s) are not on the list: {names}{more}"
        )
    unconfirmed = totals["documents"] - totals["confirmed_documents"]
    if unconfirmed:
        warnings.append(f"{unconfirmed} document row(s) are not marked confirmed")
    return ZoneReport(
        municipality_id=municipality_id,
        dataset_version=ds["dataset_version"],
        status=ds["status"],
        imported_by=ds["imported_by"],
        created_at=ds["created_at"],
        published_at=ds["published_at"],
        zones=rows,
        documents=documents,
        totals=totals,
        warnings=warnings,
        validation=ds["validation"] or {},
    )


# --- files ----------------------------------------------------------------------------------------

ZONE_COLUMNS = (
    ("zone_id", "Zone ID"),
    ("name", "Zone"),
    ("zone_type", "Type"),
    ("area_km2", "Area km²"),
    ("documents", "Documents"),
    ("adopted", "Adopted"),
    ("in_progress", "In progress"),
    ("superseded", "Superseded"),
    ("poc_documents", "POC coverage"),
    ("cadastral_parcels", "Cadastral parcels"),
    ("urban_parcels", "Urban parcels"),
    ("no_adopted_plan", "No adopted plan"),
)
DOCUMENT_COLUMNS = (
    ("zone_key", "Zone ID"),
    ("zone_name", "Zone"),
    ("document_type", "Type"),
    ("document_name", "Document"),
    ("status", "Status"),
    ("eregistri_reference", "eRegistri id"),
    ("adoption_date", "Adopted on"),
    ("poc_coverage", "POC coverage"),
    ("confirmed", "Confirmed"),
    ("match_method", "Registered match"),
    ("registered_name", "Registered as"),
    ("source_url", "Source"),
    ("notes", "Notes"),
)


def _cell(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return "" if value is None else str(value)


def _md(value: Any) -> str:
    return _cell(value).replace("|", "/").replace("\n", " ")


def render_markdown(report: ZoneReport, *, title: str) -> str:
    t = report.totals
    status_counts = ", ".join(
        f"{n} {STATUS_LABELS.get(s, s).split(' (')[0].lower()}"
        for s, n in sorted(t["documents_by_status"].items())
    )
    lines = [
        f"# {title}: zones and planning documents",
        "",
        f"Dataset **{report.dataset_version}** ({report.status}"
        f"{', published ' + report.published_at.date().isoformat() if report.published_at else ''})"
        f", imported by {report.imported_by or 'n/a'}"
        f"{' on ' + report.created_at.date().isoformat() if report.created_at else ''}.",
        "",
        "The definitive list of UrbanView zones for this municipality, the planning documents "
        "each zone groups, and the cadastral and planned urban parcels inside each zone (a parcel "
        "counts in the zone that contains its point on surface). Only adopted documents define "
        "coverage; documents in progress or superseded are recorded but cover nothing.",
        "",
        f"- Zones: **{t['zones']}** covering {t['area_km2']} km²",
        f"- Documents: **{t['documents']}** ({status_counts}); {t['poc_documents']} with POC "
        f"parameter coverage; {t['confirmed_documents']} confirmed",
        f"- Cadastral parcels: {t['cadastral_total']} "
        f"({t['cadastral_outside']} outside every zone)",
        f"- Planned urban parcels: {t['urban_total']} ({t['urban_outside']} outside every zone)",
        "",
        "## Zones",
        "",
        "| " + " | ".join(label for _, label in ZONE_COLUMNS) + " |",
        "|" + "---|" * len(ZONE_COLUMNS),
    ]
    for z in report.zones:
        values = asdict(z)
        lines.append("| " + " | ".join(_md(values[k]) for k, _ in ZONE_COLUMNS) + " |")
    lines.append(
        f"| **Total** | | | {t['area_km2']} | {t['documents']} | "
        f"{t['documents_by_status'].get('adopted', 0)} | "
        f"{t['documents_by_status'].get('in_progress', 0)} | "
        f"{t['documents_by_status'].get('superseded', 0)} | {t['poc_documents']} | "
        f"{t['cadastral_total'] - t['cadastral_outside']} | "
        f"{t['urban_total'] - t['urban_outside']} | |"
    )
    lines += ["", "## Documents by zone", ""]
    by_zone: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in report.documents:
        by_zone[d["zone_key"]].append(d)
    for z in report.zones:
        lines += [f"### {z.name} (`{z.zone_id}`)", ""]
        docs = by_zone.get(z.zone_id, [])
        if not docs:
            lines += [
                "No documents" + (" (marked: no adopted plan)." if z.no_adopted_plan else "."),
                "",
            ]
            continue
        lines += [
            "| Type | Document | Status | eRegistri | POC | Confirmed |",
            "|---|---|---|---|---|---|",
        ]
        for d in docs:
            lines.append(
                f"| {_md(d['document_type'])} | {_md(d['document_name'])} | {_md(d['status'])} | "
                f"{_md(d['eregistri_reference'])} | {_md(d['poc_coverage'])} | "
                f"{_md(d['confirmed'])} |"
            )
        lines.append("")
    if report.warnings:
        lines += ["## Open points", ""] + [f"- {w}" for w in report.warnings] + [""]
    problems = report.validation.get("problems") or []
    warnings = [p for p in problems if p.get("severity") == "warning"]
    if warnings:
        lines += [f"## Validation warnings ({len(warnings)})", ""]
        lines += [f"- `{p.get('code')}`: {p.get('message')}" for p in warnings[:50]]
        if len(warnings) > 50:
            lines.append(f"- … and {len(warnings) - 50} more (report.json)")
        lines.append("")
    lines += [
        "## Sign-off",
        "",
        "We confirm the zones above, the planning documents listed for each zone with their "
        "status, and the POC coverage.",
        "",
        "| | Name | Signature | Date |",
        "|---|---|---|---|",
        "| Client | | | |",
        "| UrbanView | | | |",
        "",
    ]
    return "\n".join(lines)


def write_report(report: ZoneReport, out_dir: Path, *, title: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    zones_csv = out_dir / "zones.csv"
    with zones_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([label for _, label in ZONE_COLUMNS])
        for z in report.zones:
            values = asdict(z)
            writer.writerow([_cell(values[k]) for k, _ in ZONE_COLUMNS])
    paths.append(zones_csv)
    documents_csv = out_dir / "documents.csv"
    with documents_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([label for _, label in DOCUMENT_COLUMNS])
        for d in report.documents:
            writer.writerow([_cell(d.get(k)) for k, _ in DOCUMENT_COLUMNS])
    paths.append(documents_csv)
    md = out_dir / "report.md"
    md.write_text(render_markdown(report, title=title), encoding="utf-8", newline="\n")
    paths.append(md)
    js = out_dir / "report.json"
    js.write_text(
        json.dumps(report.to_json(), ensure_ascii=False, indent=1, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    paths.append(js)
    return paths
