"""Prepared planning values: a plan's parameter table loaded without the model.

For a document whose parameter table the table reader maps reliably (the corpus documents: the
column map of ``tests/corpus/corpus.toml``, the reading checked against the rendered pages), the
values are prepared once (``prepared build``: the gold set's stated planning values, each with
the box and the column name of the cell it is printed in, written to
``database/seeds/prepared_values/<municipality>/<id>.json``) and loaded into STAGING
(``prepared load``) as one extraction run of the reader ``table-reader``: an approved item per
value on its urban parcel, citing its page and cell. Nothing is served by the load: the publish
job copies approved items as it copies a reviewer's.

The load replaces what is open, like a newer run: the pending items of earlier runs over the same
file are superseded (never deleted), an older unpublished decision on the same target and field
is retired, a target that already holds the same approved value keeps its item, and a parcel the
document's geometry does not have is reported (no item). Loading the same data again changes
nothing.

BRD 2.6 lets input data be prepared by hand where that is faster. The items say what they are
(``extracted_by``, the reviewer and the note of the load) and the audit log holds one row per
load (``review.approve_prepared``): no expert reviewed them item by item. Later documents go
through the extraction job and the review queue.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.extraction.corpus import BACKEND, CorpusDocument, Gold
from core.extraction.fields import FIELD_SPECS, RULE_FIELDS
from core.extraction.normalise import Conventions, parcel_key
from core.extraction.preprocess import BBox, DocumentPages

PREPARED_FORMAT = 1
READER = "table-reader"
PLANNING_FIELDS: tuple[str, ...] = ("planned_parcel_area_m2", *RULE_FIELDS)
SEEDS_DIR = BACKEND.parent / "database" / "seeds" / "prepared_values"
ACTION = "review.approve_prepared"
NOTE = (
    "Prepared value: read from the plan's parameter table by the table reader (no model) and "
    "checked against the page image; loaded as approved, not reviewed item by item by an expert."
)
SessionFactory = async_sessionmaker[AsyncSession]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreparedValue(_M):
    value: float | str
    unit: str | None = None
    printed: str
    page: int
    bbox: BBox | None = None
    cell: str | None = None
    note: str | None = None


class PreparedParcel(_M):
    number: str
    key: str
    page: int
    block_ref: str | None = None
    values: dict[str, PreparedValue]


class PreparedSource(_M):
    file: str
    sha256: str
    pages: int


class PreparedDocument(_M):
    format: int = PREPARED_FORMAT
    municipality: str
    document: str
    name: str
    registry: str | None = None
    source: PreparedSource
    reader: str = READER
    preprocess_version: str
    prepared_on: str
    basis: str
    fields: list[str]
    parcels: list[PreparedParcel]

    @property
    def value_count(self) -> int:
        return sum(len(p.values) for p in self.parcels)


def prepared_path(municipality: str, document: str, root: Path = SEEDS_DIR) -> Path:
    return root / municipality / f"{document}.json"


def save_prepared(prepared: PreparedDocument, root: Path = SEEDS_DIR) -> Path:
    path = prepared_path(prepared.municipality, prepared.document, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prepared.model_dump_json(indent=1) + "\n", encoding="utf-8", newline="\n")
    return path


def read_prepared(path: Path) -> tuple[PreparedDocument, str]:
    """The data file and its checksum (what a load is remembered by)."""
    data = path.read_bytes()
    return PreparedDocument.model_validate_json(data), hashlib.sha256(data).hexdigest()


# --- build ----------------------------------------------------------------------------------------

_CELL = re.compile(r"^r\d+c(\d+)$")


def build_prepared(
    doc: CorpusDocument,
    gold: Gold,
    pages: DocumentPages,
    *,
    municipality: str,
    prepared_on: str | None = None,
) -> tuple[PreparedDocument, list[str]]:
    """The prepared values of a corpus document: the stated planning values of its gold set, each
    with the box and the column name of its cell in the PDF stage's grid. Also returns what does
    not line up (a cell missing from the grid or reading differently, a number field holding
    text): such a value keeps its place without a box, or is left out when it has no number."""
    tables = {(page.number, table.id): table for page in pages.pages for table in page.tables}
    problems: list[str] = []
    parcels: list[PreparedParcel] = []
    for parcel in gold.parcels:
        values: dict[str, PreparedValue] = {}
        for name in PLANNING_FIELDS:
            stated = parcel.values.get(name)
            if stated is None or stated.status != "stated" or stated.value is None:
                continue  # blank or deferred in the document: nothing to load
            where = f"{doc.id} p.{stated.page} {parcel.number} {name}"
            numeric = isinstance(stated.value, int | float) and not isinstance(stated.value, bool)
            if FIELD_SPECS[name].kind == "number" and not numeric:
                problems.append(f"{where}: {stated.printed!r} is not a number (left out)")
                continue
            bbox, column = None, None
            table = tables.get((stated.page, parcel.table)) if parcel.table else None
            found = _CELL.match(stated.cell or "")
            cell = (
                next((c for row in table.rows for c in row if c.id == stated.cell), None)
                if table is not None and found
                else None
            )
            if cell is None or found is None or table is None:
                problems.append(f"{where}: cell {stated.cell!r} is not in the page's grid (no box)")
            else:
                bbox = cell.bbox
                index = int(found.group(1))
                column = table.columns[index] if index < len(table.columns) else None
                if " ".join(cell.text.split()) != " ".join(stated.text.split()):
                    problems.append(f"{where}: the cell reads {cell.text!r}, not {stated.text!r}")
            values[name] = PreparedValue(
                value=float(stated.value) if numeric else str(stated.value),
                unit=stated.unit,
                printed=stated.printed,
                page=stated.page,
                bbox=bbox,
                cell=stated.cell,
                note=" – ".join(part for part in (parcel.number, column) if part) or None,
            )
        block = parcel.values.get("block_ref")
        parcels.append(
            PreparedParcel(
                number=parcel.number,
                key=parcel.key,
                page=parcel.page,
                block_ref=block.printed if block is not None else None,
                values=values,
            )
        )
    checked = "; ".join(f"{v.by} ({v.on}, {len(v.pages)} pages)" for v in gold.verification)
    prepared = PreparedDocument(
        municipality=municipality,
        document=doc.id,
        name=doc.name,
        registry=doc.registry,
        source=PreparedSource(file=Path(doc.parameters).name, sha256=doc.sha256, pages=doc.pages),
        preprocess_version=pages.version,
        prepared_on=prepared_on or date.today().isoformat(),
        basis=(
            "Read from the parameter table by the table reader (the PDF stage's grids with the "
            "document's column map), no model. Checked against the rendered pages: "
            + (checked or "not yet")
        ),
        fields=[f for f in PLANNING_FIELDS if any(f in p.values for p in parcels)],
        parcels=parcels,
    )
    return prepared, problems


# --- load -----------------------------------------------------------------------------------------

# The document version and the one of its files the data was read from (by checksum: the pages
# and boxes belong to that PDF).
DOCUMENT_SQL = text(
    """
    SELECT d.id, d.name, d.version, d.is_current_version,
           COALESCE(d.lineage_id, d.id) AS lineage_id,
           (SELECT f.id FROM stored_files f
            WHERE f.municipality_id = d.municipality_id AND f.sha256 = :sha256
              AND (f.id = d.file_id
                   OR EXISTS (SELECT 1 FROM planning_document_files pf
                              WHERE pf.document_id = d.id AND pf.file_id = f.id))
            ORDER BY f.id LIMIT 1) AS file_id
    FROM planning_documents d
    WHERE d.municipality_id = :m AND d.id = :id
    FOR UPDATE OF d
    """
)
LOADED_SQL = text(
    """
    SELECT id FROM extraction_runs
    WHERE municipality_id = :m AND document_id = :document_id AND file_sha256 = :sha256
      AND model = :model AND status = 'ready_for_review'
      AND summary ->> 'data_sha256' = :data_sha256
    ORDER BY id DESC LIMIT 1
    """
)
INSERT_RUN_SQL = text(
    """
    INSERT INTO extraction_runs (municipality_id, document_id, lineage_id, file_id, file_sha256,
                                 model, model_version, prompt_version, schema_version,
                                 preprocess_version, status, requested_by, started_at)
    VALUES (:m, :document_id, :lineage_id, :file_id, :sha256, :model, :model_version, 'none',
            :schema_version, :preprocess_version, 'extracting', :by, :at)
    RETURNING id
    """
)
INSERT_ITEM_SQL = text(
    """
    INSERT INTO planning_parameter_extractions (
        municipality_id, document_id, entity_type, urban_parcel_id, field_key, parameter_key,
        value_text, value_number, unit, raw_text, source_page, source_bbox, source_note,
        extracted_by, review_state, reviewer, reviewed_at, review_note, extraction_method,
        run_id, target_label, target_key, previous_item_id, change)
    VALUES (
        :m, :document_id, 'urban_parcel', :urban_parcel_id, :field_key, :field_key,
        :value_text, :value_number, :unit, :raw_text, :source_page, CAST(:source_bbox AS jsonb),
        :source_note, :extracted_by, CAST('approved' AS review_state), :reviewer, :at, :note,
        'table', :run_id, :target_label, :target_key, :previous_item_id, :change)
    """
)
# An older decision on a target and field the load states anew: it must not publish after it.
RETIRE_SQL = text(
    """
    UPDATE planning_parameter_extractions
    SET superseded_by_run_id = :run_id, superseded_at = :at
    WHERE id = ANY(:ids) AND municipality_id = :m AND published_value_id IS NULL
      AND superseded_at IS NULL AND review_state IN ('approved', 'amended')
    RETURNING id
    """
)
COUNTERS_SQL = text(
    """
    SELECT count(*) FILTER (WHERE review_state = 'pending_review') AS pending,
           count(*) FILTER (WHERE review_state = 'approved') AS approved,
           count(*) FILTER (WHERE review_state = 'amended') AS amended,
           count(*) FILTER (WHERE review_state = 'rejected') AS rejected
    FROM planning_parameter_extractions
    WHERE municipality_id = :m AND document_id = :document_id AND superseded_at IS NULL
    """
)
FINISH_RUN_SQL = text(
    """
    UPDATE extraction_runs
    SET status = 'ready_for_review', finished_at = :at, pages_total = :pages_total,
        pages_processed = CAST(:pages AS jsonb), items_written = :items,
        items_superseded = :superseded, estimated_cost_eur = 0,
        summary = CAST(:summary AS jsonb)
    WHERE id = :id
    """
)


class PreparedLoadError(Exception):
    """The data cannot be loaded on this document (the message says why); nothing changed."""


@dataclass(slots=True)
class LoadSummary:
    document_id: int
    document_name: str = ""
    data: str = ""
    data_sha256: str = ""
    file_id: int | None = None
    run_id: int | None = None
    already_loaded: bool = False
    dry_run: bool = False
    parcels_in_data: int = 0
    parcels_matched: int = 0
    unmatched_parcels: list[str] = field(default_factory=list)
    values_in_data: int = 0
    items_written: int = 0
    items_kept: int = 0  # the target already holds the same approved value
    by_change: dict[str, int] = field(default_factory=dict)
    changed: list[dict[str, Any]] = field(default_factory=list)
    superseded_items: int = 0
    superseded_runs: list[int] = field(default_factory=list)
    superseded_not_in_data: int = 0  # pending items the load replaced without stating them
    retired_decisions: int = 0
    review_before: dict[str, int] = field(default_factory=dict)
    review_after: dict[str, int] = field(default_factory=dict)


def _effective(previous: Mapping[str, Any]) -> Any:
    amended = previous["review_state"] == "amended"
    number = previous["amended_value_number"] if amended else previous["value_number"]
    return (
        number
        if number is not None
        else (previous["amended_value_text"] if amended else previous["value_text"])
    )


async def load_prepared(
    factory: SessionFactory,
    *,
    municipality_id: str,
    document_id: int,
    prepared: PreparedDocument,
    data_sha256: str,
    by: str,
    note: str = NOTE,
    dry_run: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> LoadSummary:
    """Stage the prepared values of a document as approved items of one ``table-reader`` run
    (one transaction; ``dry_run`` rolls it back and still reports)."""
    from api.services.audit import write_audit
    from jobs.extraction_runner import (  # one rule for what a newer reading replaces
        PARCELS_SQL,
        PREVIOUS_SQL,
        SUPERSEDE_RUNS_SQL,
        SUPERSEDE_SQL,
        value_change,
    )

    if prepared.municipality != municipality_id:
        raise PreparedLoadError(
            f"the data is {prepared.municipality!r}'s, this is {municipality_id!r}"
        )
    summary = LoadSummary(
        document_id=document_id,
        data=prepared.document,
        data_sha256=data_sha256,
        dry_run=dry_run,
        parcels_in_data=len(prepared.parcels),
        values_in_data=prepared.value_count,
    )
    now = clock()
    abbreviation = Conventions.from_profile(municipality_id).parcel_abbreviation
    async with factory() as session:
        params = {"m": municipality_id, "document_id": document_id}
        document = (
            (
                await session.execute(
                    DOCUMENT_SQL,
                    {"m": municipality_id, "id": document_id, "sha256": prepared.source.sha256},
                )
            )
            .mappings()
            .first()
        )
        if document is None:
            raise PreparedLoadError(f"no planning document {document_id} in {municipality_id!r}")
        summary.document_name = document["name"]
        if not document["is_current_version"]:
            raise PreparedLoadError(f"document {document_id} is not the current version")
        if document["file_id"] is None:
            raise PreparedLoadError(
                f"document {document_id} has no file with the checksum the data was read from "
                f"({prepared.source.file}, sha256 {prepared.source.sha256[:12]}…): the pages and "
                "boxes would not match"
            )
        summary.file_id = int(document["file_id"])
        loaded = (
            await session.execute(
                LOADED_SQL,
                {
                    **params,
                    "sha256": prepared.source.sha256,
                    "model": READER,
                    "data_sha256": data_sha256,
                },
            )
        ).scalar_one_or_none()
        if loaded is not None:
            summary.run_id, summary.already_loaded = int(loaded), True
            return summary

        summary.review_before = dict((await session.execute(COUNTERS_SQL, params)).mappings().one())
        run_id = int(
            (
                await session.execute(
                    INSERT_RUN_SQL,
                    {
                        **params,
                        "lineage_id": document["lineage_id"],
                        "file_id": summary.file_id,
                        "sha256": prepared.source.sha256,
                        "model": READER,
                        "model_version": f"{READER}/{prepared.preprocess_version}",
                        "schema_version": f"prepared-{prepared.format}",
                        "preprocess_version": prepared.preprocess_version,
                        "by": by,
                        "at": now,
                    },
                )
            ).scalar_one()
        )
        summary.run_id = run_id
        parcels: dict[str, int] = {}
        for row in (await session.execute(PARCELS_SQL, params)).mappings():
            key = parcel_key(row["urban_parcel_number"], abbreviation)
            if key:
                parcels.setdefault(key, int(row["id"]))
        previous = {
            (p["entity_type"], p["target_key"], p["field_key"]): p
            for p in (
                await session.execute(
                    PREVIOUS_SQL,
                    {"m": municipality_id, "lineage_id": document["lineage_id"], "run_id": run_id},
                )
            ).mappings()
        }

        changes: Counter[str] = Counter()
        retire: list[int] = []
        stated: set[tuple[str, str, str]] = set()
        pages: set[int] = set()
        for parcel in prepared.parcels:
            stated.update(("urban_parcel", parcel.key, name) for name in parcel.values)
            parcel_id = parcels.get(parcel.key)
            if parcel_id is None:
                summary.unmatched_parcels.append(parcel.number)
                continue
            summary.parcels_matched += 1
            for name, value in parcel.values.items():
                numeric = not isinstance(value.value, str)
                row = {
                    "value_text": None if numeric else value.value,
                    "value_number": float(value.value) if numeric else None,
                    "unit": value.unit,
                }
                prior = previous.get(("urban_parcel", parcel.key, name))
                change = value_change(prior, row)
                decided = prior is not None and prior["review_state"] in ("approved", "amended")
                if decided and change == "same":
                    summary.items_kept += 1
                    continue
                if decided:
                    retire.append(int(prior["id"]))
                changes[change] += 1
                if change == "changed" and len(summary.changed) < 50:
                    summary.changed.append(
                        {
                            "parcel": parcel.number,
                            "field": name,
                            "previous": _effective(prior),
                            "previous_state": prior["review_state"],
                            "prepared": value.value,
                        }
                    )
                pages.add(value.page)
                await session.execute(
                    INSERT_ITEM_SQL,
                    {
                        **params,
                        **row,
                        "urban_parcel_id": parcel_id,
                        "field_key": name,
                        "raw_text": value.printed,
                        "source_page": value.page,
                        "source_bbox": json.dumps(list(value.bbox)) if value.bbox else None,
                        "source_note": value.note,
                        "extracted_by": f"{READER}:{prepared.preprocess_version}",
                        "reviewer": by,
                        "at": now,
                        "note": note,
                        "run_id": run_id,
                        "target_label": parcel.number,
                        "target_key": parcel.key,
                        "previous_item_id": prior["id"] if prior is not None else None,
                        "change": change,
                    },
                )
                summary.items_written += 1
        if summary.items_written == 0:
            await session.rollback()
            if summary.parcels_matched == 0:
                raise PreparedLoadError(
                    f"none of the {summary.parcels_in_data} parcels of the data matches a planned "
                    f"parcel of document {document_id}: publish its geometry first"
                )
            summary.run_id = None
            summary.review_after = summary.review_before
            return summary  # every value is already approved on its target

        summary.by_change = dict(changes)
        summary.superseded_not_in_data = sum(
            1
            for key, item in previous.items()
            if item["review_state"] == "pending_review" and key not in stated
        )
        if retire:
            retired = await session.execute(
                RETIRE_SQL, {"ids": retire, "m": municipality_id, "run_id": run_id, "at": now}
            )
            summary.retired_decisions = len(retired.all())
        superseded = (
            await session.execute(
                SUPERSEDE_SQL,
                {
                    "m": municipality_id,
                    "run_id": run_id,
                    "at": now,
                    "lineage_id": document["lineage_id"],
                    "document_id": document_id,
                    "file_id": summary.file_id,
                    "version": document["version"],
                },
            )
        ).all()
        summary.superseded_items = len(superseded)
        summary.superseded_runs = sorted({int(r[1]) for r in superseded})
        if summary.superseded_runs:
            await session.execute(
                SUPERSEDE_RUNS_SQL, {"run_id": run_id, "ids": summary.superseded_runs}
            )
        summary.review_after = dict((await session.execute(COUNTERS_SQL, params)).mappings().one())
        details = {
            k: v
            for k, v in asdict(summary).items()
            if k not in ("review_before", "review_after", "dry_run", "already_loaded")
        }
        details |= {
            "reader": READER,
            "source_file": prepared.source.file,
            "source_sha256": prepared.source.sha256,
            "prepared_on": prepared.prepared_on,
            "basis": prepared.basis,
        }
        await session.execute(
            FINISH_RUN_SQL,
            {
                "id": run_id,
                "at": now,
                "pages_total": prepared.source.pages,
                "pages": json.dumps(sorted(pages)),
                "items": summary.items_written,
                "superseded": summary.superseded_items,
                "summary": json.dumps({"status": "ready_for_review", **details}, default=str),
            },
        )
        await write_audit(
            session,
            municipality_id=municipality_id,
            actor=by,
            action=ACTION,
            entity_type="extraction_run",
            entity_id=run_id,
            details=details,
            before=summary.review_before,
            after=summary.review_after,
            note=note,
        )
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    return summary


# --- command line ---------------------------------------------------------------------------------


def cmd_build(doc_ids: list[str]) -> int:
    from core.extraction.corpus import load_corpus, load_gold
    from core.extraction.evalcli import CACHE_DIR
    from core.extraction.harness import document_pages

    corpus = load_corpus()
    for doc_id in doc_ids or corpus.ids:
        doc = corpus.get(doc_id)
        pages = document_pages(doc, CACHE_DIR, municipality=corpus.municipality)
        prepared, problems = build_prepared(
            doc, load_gold(doc.id), pages, municipality=corpus.municipality
        )
        path = save_prepared(prepared)
        boxes = sum(1 for p in prepared.parcels for v in p.values.values() if v.bbox)
        print(
            f"{doc.id}: {len(prepared.parcels)} parcels, {prepared.value_count} values "
            f"({boxes} with a box) -> {path}"
        )
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
    return 0


def report(summary: LoadSummary) -> str:
    lines = [f"document {summary.document_id} {summary.document_name!r} <- {summary.data}"]
    if summary.already_loaded:
        return "\n".join([*lines, f"  already loaded (run {summary.run_id}): nothing changed"])
    lines += [
        f"  parcels: {summary.parcels_matched} of {summary.parcels_in_data} match a planned parcel",
        f"  items written (approved): {summary.items_written} of {summary.values_in_data} values"
        + (f"; kept as they were: {summary.items_kept}" if summary.items_kept else ""),
        f"  against the previous reading: {summary.by_change or '-'}",
        f"  superseded pending items: {summary.superseded_items}"
        f" (runs {summary.superseded_runs or '-'}; {summary.superseded_not_in_data} of them state"
        " something the data does not)",
        f"  review before: {summary.review_before}",
        f"  review after:  {summary.review_after}",
    ]
    if summary.retired_decisions:
        lines.append(f"  older decisions retired: {summary.retired_decisions}")
    if summary.unmatched_parcels:
        lines.append(
            f"  no planned parcel for {len(summary.unmatched_parcels)}: "
            + ", ".join(summary.unmatched_parcels)
        )
    for item in summary.changed:
        lines.append(
            f"  changed {item['parcel']} {item['field']}: {item['previous']!r} "
            f"({item['previous_state']}) -> {item['prepared']!r}"
        )
    lines.append(
        "  dry run: nothing was written"
        if summary.dry_run
        else f"  run {summary.run_id}: publish to serve the values"
        if summary.run_id is not None
        else "  nothing to write"
    )
    return "\n".join(lines)


async def _load(args: argparse.Namespace) -> int:
    from core.config import get_settings

    settings = get_settings()
    path = args.file or prepared_path(settings.municipality_id, args.doc)
    if not path.is_file():
        print(f"{path} is missing: run `prepared build` first", file=sys.stderr)
        return 2
    prepared, checksum = read_prepared(path)
    engine = create_async_engine(settings.database_url)
    try:
        summary = await load_prepared(
            async_sessionmaker(engine, expire_on_commit=False),
            municipality_id=settings.municipality_id,
            document_id=args.document_id,
            prepared=prepared,
            data_sha256=checksum,
            by=args.by,
            note=f"{NOTE} {args.note}".strip() if args.note else NOTE,
            dry_run=args.dry_run,
        )
    except PreparedLoadError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(
        json.dumps(asdict(summary), ensure_ascii=False, default=str)
        if args.json
        else report(summary)
    )
    return 0


def add_parser(commands: Any) -> None:
    prepared = commands.add_parser(
        "prepared", help="planning values prepared from a parameter table (no model)"
    )
    actions = prepared.add_subparsers(dest="action", required=True)
    build = actions.add_parser("build", help="write the data files from the corpus gold sets")
    build.add_argument("--doc", action="append", default=[])
    load = actions.add_parser("load", help="stage a data file as approved items of a document")
    load.add_argument("--doc", required=True, help="the corpus document id (the data file's name)")
    load.add_argument("--document-id", type=int, required=True, help="the registered document")
    load.add_argument("--file", type=Path, help="another data file than the committed one")
    load.add_argument("--by", default="cli", help="who loads it (reviewer and audit actor)")
    load.add_argument("--note", help="added to the note every item and the audit row carry")
    load.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    load.add_argument("--json", action="store_true")


def run(args: argparse.Namespace) -> int:
    if args.action == "build":
        return cmd_build(args.doc)
    return asyncio.run(_load(args))
