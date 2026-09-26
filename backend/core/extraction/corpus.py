"""The extraction evaluation corpus: specification, gold sets and the gold-set builder.

The corpus (``tests/corpus/``, README there) is the client's POC planning documents. Each
document has a gold set ``tests/corpus/<id>/gold.json``: for every urban parcel of its
planning-parameter table, the expected value of every scored field (``corpus.toml`` ``fields``:
the 11 Group-1 fields plus the block reference) with its page, or ``null`` when the document does
not state it for that parcel (the correct answer is then a blank), or a deferred marker; the
block totals rows; the document metadata the parameter pages state; the special cases the
document holds and those it does not.

``build_gold`` makes a draft gold set from the table grids of the PDF stage with the column map of
``[document.labelling]`` (deterministic, no model); the draft is then checked against the rendered
pages by a person (``verification`` in the file records who checked which pages and what was
corrected). Values are stored as printed and as canonical values through the same deterministic
normalisation the validator uses (a ratio "0.4" -> 40 %), so the scorer compares both.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.extraction.chunking import merged_from
from core.extraction.fields import FIELD_SPECS
from core.extraction.normalise import Conventions, normalise_number, parcel_key, parse_number
from core.extraction.preprocess import DocumentPages, PageTable
from core.extraction.textmatch import words_fold

BACKEND = Path(__file__).resolve().parents[2]
CORPUS_DIR = BACKEND / "tests" / "corpus"
DEFAULT_SOURCE_DIR = BACKEND.parent / "docs" / "gis" / "source"
GOLD_VERSION = 1


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- the corpus specification --------------------------------------------------------------------


class ColumnSpec(_M):
    field: str
    header: str
    match: Literal["exact", "prefix"] = "exact"
    unit: Literal["m", "m2", "ha", "percent", "ratio", "none", "words"] = "none"


class HeaderSpec(_M):
    header: str
    match: Literal["exact", "prefix"] = "exact"


class Labelling(_M):
    number: HeaderSpec
    block: HeaderSpec | None = None
    columns: list[ColumnSpec]
    block_total: str | None = None
    document_total: str | None = None
    footnote_row: str | None = None
    building_row: str | None = None
    deferred: str | None = None
    special_cases: list[str] = Field(default_factory=list)
    not_covered: list[str] = Field(default_factory=list)


class CorpusDocument(_M):
    id: str
    name: str
    type: str
    registry: str | None = None
    parameters: str
    sha256: str
    pages: int
    labelling: Labelling


class Corpus(_M):
    municipality: str
    fields: list[str]
    document: list[CorpusDocument]

    def get(self, document_id: str) -> CorpusDocument:
        for doc in self.document:
            if doc.id == document_id:
                return doc
        raise KeyError(f"no corpus document {document_id!r} (have {self.ids})")

    @property
    def ids(self) -> list[str]:
        return [d.id for d in self.document]


def load_corpus(corpus_dir: Path = CORPUS_DIR) -> Corpus:
    with (corpus_dir / "corpus.toml").open("rb") as fh:
        return Corpus.model_validate(tomllib.load(fh))


def source_dir() -> Path:
    return Path(os.environ.get("CORPUS_SOURCE_DIR") or DEFAULT_SOURCE_DIR)


def read_source(doc: CorpusDocument, root: Path | None = None) -> bytes:
    """The document's PDF, checked against the pinned checksum (the gold set belongs to it)."""
    path = (root or source_dir()) / doc.parameters
    if not path.is_file():
        raise FileNotFoundError(
            f"{doc.id}: {path} is missing; set CORPUS_SOURCE_DIR to the client's source folder"
        )
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != doc.sha256:
        raise ValueError(f"{doc.id}: {path.name} changed (sha256 {digest}); re-label the gold set")
    return data


# --- gold sets ----------------------------------------------------------------------------------


class GoldValue(_M):
    status: Literal["stated", "deferred"] = "stated"
    printed: str = Field(description='As printed ("" for a deferred value)')
    value: float | str | None = Field(description="Canonical value; null when deferred")
    unit: str | None = None
    page: int
    text: str = Field(description="The cell text, or the words that defer the value")
    cell: str | None = None


class GoldParcel(_M):
    number: str
    key: str
    page: int
    table: str | None = None
    values: dict[str, GoldValue | None]
    notes: list[str] = Field(default_factory=list)


class GoldBlock(_M):
    label: str
    page: int
    is_total_row: bool = True
    values: dict[str, GoldValue]


