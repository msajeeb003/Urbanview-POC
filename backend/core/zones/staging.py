"""A validated zone dataset into staging (``import``), and from staging onto the serving tables
(the publish job's ``geometry`` step).

Import (:func:`stage_dataset`): the zones become one ``zones`` batch in ``staging_geometry``,
reprojected from the editing CRS to EPSG:4326 in PostGIS (``ST_Transform``; ``ST_MakeValid`` +
polygon extraction as the publish job does), keyed by ``zone_key``; the documents become
``staging_zone_documents`` rows, each matched to a planning document already registered (through the
admin API or an earlier dataset) so nothing is duplicated; a ``zone_datasets`` row records the
``dataset_version``, the checksums of the inputs and the validation report. A newer import
supersedes the staged one (and its batch) instead of stacking up.

Publish (:func:`apply_zone_datasets`): after the job upserted the zones of a dataset's batch, its
documents update the matched ``planning_documents`` (zone, status, type, source, registry id; the
registered name and files are left alone) or insert new ones (no file, no coverage: an uncovered
document until its PDF and geometry arrive). Matching runs again at publish time, so a document
registered between import and publish is still found. Only adopted, live documents with a coverage
geometry define coverage, so in-progress and superseded rows are recorded without covering anything.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.zones.schema import (
    DOCUMENT_FIELD_NAMES,
    ZONE_FIELD_NAMES,
    ZONE_TYPE_BY_CODE,
    ZONE_TYPE_BY_VALUE,
    name_key,
)

if TYPE_CHECKING:
    from core.zones.validate import ValidationReport, ZoneDataset

REVIEW_AIDS = (
    "listed_as",
    "listed_year",
    "eregistri_name",
    "eregistri_code",
    "eregistri_gazette",
    "eregistri_note",
    "match",
)
_DETAILS_ID = re.compile(r"/PlanningDocument/Details/(\d+)", re.IGNORECASE)


# --- matching registered documents --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExistingDocument:
    id: int
    name: str
    eregistri_reference: str | None
    source_url: str | None


def eregistri_id_from_url(url: str | None) -> str | None:
    match = _DETAILS_ID.search(url or "")
    return match.group(1) if match else None


def match_existing(
    rows: Sequence[Mapping[str, Any]], existing: Sequence[ExistingDocument]
) -> list[tuple[int | None, str]]:
    """(document id or None, method) per row: the registry id first, then a registry link in the
    registered document's source URL, then the folded name; each registered document is taken
    once. ``new`` = no registered document matches."""
    by_ref: dict[str, list[ExistingDocument]] = {}
    by_url: dict[str, list[ExistingDocument]] = {}
    by_name: dict[str, list[ExistingDocument]] = {}
    for doc in existing:
        if doc.eregistri_reference:
            by_ref.setdefault(doc.eregistri_reference.strip().lower(), []).append(doc)
        if url_id := eregistri_id_from_url(doc.source_url):
            by_url.setdefault(url_id, []).append(doc)
        by_name.setdefault(name_key(doc.name), []).append(doc)
    used: set[int] = set()

    def take(candidates: list[ExistingDocument] | None) -> ExistingDocument | None:
        for doc in candidates or []:
            if doc.id not in used:
                used.add(doc.id)
                return doc
        return None

    out: list[tuple[int | None, str]] = []
    for row in rows:
        ref = str(row.get("eregistri_reference") or "").strip().lower()
        doc = None
        method = "new"
        if ref and (doc := take(by_ref.get(ref))):
            method = "eregistri"
        elif ref and (doc := take(by_url.get(ref))):
            method = "source_url"
        elif doc := take(by_name.get(name_key(str(row.get("document_name") or "")))):
            method = "name"
        out.append((doc.id if doc else None, method))
    return out


EXISTING_SQL = text(
    """
    SELECT id, name, eregistri_reference, source_url FROM planning_documents
    WHERE municipality_id = :m AND is_current_version ORDER BY id
    """
)


async def existing_documents(session: AsyncSession, municipality_id: str) -> list[ExistingDocument]:
    rows = (await session.execute(EXISTING_SQL, {"m": municipality_id})).mappings().all()
    return [
        ExistingDocument(r["id"], r["name"], r["eregistri_reference"], r["source_url"])
        for r in rows
    ]


# --- import -------------------------------------------------------------------------------------


@dataclass(slots=True)
class StageResult:
    dataset_id: int
    batch_id: int
    dataset_version: str
    zones: int
    documents: int
    superseded: list[str] = field(default_factory=list)
    matches: dict[str, int] = field(default_factory=dict)


SUPERSEDE_SQL = text(
    """
    UPDATE zone_datasets SET status = 'superseded'
    WHERE municipality_id = :m AND status = 'staged'
    RETURNING dataset_version, zones_batch_id
    """
)
SUPERSEDE_BATCH_SQL = text(
    "UPDATE geometry_batches SET status = 'superseded' WHERE id = :id AND status = 'staged'"
)
BATCH_SQL = text(
    """
    INSERT INTO geometry_batches (municipality_id, layer_id, status, feature_count, produced_by,
                                  qa_report)
    VALUES (:m, 'zones', 'staged', :n, :by, CAST(:qa AS jsonb))
    RETURNING id
    """
)
ZONE_SQL = text(
    """
    INSERT INTO staging_geometry (municipality_id, batch_id, layer_id, feature_key, geom,
                                  properties)
    VALUES (:m, :batch, 'zones', :key,
            ST_Multi(ST_CollectionExtract(ST_MakeValid(
                ST_Transform(ST_SetSRID(ST_GeomFromWKB(CAST(:wkb AS bytea)), :srid), 4326)), 3)),
            CAST(:props AS jsonb))
    """
)
DATASET_SQL = text(
    """
    INSERT INTO zone_datasets (municipality_id, dataset_version, status, zones_batch_id,
                               editing_crs, zone_count, document_count, sources, validation,
                               imported_by)
    VALUES (:m, :label, 'staged', :batch, :srid, :zones, :documents, CAST(:sources AS jsonb),
            CAST(:validation AS jsonb), :by)
    RETURNING id
    """
)
DOCUMENT_SQL = text(
    """
    INSERT INTO staging_zone_documents (municipality_id, dataset_id, row_number, zone_key,
        document_name, document_type, status, eregistri_reference, source_url, adoption_date,
        notes, poc_coverage, confirmed, review, matched_document_id, match_method)
    VALUES (:m, :dataset, :row, :zone, :name, :type, :status, :ref, :url, :adopted, :notes,
            :poc, :confirmed, CAST(:review AS jsonb), :matched, :method)
    """
)
LABEL_TAKEN_SQL = text(
    "SELECT 1 FROM zone_datasets WHERE municipality_id = :m AND dataset_version = :label"
)
LABELS_LIKE_SQL = text(
    "SELECT count(*) FROM zone_datasets WHERE municipality_id = :m AND dataset_version LIKE :p"
)


def _blank(value: Any) -> Any:
    return None if value == "" else value


def zone_properties(zone: Any) -> dict[str, Any]:
    zone_type = ZONE_TYPE_BY_VALUE.get(zone.zone_type or "")
    return {
        "zone_key": zone.zone_id,
        "name": zone.name,
        "zone_type": zone_type.code if zone_type else None,
        "general_planning_summary": _blank(zone.general_planning_summary),
        "notes": _blank(zone.notes),
        "no_adopted_plan": bool(zone.no_adopted_plan),
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


async def next_label(session: AsyncSession, municipality_id: str, prefix: str, today: date) -> str:
    stem = f"{prefix}-{today:%Y%m%d}"
    count = (
        await session.execute(LABELS_LIKE_SQL, {"m": municipality_id, "p": f"{stem}%"})
    ).scalar()
    return f"{stem}-{int(count or 0) + 1}"


async def stage_dataset(
    session: AsyncSession,
    *,
    municipality_id: str,
    dataset: ZoneDataset,
    validation: ValidationReport,
    label: str,
    imported_by: str,
    sources: Mapping[str, Any],
) -> StageResult:
    """Stage a validated dataset (the caller refuses one with errors). Does not commit."""
    import shapely  # the gis extra: the API image imports this module (via the publish
    # pipeline) without it, and only the import CLI stages geometry

    if (await session.execute(LABEL_TAKEN_SQL, {"m": municipality_id, "label": label})).first():
        raise ValueError(f"dataset version {label!r} exists already")
    superseded = (await session.execute(SUPERSEDE_SQL, {"m": municipality_id})).all()
    for _, batch_id in superseded:
        if batch_id is not None:
            await session.execute(SUPERSEDE_BATCH_SQL, {"id": batch_id})
    zones = [z for z in dataset.zones if z.geometry is not None and not z.geometry.is_empty]
    qa = {"dataset_version": label, **validation.to_json()}
    batch_id = (
        await session.execute(
            BATCH_SQL,
            {
                "m": municipality_id,
                "n": len(zones),
                "by": f"import_zones:{imported_by}",
                "qa": _json(qa),
            },
        )
    ).scalar_one()
    for zone in zones:
        await session.execute(
            ZONE_SQL,
            {
                "m": municipality_id,
                "batch": batch_id,
                "key": zone.zone_id,
                "wkb": shapely.to_wkb(zone.geometry, output_dimension=2),
                "srid": dataset.srs_id,
                "props": _json(zone_properties(zone)),
            },
        )
    dataset_id = (
        await session.execute(
            DATASET_SQL,
            {
                "m": municipality_id,
                "label": label,
                "batch": batch_id,
                "srid": dataset.srs_id,
                "zones": len(zones),
                "documents": len(dataset.documents),
                "sources": _json(dict(sources)),
                "validation": _json(validation.to_json()),
                "by": imported_by,
            },
        )
    ).scalar_one()
    rows = [d.values for d in dataset.documents]
    matches = match_existing(rows, await existing_documents(session, municipality_id))
    for record, (doc_id, method) in zip(dataset.documents, matches, strict=True):
        v = record.values
        await session.execute(
            DOCUMENT_SQL,
            {
                "m": municipality_id,
                "dataset": dataset_id,
                "row": record.row,
                "zone": v.get("zone_id"),
                "name": v.get("document_name"),
                "type": v.get("document_type"),
                "status": v.get("status"),
                "ref": _blank(v.get("eregistri_reference")),
                "url": _blank(v.get("source_url")),
                "adopted": v.get("adoption_date"),
                "notes": _blank(v.get("notes")),
                "poc": bool(v.get("poc_coverage")),
                "confirmed": bool(v.get("confirmed")),
                "review": _json({k: v.get(k) for k in REVIEW_AIDS if v.get(k) not in (None, "")}),
                "matched": doc_id,
                "method": method,
            },
        )
    return StageResult(
        dataset_id=dataset_id,
        batch_id=batch_id,
        dataset_version=label,
        zones=len(zones),
        documents=len(dataset.documents),
        superseded=[row[0] for row in superseded],
        matches=dict(Counter(method for _, method in matches)),
    )


# --- the checked-in exports ---------------------------------------------------------------------

EXPORT_SQL = text(
    """
    SELECT feature_key, properties, ST_AsGeoJSON(geom, 7) AS geometry
    FROM staging_geometry WHERE batch_id = :batch ORDER BY feature_key
    """
)


async def export_geojson(session: AsyncSession, batch_id: int, path: Path, *, name: str) -> int:
    """The staged zones as GeoJSON in EPSG:4326 (the QGIS field values, one feature per line so
    a change reads as a small diff)."""
    rows = (await session.execute(EXPORT_SQL, {"batch": batch_id})).mappings().all()
    features = []
    for row in rows:
        props = dict(row["properties"])
        zone_type = ZONE_TYPE_BY_CODE.get(props.get("zone_type") or "")
        values = {
            "zone_id": props.get("zone_key"),
            "name": props.get("name"),
            "zone_type": zone_type.value if zone_type else None,
            "general_planning_summary": props.get("general_planning_summary"),
            "notes": props.get("notes"),
            "no_adopted_plan": bool(props.get("no_adopted_plan")),
        }
        assert tuple(values) == ZONE_FIELD_NAMES
        features.append(
            json.dumps(
                {"type": "Feature", "properties": values, "geometry": json.loads(row["geometry"])},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    head = json.dumps(
        {
            "type": "FeatureCollection",
            "name": name,
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}},
        },
        ensure_ascii=False,
    )[:-1]
    body = ",\n".join(features)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{head}, "features": [\n{body}\n]}}\n', encoding="utf-8", newline="\n")
    return len(features)


def write_documents_csv(dataset: ZoneDataset, path: Path) -> int:
    """The imported document list, normalised (the confirmed list that is checked in)."""
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(DOCUMENT_FIELD_NAMES), extrasaction="ignore")
        writer.writeheader()
        for record in sorted(
            dataset.documents, key=lambda d: (str(d.values.get("zone_id")), d.row)
        ):
            row = {}
            for name in DOCUMENT_FIELD_NAMES:
                value = record.values.get(name)
                if isinstance(value, bool):
                    value = "true" if value else "false"
                elif isinstance(value, date):
                    value = value.isoformat()
                row[name] = "" if value is None else value
            writer.writerow(row)
    return len(dataset.documents)


# --- publish --------------------------------------------------------------------------------------

DATASETS_FOR_BATCHES_SQL = text(
    """
    SELECT id, dataset_version FROM zone_datasets
    WHERE municipality_id = :m AND status = 'staged' AND zones_batch_id = ANY(:batches)
    ORDER BY id
    """
)
STAGED_DOCUMENTS_SQL = text(
    """
    SELECT id, row_number, zone_key, document_name, document_type, status, eregistri_reference,
           source_url, adoption_date
    FROM staging_zone_documents WHERE dataset_id = :d ORDER BY row_number
    """
)
ZONE_IDS_SQL = text(
    "SELECT zone_key, id FROM zones WHERE municipality_id = :m AND zone_key = ANY(:keys)"
)
UPDATE_DOCUMENT_SQL = text(
    """
    UPDATE planning_documents
    SET zone_id = :zone, status = CAST(:status AS planning_document_status), type = :type,
        source = :source, source_url = COALESCE(:url, source_url),
        eregistri_reference = COALESCE(:ref, eregistri_reference),
        adopted_on = COALESCE(:adopted, adopted_on), dataset_version = :label
    WHERE id = :id
    """
)
INSERT_DOCUMENT_SQL = text(
    """
    INSERT INTO planning_documents (municipality_id, name, type, status, source, source_url,
        zone_id, eregistri_reference, adopted_on, dataset_version, registered_by, registered_at,
        version, is_current_version, coverage_live)
    VALUES (:m, :name, :type, CAST(:status AS planning_document_status), :source, :url, :zone,
            :ref, :adopted, :label, :by, now(), 1, true, false)
    RETURNING id
    """
)
APPLIED_ROW_SQL = text(
    """
    UPDATE staging_zone_documents
    SET applied_document_id = :doc, matched_document_id = :matched, match_method = :method
    WHERE id = :id
    """
)
DATASET_PUBLISHED_SQL = text(
    """
    UPDATE zone_datasets SET status = 'published', published_version_id = :v, published_at = now()
    WHERE id = :id
    """
)


async def apply_zone_datasets(
    session: AsyncSession,
    *,
    municipality_id: str,
    version_id: int,
    published_batch_ids: Collection[int],
) -> dict[str, int]:
    """Apply the documents of the zone datasets whose zones batch this publish run applied."""
    counts = Counter({"zone_datasets": 0, "documents_updated": 0, "documents_created": 0})
    if not published_batch_ids:
        return dict(counts)
    datasets = (
        (
            await session.execute(
                DATASETS_FOR_BATCHES_SQL,
                {"m": municipality_id, "batches": list(published_batch_ids)},
            )
        )
        .mappings()
        .all()
    )
    for dataset in datasets:
        rows = (await session.execute(STAGED_DOCUMENTS_SQL, {"d": dataset["id"]})).mappings().all()
        keys = sorted({r["zone_key"] for r in rows})
        zone_ids = dict(
            (await session.execute(ZONE_IDS_SQL, {"m": municipality_id, "keys": keys})).all()
        )
        matches = match_existing(rows, await existing_documents(session, municipality_id))
        label = dataset["dataset_version"]
        for row, (doc_id, method) in zip(rows, matches, strict=True):
            params = {
                "zone": zone_ids.get(row["zone_key"]),
                "status": row["status"],
                "type": row["document_type"],
                "source": "eRegistri" if row["eregistri_reference"] else f"zone list {label}",
                "url": row["source_url"],
                "ref": row["eregistri_reference"],
                "adopted": row["adoption_date"],
                "label": label,
            }
            if doc_id is not None:
                await session.execute(UPDATE_DOCUMENT_SQL, {**params, "id": doc_id})
                applied = doc_id
                counts["documents_updated"] += 1
            else:
                applied = (
                    await session.execute(
                        INSERT_DOCUMENT_SQL,
                        {
                            **params,
                            "m": municipality_id,
                            "name": row["document_name"],
                            "by": f"zone_dataset:{label}",
                        },
                    )
                ).scalar_one()
                counts["documents_created"] += 1
            await session.execute(
                APPLIED_ROW_SQL,
                {"doc": applied, "matched": doc_id, "method": method, "id": row["id"]},
            )
        await session.execute(DATASET_PUBLISHED_SQL, {"v": version_id, "id": dataset["id"]})
        counts["zone_datasets"] += 1
    return dict(counts)
