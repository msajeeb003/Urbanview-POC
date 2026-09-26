"""Hand-labelled sample pages: what a person transcribed from real pages of the POC documents, used
to check that the contract holds real documents before prompts are tuned, and to score a model.

A label file ``<name>.labels.json`` covers one page of one source PDF for one task::

    {
      "source": "02 Novi Grad 1 i 2/Novi Grad 1 i 2 - Urban parcels - Planning parameters.pdf",
      "page": 1, "task": "parameter_table", "municipality": "podgorica",
      "document": {"id": 1, "name": "DUP Novi grad 1 i 2 – izmjene i dopune", "type": "DUP"},
      "labelled_by": "...", "labelled_on": "2026-09-26", "notes": "...",
      "urban_parcels": [
        {"urban_parcel_number": "UP 2", "block_ref": "A",
         "planned_parcel_area_m2": ["1906.09", "none"], "land_use": "stanovanje sa djelatn.",
         "max_floors": "Po+P+6", "max_site_coverage_pct": ["1", "ratio"],
         "max_far": ["7", "ratio"]},
        {"urban_parcel_number": "UP 1", ..., "deferred": {"max_far": "definisaće se ..."}}
      ],
      "blocks": [{"block_ref": "UKUPNO BLOK F", "is_total_row": true, "area_m2": [...]}],
      "document_fields": {"name": "...", "document_type": {"value": "DUP", "raw_text": "DUP-a"},
                          "notes": ["Indeks zauzetosti 0.2"], "amendments": [...]}
    }

A field is a string (printed value = raw text), ``[value, unit]`` or ``{"value", "unit",
"raw_text"}``; a field not listed is not stated on the page. :func:`check_label_file` turns a
label file into the canonical contract through the same validator the model's output goes
through (so every label must be found on its page), writes ``<name>.result.json`` and reads it
back with the schema, unchanged. :func:`compare` scores a model's result against the labels.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.extraction.cases import deferred, nf, v
from core.extraction.fields import RULE_FIELDS
from core.extraction.normalise import Conventions
from core.extraction.pages import PageInput, read_pdf_pages
from core.extraction.prompts import PROMPT_VERSION, DocumentContext
from core.extraction.response import RESPONSE_MODELS
from core.extraction.schema import (
    ExtractionResult,
    Leaf,
    MissingValue,
    StatedValue,
    TaskKind,
)
from core.extraction.validate import assemble

_PARCEL_FIELDS = ("block_ref", "planned_parcel_area_m2", "public_area_relation", *RULE_FIELDS)
_DOCUMENT_FIELDS = (
    "name",
    "document_type",
    "status",
    "gazette_reference",
    "decision_number",
    "decision_date",
    "area_ha",
)


@dataclass(frozen=True, slots=True)
class LabelFile:
    path: Path
    source: str
    page: int
    task: TaskKind
    municipality: str
    document: DocumentContext
    data: dict[str, Any]

    @property
    def name(self) -> str:
        return self.path.name.removesuffix(".labels.json")


def load_labels(path: Path) -> LabelFile:
    data = json.loads(path.read_text(encoding="utf-8"))
    doc = data["document"]
    return LabelFile(
        path=path,
        source=data["source"],
        page=int(data["page"]),
        task=data["task"],
        municipality=data.get("municipality", "podgorica"),
        document=DocumentContext(id=int(doc["id"]), name=doc["name"], type=doc.get("type")),
        data=data,
    )


def _out(label: Any, page: int, row: str | None, column: str | None = None) -> dict[str, Any]:
    where = {"page": page, "row": row, "column": column, "confidence": 1.0}
    if isinstance(label, str):
        return v(label, **where)
    if isinstance(label, list):
        value, unit = label
        return v(value, unit=unit, **where)
    return v(label["value"], label.get("raw_text"), unit=label.get("unit"), **where)


def _entity(
    labels: dict[str, Any],
    keys: tuple[str, ...],
    page: int,
    row: str | None,
    columns: dict[str, str],
) -> dict[str, dict[str, Any]]:
    deferrals = labels.get("deferred", {})
    out: dict[str, dict[str, Any]] = {}
    for key in keys:
        if key in labels:
            out[key] = _out(labels[key], page, row, columns.get(key))
        elif key in deferrals:
            out[key] = deferred(deferrals[key], page=page, row=row)
        else:
            out[key] = nf()
    return out


def labels_to_response(labels: LabelFile) -> dict[str, Any]:
    """The response a faithful model would give for the labelled page. A table's file-level
    ``columns`` map (field -> header as printed) becomes each value's table_ref.column."""
    page, data = labels.page, labels.data
    table = labels.task == "parameter_table"
    columns: dict[str, str] = data.get("columns", {}) if table else {}
    parcels = []
    for item in data.get("urban_parcels", []):
        row = item["urban_parcel_number"] if table else None
        fields = _entity(item, _PARCEL_FIELDS, page, row, columns)
        number_column = columns.get("urban_parcel_number")
        parcels.append(
            {
                "urban_parcel_number": _out(item["urban_parcel_number"], page, row, number_column),
                "block_ref": fields["block_ref"],
                "planned_parcel_area_m2": fields["planned_parcel_area_m2"],
                "rules": {key: fields[key] for key in RULE_FIELDS},
                "public_area_relation": fields["public_area_relation"],
                "other_conditions": [_out(o, page, row) for o in item.get("other_conditions", [])],
            }
        )
    blocks = []
    for item in data.get("blocks", []):
        row = item["block_ref"] if table else None
        block_columns = dict(columns)
        if "planned_parcel_area_m2" in columns:  # a totals row sums the parcel-area column
            block_columns["area_m2"] = columns["planned_parcel_area_m2"]
        fields = _entity(item, ("area_m2", *RULE_FIELDS), page, row, block_columns)
        blocks.append(
            {
                "block_ref": _out(item["block_ref"], page, row),
                "is_total_row": bool(item.get("is_total_row", False)),
                "area_m2": fields["area_m2"],
                "rules": {key: fields[key] for key in RULE_FIELDS},
                "notes": [_out(n, page, None) for n in item.get("notes", [])],
            }
        )
    if labels.task == "document":
        doc = data.get("document_fields", {})
        fields = _entity(doc, _DOCUMENT_FIELDS, page, None, {})
        rule_labels = _entity(doc.get("rules", {}), RULE_FIELDS, page, None, {})
        return {
            "document": {
                **fields,
                "amendments": [
                    {"relation": a["relation"], "document_name": _out(a, page, None)}
                    for a in doc.get("amendments", [])
                ],
                "rules": rule_labels,
                "notes": [_out(n, page, None) for n in doc.get("notes", [])],
            }
        }
    if labels.task in ("parameter_table", "urban_parcel"):
        return {"urban_parcels": parcels, "blocks": blocks, "columns": []}
    if labels.task == "block":
        return {"blocks": blocks}
    raise ValueError(f"{labels.path.name}: no label format for task {labels.task!r}")