class Verification(_M):
    pages: list[int]
    by: str
    on: str
    corrections: list[str] = Field(default_factory=list)
    notes: str | None = None


class Gold(_M):
    gold_version: int = GOLD_VERSION
    document: str
    name: str
    source: str
    sha256: str
    labelled_by: str
    labelled_on: str
    method: str
    verification: list[Verification] = Field(default_factory=list)
    fields: list[str]
    document_fields: dict[str, GoldValue | None]
    parcels: list[GoldParcel]
    blocks: list[GoldBlock] = Field(default_factory=list)
    special_cases: list[str] = Field(default_factory=list)
    not_covered: list[str] = Field(default_factory=list)


def gold_path(document_id: str, corpus_dir: Path = CORPUS_DIR) -> Path:
    return corpus_dir / document_id / "gold.json"


def load_gold(document_id: str, corpus_dir: Path = CORPUS_DIR) -> Gold:
    return Gold.model_validate_json(gold_path(document_id, corpus_dir).read_text(encoding="utf-8"))


def save_gold(gold: Gold, corpus_dir: Path = CORPUS_DIR) -> Path:
    path = gold_path(gold.document, corpus_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(gold.model_dump_json(indent=1) + "\n", encoding="utf-8", newline="\n")
    return path


# --- canonical values ---------------------------------------------------------------------------


def canonical(field: str, printed: str, unit: str) -> tuple[float | str | None, str | None]:
    """The canonical value of a printed value, by the validator's deterministic rules."""
    spec = FIELD_SPECS[field]
    if spec.kind == "number":
        parsed = parse_number(printed)
        if parsed is None:
            return printed, None
        number = normalise_number(parsed, None if unit == "words" else unit, spec)  # type: ignore[arg-type]
        return number.value, number.unit
    return " ".join(printed.split()), None


# --- the gold-set builder -------------------------------------------------------------------------


def _match(header: str, spec: HeaderSpec | ColumnSpec) -> bool:
    folded = words_fold(header)
    wanted = words_fold(spec.header)
    return folded == wanted if spec.match == "exact" else folded.startswith(wanted)


def _column(columns: list[str], spec: HeaderSpec | ColumnSpec) -> int | None:
    return next((i for i, name in enumerate(columns) if name and _match(name, spec)), None)


def build_gold(
    doc: CorpusDocument,
    pages: DocumentPages,
    fields: list[str],
    *,
    municipality: str,
    labelled_by: str,
) -> Gold:
    """A draft gold set from the parameter table grids (checked by eye afterwards)."""
    abbreviation = Conventions.from_profile(municipality).parcel_abbreviation
    lab = doc.labelling
    total_re = re.compile(lab.block_total) if lab.block_total else None
    document_total_re = re.compile(lab.document_total) if lab.document_total else None
    footnote_re = re.compile(lab.footnote_row) if lab.footnote_row else None
    building_re = re.compile(lab.building_row) if lab.building_row else None
    deferred_re = re.compile(lab.deferred) if lab.deferred else None
    parcels: list[GoldParcel] = []
    blocks: list[GoldBlock] = []
    for page in pages.pages:
        for table in page.tables:
            number_col = _column(table.columns, lab.number)
            if number_col is None:
                continue
            block_col = _column(table.columns, lab.block) if lab.block else None
            field_cols = {c.field: (_column(table.columns, c), c) for c in lab.columns}
            for index, row in enumerate(table.rows):
                if index < table.header_rows:
                    continue
                texts = [" ".join(c.text.split()) for c in row]
                first = words_fold(texts[0]) if texts else ""
                number = texts[number_col] if number_col < len(texts) else ""

                def cell(
                    col: int | None, row=row, texts=texts, index=index, table=table
                ) -> tuple[str, str | None]:
                    if col is None or col >= len(row):
                        return "", None
                    if not texts[col] and (k := merged_from(table, index, col)) is not None:
                        origin = table.rows[k][col]  # a merged cell printed once for these rows
                        return " ".join(origin.text.split()), origin.id
                    return texts[col], row[col].id

                label_text = texts[0] if total_re and total_re.search(first) else number
                if document_total_re and document_total_re.search(words_fold(label_text)):
                    continue  # the plan-wide total: not a block
                if total_re and (total_re.search(first) or total_re.search(words_fold(number))):
                    values = {}
                    for field, (col, spec) in field_cols.items():
                        text, cell_id = cell(col)
                        # a total row sums numbers; a digit in a text column is the wrapped
                        # tail of a neighbouring total, not a value
                        if text and FIELD_SPECS[field].kind == "number":
                            value, unit = canonical(field, text, spec.unit)
                            values[field] = GoldValue(
                                printed=text,
                                value=value,
                                unit=unit,
                                page=page.number,
                                text=text,
                                cell=cell_id,
                            )
                    blocks.append(GoldBlock(label=label_text, page=page.number, values=values))
                    continue
                if not number:
                    continue  # footnote rows, building rows, annex and blank rows
                values: dict[str, GoldValue | None] = {f: None for f in fields}
                deferred_text = next(
                    (t for t in texts if deferred_re and deferred_re.search(words_fold(t))), None
                )
                for field, (col, spec) in field_cols.items():
                    text, cell_id = cell(col)
                    if deferred_text is not None and (not text or text == deferred_text):
                        # the deferring cell spans this column (or is it): the value is deferred
                        if field in ("max_floors", "max_site_coverage_pct", "max_far"):
                            values[field] = GoldValue(
                                status="deferred",
                                printed="",
                                value=None,
                                page=page.number,
                                text=deferred_text,
                            )
                        continue
                    if not text:
                        continue
                    value, unit = canonical(field, text, spec.unit)
                    values[field] = GoldValue(
                        printed=text,
                        value=value,
                        unit=unit,
                        page=page.number,
                        text=text,
                        cell=cell_id,
                    )
                if block_col is not None:
                    text, cell_id = cell(block_col)
                    if text:
                        values["block_ref"] = GoldValue(
                            printed=text, value=text, page=page.number, text=text, cell=cell_id
                        )
                notes = []
                if footnote_re is not None:
                    following = table.rows[index + 1] if index + 1 < len(table.rows) else None
                    if following is not None and footnote_re.search(following[0].text.strip()):
                        notes.append("followed by a *** footnote row (extension areas)")
                if building_re is not None and _has_building_rows(
                    table, index, number_col, building_re
                ):
                    notes.append("building sub-rows printed above it (not parcel values)")
                merged = sorted(
                    field
                    for field, (col, _) in field_cols.items()
                    if col is not None and merged_from(table, index, col) is not None
                )
                if merged:
                    notes.append("merged cell printed once for several rows: " + ", ".join(merged))
                parcels.append(
                    GoldParcel(
                        number=number,
                        key=parcel_key(number, abbreviation) or number,
                        page=page.number,
                        table=table.id,
                        values=values,
                        notes=notes,
                    )
                )
    document_fields = _document_fields(doc, pages, fields_of_document=True)
    return Gold(
        document=doc.id,
        name=doc.name,
        source=doc.parameters,
        sha256=doc.sha256,
        labelled_by=labelled_by,
        labelled_on=date.today().isoformat(),
        method=(
            "draft from the PDF stage's table grids with the column map of corpus.toml "
            "(core.extraction.corpus.build_gold), then checked against the rendered pages "
            "(see verification)"
        ),
        fields=fields,
        document_fields=document_fields,
        parcels=parcels,
        blocks=blocks,
        special_cases=lab.special_cases,
        not_covered=lab.not_covered,
    )


def _has_building_rows(
    table: PageTable, index: int, number_col: int, building_re: re.Pattern[str]
) -> bool:
    for row in reversed(table.rows[table.header_rows : index]):
        if number_col < len(row) and row[number_col].text.strip():
            return False
        if any(building_re.search(words_fold(c.text)) for c in row if c.text):
            return True
    return False


DOCUMENT_FIELDS = (
    "name",
    "document_type",
    "status",
    "gazette_reference",
    "decision_number",
    "decision_date",
    "area_ha",
)


def _document_fields(
    doc: CorpusDocument, pages: DocumentPages, *, fields_of_document: bool
) -> dict[str, GoldValue | None]:
    """The document identity the parameter pages state: the title in the running header (name,
    and the type it designates); status, gazette, decision and area are not in these pages."""
    out: dict[str, GoldValue | None] = {key: None for key in DOCUMENT_FIELDS}
    first = pages.pages[0]
    folded_name = words_fold(doc.name)
    for block in first.blocks:
        text = " ".join(block.text.split())
        if words_fold(text) == folded_name:
            out["name"] = GoldValue(printed=text, value=text, page=first.number, text=text)
            out["document_type"] = GoldValue(
                printed=doc.type, value=doc.type, page=first.number, text=text
            )
            break
    return out