def pages_for(labels: LabelFile, docs_root: Path) -> list[PageInput]:
    return read_pdf_pages(
        docs_root / labels.source, [labels.page], tables=labels.task == "parameter_table"
    )


def result_from_labels(labels: LabelFile, pages: list[PageInput]) -> ExtractionResult:
    response = RESPONSE_MODELS[labels.task].model_validate(labels_to_response(labels))
    return assemble(
        labels.task,
        response,
        pages=pages,
        document_id=labels.document.id,
        municipality_id=labels.municipality,
        conventions=Conventions.from_profile(labels.municipality),
        prompt_version=PROMPT_VERSION,
        model="hand-labelled",
    )


@dataclass(slots=True)
class LabelCheck:
    name: str
    result: ExtractionResult
    problems: list[str] = field(default_factory=list)
    written: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def check_label_file(path: Path, docs_root: Path, *, write: bool = True) -> LabelCheck:
    """Build the canonical result of a label file; every label must be found on its page, and the
    written result must read back unchanged with the schema."""
    labels = load_labels(path)
    result = result_from_labels(labels, pages_for(labels, docs_root))
    check = LabelCheck(labels.name, result)
    for issue in result.issues:
        check.problems.append(f"{issue.path}: {issue.code} ({issue.message})")
    if write:
        target = path.with_name(f"{labels.name}.result.json")
        text = result.model_dump_json(indent=2)
        target.write_text(text + "\n", encoding="utf-8")
        if ExtractionResult.model_validate_json(target.read_text(encoding="utf-8")) != result:
            check.problems.append(f"{target.name} does not read back unchanged")
        check.written = target
    return check


# --- scoring a model against the labels ----------------------------------------------------------


def addressed_leaves(result: ExtractionResult) -> dict[str, Leaf]:
    """Leaves by stable address (``parcel:<key>/<path>``, ``block:<key>/<path>``,
    ``document/<path>``), independent of the order the entities came in."""
    out: dict[str, Leaf] = {}

    def add(prefix: str, entity: Any, names: tuple[str, ...]) -> None:
        for name in names:
            out[f"{prefix}/{name}"] = getattr(entity, name)
        for key in RULE_FIELDS:
            out[f"{prefix}/rules.{key}"] = getattr(entity.rules, key)

    for parcel in result.urban_parcels:
        if parcel.parcel_key:
            add(
                f"parcel:{parcel.parcel_key}",
                parcel,
                ("block_ref", "planned_parcel_area_m2", "public_area_relation"),
            )
    for block in result.blocks:
        if block.block_key:
            add(f"block:{block.block_key}", block, ("area_m2",))
    if result.document is not None:
        add("document", result.document, _DOCUMENT_FIELDS)
    return out


@dataclass(slots=True)
class Score:
    matched: list[str] = field(default_factory=list)  # same printed value (or both deferred)
    missed: list[str] = field(default_factory=list)  # labelled, the model gave nothing
    wrong: list[str] = field(default_factory=list)  # labelled, the model gave another value
    extra: list[str] = field(default_factory=list)  # not on the page, the model gave a value
    unverified: list[str] = field(default_factory=list)  # removed: text not on the page

    @property
    def guesses(self) -> int:
        return len(self.extra)


def _state(leaf: Leaf | None) -> str | None:
    if isinstance(leaf, StatedValue):
        return leaf.stated.value
    if isinstance(leaf, MissingValue) and leaf.reason == "deferred":
        return "<deferred>"
    return None


def compare(model: ExtractionResult, labels: ExtractionResult) -> Score:
    score = Score()
    got = addressed_leaves(model)
    for address, leaf in got.items():
        if isinstance(leaf, MissingValue) and leaf.reason == "unverified":
            score.unverified.append(address)
    expected = addressed_leaves(labels)
    for address in sorted(set(expected) | set(got)):
        want, have = _state(expected.get(address)), _state(got.get(address))
        if want is None and have is None:
            continue
        if want is None:
            score.extra.append(address)
        elif have is None:
            score.missed.append(address)
        elif " ".join(want.split()).casefold() == " ".join(have.split()).casefold():
            score.matched.append(address)
        else:
            score.wrong.append(address)
    return score
