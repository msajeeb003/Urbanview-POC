"""From transcription to contract: :func:`assemble` builds the canonical
:class:`~core.extraction.schema.ExtractionResult` from a model response and checks every value.

For each field the model returned, in this order:

1. shape: a value needs ``raw_text`` and a page, a null needs ``absent_reason``;
2. the source text: ``raw_text`` must be on the cited page (case-, accent- and
   whitespace-insensitive, whole words). When it is not but it is on exactly one other page that
   was read, that page is taken and the value flagged ``page_corrected``; otherwise the value is
   removed (``raw_text_not_on_page`` / ``page_not_in_input``);
3. the value: it must be inside that text (``value_not_in_raw_text``). A number the model
   computed, converted or rounded is not printed on the page, so it never survives;
4. typing and normalisation by field kind (``core.extraction.normalise``): numbers in canonical
   units, dates, the floor notation, land-use classes, codes; every rule is named;
5. the source box from the page's words (a table row label settles repeats; a cited grid cell
   gives the cell's box), the extraction method, the confidence.

Flags (``low_confidence`` below the configured threshold, ``out_of_range`` for implausible values,
``unit_assumed`` ...) never remove a value: the reviewer sees it with its flags. A removed value
is never silent either: the field becomes ``unverified`` and an :class:`~core.extraction.schema.
Issue` keeps what the model returned. ``raw_text`` and the printed value are always taken from
the page, never from the model's copy. Nothing is published from here.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from core.extraction.fields import FIELD_SPECS, RULE_FIELDS, FieldSpec
from core.extraction.normalise import (
    Conventions,
    block_key,
    classify_land_use,
    match_code,
    normalise_number,
    parcel_key,
    parse_date,
    parse_floors,
    parse_number,
)
from core.extraction.pages import PageInput, TableCell
from core.extraction.response import (
    BlocksResponse,
    DocumentResponse,
    InfrastructureResponse,
    LegendResponse,
    OutBlock,
    OutDocument,
    OutParcel,
    OutRules,
    OutTableRef,
    OutValue,
    ParcelsResponse,
)
from core.extraction.schema import (
    Amendment,
    Block,
    ColumnMapping,
    ExtractionResult,
    Flag,
    FloorCount,
    Issue,
    IssueCode,
    LandUseClass,
    Leaf,
    LegendEntry,
    MissingValue,
    PlanningDocument,
    RuleFields,
    ScopeRef,
    SourceRef,
    Stated,
    StatedValue,
    TableRef,
    TaskKind,
    UrbanParcel,
    UtilityItem,
    summarise,
)
from core.extraction.textmatch import (
    Occurrence,
    PageIndex,
    find_in,
    pick_box,
    round_box,
    words_fold,
)

DOCUMENT_STATUSES: tuple[str, ...] = ("adopted", "in_progress", "superseded")
MAX_FLOORS_ABOVE, MAX_FLOORS_BELOW = 60, 10


@dataclass(frozen=True, slots=True)
class _Found:
    page: int
    occurrence: Occurrence
    corrected: bool
    source: str | None = None  # the text the occurrence indexes: a grid cell's, else the page's


@dataclass(slots=True)
class _Typed:
    value: float | str
    unit: str | None = None
    rules: list[str] = field(default_factory=list)
    derived: FloorCount | None = None
    category: LandUseClass | None = None


@dataclass(slots=True)
class _Run:
    task: TaskKind
    document_id: int
    pages: dict[int, PageInput]
    conventions: Conventions
    low_confidence: float
    legend: Mapping[str, str]
    issues: list[Issue] = field(default_factory=list)
    indexes: dict[int, PageIndex] = field(default_factory=dict)

    def index(self, page: int) -> PageIndex:
        if page not in self.indexes:
            self.indexes[page] = PageIndex(self.pages[page])
        return self.indexes[page]

    def issue(
        self, code: IssueCode, path: str, message: str, out: OutValue | None, *, warning=False
    ) -> None:
        self.issues.append(
            Issue(
                code=code,
                severity="warning" if warning else "error",
                path=path,
                message=message,
                candidate=out.model_dump(exclude_none=True) if out is not None else None,
            )
        )

    # --- where is the text -------------------------------------------------------------------

    def locate(
        self, text: str, cited: int | None, ref: OutTableRef | None = None
    ) -> tuple[_Found | None, IssueCode]:
        if cited in self.pages:
            # the cited grid cell first (the page's own words inside the cell box): a short value
            # ("A", "1") would otherwise match its first occurrence anywhere on the page, and a
            # cell wrapped over several lines is interleaved with its neighbours in the page text
            cell = self.cited_cell(self.pages[cited], ref)
            if cell is not None and (hits := find_in(cell.text, text)):
                return _Found(cited, hits[0], False, cell.text), "raw_text_not_on_page"
            hits = self.index(cited).find(text)
            if hits:
                return _Found(cited, hits[0], False), "raw_text_not_on_page"
        elsewhere = [
            _Found(number, hits[0], True)
            for number in self.pages
            if number != cited and (hits := self.index(number).find(text))
        ]
        if len(elsewhere) == 1:
            return elsewhere[0], "raw_text_not_on_page"
        return None, ("raw_text_not_on_page" if cited in self.pages else "page_not_in_input")

    @staticmethod
    def cited_cell(page: PageInput, ref: OutTableRef | None) -> TableCell | None:
        if ref is None or not ref.cell:
            return None
        grid_ids = {t.id for t in page.tables}
        return page.cell(ref.cell, ref.table if ref.table in grid_ids else None)

    def resolve_merged(self, out: OutValue) -> OutValue:
        """A value printed once over several rows, cited by the empty cell of the row it is
        given for, cites the cell it is printed in."""
        ref = out.table_ref
        if ref is None or not ref.cell or out.page not in self.pages:
            return out
        page = self.pages[out.page]
        cell = self.cited_cell(page, ref)
        if cell is None or cell.text:
            return out
        grid_ids = {t.id for t in page.tables}
        origin = page.origin_cell(ref.cell, ref.table if ref.table in grid_ids else None)
        if origin is None:
            return out
        return out.model_copy(update={"table_ref": ref.model_copy(update={"cell": origin.id})})

    def where(
        self, found: _Found, out: OutValue, anchor: str | None, printed: str, flags: list[Flag]
    ) -> tuple[TableRef | None, tuple[float, float, float, float] | None]:
        page = self.pages[found.page]
        ref = out.table_ref
        table_ref: TableRef | None = None
        box = None
        if ref is not None and any((ref.table, ref.row, ref.column, ref.cell)):
            cell_id = ref.cell
            if cell_id:
                grid_ids = {t.id for t in page.tables}
                cell = page.cell(cell_id, ref.table if ref.table in grid_ids else None)
                if cell is not None and find_in(cell.text, printed):
                    box = cell.bbox
                else:
                    flags.append(Flag.cell_mismatch)
                    cell_id = None
            table_ref = TableRef(table=ref.table, row=ref.row, column=ref.column, cell=cell_id)
        if box is None:
            index = self.index(found.page)
            boxes = index.word_boxes(found.occurrence.text)
            if len(boxes) == 1:
                box = boxes[0]
            elif boxes:
                row = (ref.row if ref is not None and ref.row else None) or anchor
                column = ref.column if ref is not None else None
                box = pick_box(
                    boxes,
                    index.word_boxes(row) if row else [],
                    index.word_boxes(column) if column else [],
                )
                if box is None:
                    flags.append(Flag.bbox_ambiguous)
        return table_ref, round_box(box) if box else None

    def method(self, page: PageInput, out: OutValue) -> str:
        if page.method == "ocr":
            return "ocr"
        ref = out.table_ref
        if self.task == "parameter_table" or (ref is not None and (ref.cell or ref.column)):
            return "table"
        return "text"

    # --- one field ---------------------------------------------------------------------------

    def leaf(
        self,
        out: OutValue,
        key: str,
        path: str,
        *,
        anchor: str | None = None,
        aggregate: bool = False,
    ) -> Leaf:
        spec = FIELD_SPECS[key]
        value = (out.value or "").strip()
        if not value:
            return self.absent(out, path, anchor)
        if out.absent_reason == "deferred":
            self.issue("malformed_leaf", path, "a deferred field with a value", out, warning=True)
            return self.absent(out, path, anchor)
        if out.absent_reason is not None:
            self.issue("malformed_leaf", path, "a value together with an absent_reason", out)
            return MissingValue(reason="unverified", note="value and absent_reason both given")
        if not (out.raw_text or "").strip() or out.page is None:
            self.issue("malformed_leaf", path, "a value needs raw_text and a page", out)
            return MissingValue(reason="unverified", note="no raw_text or no page")
        out = self.resolve_merged(out)
        found, problem = self.locate(out.raw_text or "", out.page, out.table_ref)
        if found is None:
            self.issue(problem, path, f"raw_text is not on page {out.page}", out)
            return MissingValue(reason="unverified", note="its text is not on the cited page")
        page = self.pages[found.page]
        if spec.kind == "code":
            printed = value
        else:
            source = found.source if found.source is not None else page.text
            inside = find_in(source[found.occurrence.start : found.occurrence.end], value)
            if not inside:
                self.issue("value_not_in_raw_text", path, f"{value!r} is not in its raw_text", out)
                return MissingValue(
                    reason="unverified", note="the value is not in the text it cites"
                )
            printed = inside[0].text
        flags: list[Flag] = [Flag.page_corrected] if found.corrected else []
        typed = self.typed(spec, printed, out, flags)
        confidence = self.confidence(out, path, flags)
        table_ref, box = self.where(found, out, anchor, printed, flags)
        if aggregate:
            flags.append(Flag.aggregate_row)
        return StatedValue(
            value=typed.value,
            unit=typed.unit,
            raw_text=found.occurrence.text,
            source=SourceRef(
                document_id=self.document_id, page=found.page, bbox=box, table_ref=table_ref
            ),
            confidence=confidence,
            extraction_method=self.method(page, out),  # type: ignore[arg-type]
            stated=Stated(value=printed, unit=out.unit),
            normalisation=typed.rules,
            derived=typed.derived,
            category=typed.category,
            flags=list(dict.fromkeys(flags)),
        )

    def absent(self, out: OutValue, path: str, anchor: str | None) -> MissingValue:
        if out.absent_reason == "not_found":
            return MissingValue(reason="not_found")
        if out.absent_reason is None:
            self.issue("malformed_leaf", path, "null without absent_reason", out, warning=True)
            return MissingValue(reason="not_found", note="the model gave no reason")
        # deferred: the deferring text is checked like a value's
        if not (out.raw_text or "").strip() or out.page is None:
            self.issue("malformed_leaf", path, "deferred without its text and page", out)
            return MissingValue(reason="unverified", note="deferred without its text")
        found, problem = self.locate(out.raw_text or "", out.page, out.table_ref)
        if found is None:
            self.issue(problem, path, f"the deferring text is not on page {out.page}", out)
            return MissingValue(reason="unverified", note="its text is not on the cited page")
        table_ref, box = self.where(found, out, anchor, found.occurrence.text, [])
        return MissingValue(
            reason="deferred",
            raw_text=found.occurrence.text,
            source=SourceRef(
                document_id=self.document_id, page=found.page, bbox=box, table_ref=table_ref
            ),
        )

    def typed(self, spec: FieldSpec, printed: str, out: OutValue, flags: list[Flag]) -> _Typed:
        if spec.kind == "number":
            parsed = parse_number(printed)
            if parsed is None:
                flags.append(Flag.text_for_numeric_field)
                return _Typed(printed)
            number = normalise_number(parsed, out.unit, spec)
            flags.extend(number.flags)
            if number.unit == spec.unit and not spec.in_range(number.value):
                flags.append(Flag.out_of_range)
            return _Typed(number.value, number.unit, list(number.rules))
        if spec.kind == "floors":
            derived = parse_floors(printed, self.conventions.floor_tokens)
            if derived is None:
                flags.append(Flag.floors_not_derivable)
            elif derived.above_ground > MAX_FLOORS_ABOVE or derived.below_ground > MAX_FLOORS_BELOW:
                flags.append(Flag.out_of_range)
            return _Typed(printed, derived=derived)
        if spec.kind == "land_use":
            category = classify_land_use(printed, self.conventions.land_use_rules, self.legend)
            if category is None:
                flags.append(Flag.land_use_unmapped)
            return _Typed(printed, category=category)
        if spec.kind == "date":
            parsed_date = parse_date(printed)
            if parsed_date is None:
                flags.append(Flag.date_not_parsed)
                return _Typed(printed)
            return _Typed(parsed_date.isoformat(), rules=["date_dmy"])
        if spec.kind == "code":
            allowed = (
                self.conventions.document_types
                if spec.key == "document_type"
                else DOCUMENT_STATUSES
            )
            code = match_code(printed, allowed)
            if code is None:
                flags.append(Flag.unknown_code)
                return _Typed(printed)
            return _Typed(code)
        return _Typed(printed)

    def confidence(self, out: OutValue, path: str, flags: list[Flag]) -> float:
        value = out.confidence
        if value is None or math.isnan(value) or not 0 <= value <= 1:
            self.issue(
                "malformed_leaf", path, f"confidence {value} is outside 0..1", out, warning=True
            )
            value = 0.0
        if value < self.low_confidence:
            flags.append(Flag.low_confidence)
        return float(value)


# --- entities ------------------------------------------------------------------------------------


def _rules(
    run: _Run, out: OutRules, path: str, *, anchor: str | None = None, aggregate: bool = False
) -> RuleFields:
    return RuleFields(
        **{
            key: run.leaf(
                getattr(out, key), key, f"{path}.{key}", anchor=anchor, aggregate=aggregate
            )
            for key in RULE_FIELDS
        }
    )


def _stated(run: _Run, outs: Iterable[OutValue], path: str) -> list[StatedValue]:
    leaves = [run.leaf(out, "note", f"{path}[{i}]") for i, out in enumerate(outs)]
    return [leaf for leaf in leaves if isinstance(leaf, StatedValue)]


def _printed(leaf: Leaf) -> str | None:
    return leaf.stated.value if isinstance(leaf, StatedValue) else None


def _parcel(run: _Run, out: OutParcel, path: str) -> UrbanParcel:
    number = run.leaf(out.urban_parcel_number, "urban_parcel_number", f"{path}.urban_parcel_number")
    printed = _printed(number)
    key = parcel_key(printed, run.conventions.parcel_abbreviation) if printed else None
    if key is None:
        run.issue(
            "parcel_without_number",
            path,
            "an urban parcel without a verified number cannot be keyed",
            out.urban_parcel_number,
        )
    return UrbanParcel(
        urban_parcel_number=number,
        parcel_key=key,
        block_ref=run.leaf(out.block_ref, "block_ref", f"{path}.block_ref", anchor=printed),
        planned_parcel_area_m2=run.leaf(
            out.planned_parcel_area_m2,
            "planned_parcel_area_m2",
            f"{path}.planned_parcel_area_m2",
            anchor=printed,
        ),
        rules=_rules(run, out.rules, f"{path}.rules", anchor=printed),
        public_area_relation=run.leaf(
            out.public_area_relation, "public_area_relation", f"{path}.public_area_relation"
        ),
        other_conditions=_stated(run, out.other_conditions, f"{path}.other_conditions"),
    )


def _block(run: _Run, out: OutBlock, path: str) -> Block:
    ref = run.leaf(out.block_ref, "block_ref", f"{path}.block_ref")
    printed = _printed(ref)
    total = out.is_total_row
    return Block(
        block_ref=ref,
        block_key=block_key(printed, run.conventions.block_label_words) if printed else None,
        total_row=total,
        area_m2=run.leaf(
            out.area_m2, "area_m2", f"{path}.area_m2", anchor=printed, aggregate=total
        ),
        rules=_rules(run, out.rules, f"{path}.rules", anchor=printed, aggregate=total),
        notes=_stated(run, out.notes, f"{path}.notes"),
    )


def _document(run: _Run, out: OutDocument) -> PlanningDocument:
    def one(key: str) -> Leaf:
        return run.leaf(getattr(out, key), key, f"document.{key}")

    amendments: list[Amendment] = []
    for i, amendment in enumerate(out.amendments):
        name = run.leaf(amendment.document_name, "note", f"document.amendments[{i}]")
        if isinstance(name, StatedValue):
            amendments.append(Amendment(relation=amendment.relation, document_name=name))
    return PlanningDocument(
        name=one("name"),
        document_type=one("document_type"),
        status=one("status"),
        gazette_reference=one("gazette_reference"),
        decision_number=one("decision_number"),
        decision_date=one("decision_date"),
        area_ha=one("area_ha"),
        amendments=amendments,
        rules=_rules(run, out.rules, "document.rules"),
        notes=_stated(run, out.notes, "document.notes"),
    )


def _flag_foreign_text(run: _Run, parcels: Sequence[UrbanParcel]) -> None:
    """Conditions in prose: a parcel's value whose text sits only in another parcel's part of the
    page (between that parcel's heading and the next) is flagged ``found_under_other_parcel``.
    Grounding cannot catch a real printed value put on the wrong parcel; this points at it."""
    for number, page in run.pages.items():
        heads: list[tuple[int, int]] = []
        for i, parcel in enumerate(parcels):
            heading = parcel.urban_parcel_number
            if isinstance(heading, StatedValue) and heading.source.page == number:
                hits = find_in(page.text, heading.raw_text)
                if hits:
                    heads.append((hits[0].start, i))
        if len(heads) < 2:
            continue
        heads.sort()
        ends = [start for start, _ in heads[1:]] + [len(page.text)]
        for (start, i), end in zip(heads, ends, strict=True):
            parcel = parcels[i]
            leaves = [
                parcel.planned_parcel_area_m2,
                parcel.public_area_relation,
                *(getattr(parcel.rules, key) for key in RULE_FIELDS),
                *parcel.other_conditions,
            ]
            for leaf in leaves:
                if not isinstance(leaf, StatedValue) or leaf.source.page != number:
                    continue
                starts = [hit.start for hit in find_in(page.text, leaf.raw_text)]
                if starts and not any(start <= s < end for s in starts):
                    leaf.flags.append(Flag.found_under_other_parcel)


def legend_map(entries: Iterable[LegendEntry]) -> dict[str, str]:
    """Legend code -> name, keyed like :func:`~core.extraction.normalise.classify_land_use`."""
    out: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry.code, StatedValue):
            out[words_fold(str(entry.code.value)).replace(" ", "")] = str(entry.name.value)
    return out


def assemble(
    task: TaskKind,
    response: BaseModel,
    *,
    pages: Sequence[PageInput],
    document_id: int,
    municipality_id: str,
    conventions: Conventions,
    prompt_version: str,
    model: str | None = None,
    low_confidence: float = 0.7,
    legend: Mapping[str, str] | None = None,
) -> ExtractionResult:
    """The canonical result of one model response over ``pages``."""
    run = _Run(
        task=task,
        document_id=document_id,
        pages={p.page: p for p in pages},
        conventions=conventions,
        low_confidence=low_confidence,
        legend=legend or {},
    )
    result = ExtractionResult(
        prompt_version=prompt_version,
        task=task,
        municipality_id=municipality_id,
        document_id=document_id,
        pages=sorted(run.pages),
        model=model,
    )
    if isinstance(response, ParcelsResponse):
        result.urban_parcels = [
            _parcel(run, p, f"urban_parcels[{i}]") for i, p in enumerate(response.urban_parcels)
        ]
        result.blocks = [_block(run, b, f"blocks[{i}]") for i, b in enumerate(response.blocks)]
        result.table_columns = [
            ColumnMapping(table=c.table, page=c.page, header=c.header, field=c.field)
            for c in response.columns
            if c.page >= 1 and c.header.strip()
        ]
        if task == "urban_parcel":
            _flag_foreign_text(run, result.urban_parcels)
        seen = Counter(p.parcel_key for p in result.urban_parcels if p.parcel_key)
        for i, parcel in enumerate(result.urban_parcels):
            if parcel.parcel_key and seen[parcel.parcel_key] > 1:
                run.issue(
                    "duplicate_parcel",
                    f"urban_parcels[{i}]",
                    f"urban parcel {parcel.parcel_key!r} appears {seen[parcel.parcel_key]} times",
                    None,
                    warning=True,
                )
    elif isinstance(response, BlocksResponse):
        result.blocks = [_block(run, b, f"blocks[{i}]") for i, b in enumerate(response.blocks)]
    elif isinstance(response, DocumentResponse):
        result.document = _document(run, response.document)
    elif isinstance(response, InfrastructureResponse):
        for i, item in enumerate(response.utilities):
            description = run.leaf(item.description, "note", f"utilities[{i}].description")
            if isinstance(description, StatedValue):
                result.utilities.append(
                    UtilityItem(
                        scope=ScopeRef(level=item.scope_level, ref=item.scope_ref),
                        kind=item.kind,
                        status=item.status,
                        description=description,
                    )
                )
    elif isinstance(response, LegendResponse):
        for i, entry in enumerate(response.entries):
            name = run.leaf(entry.name, "note", f"land_use_legend[{i}].name")
            if not isinstance(name, StatedValue):
                continue
            category = classify_land_use(str(name.value), conventions.land_use_rules)
            if category is None:
                name = name.model_copy(update={"flags": [*name.flags, Flag.land_use_unmapped]})
            result.land_use_legend.append(
                LegendEntry(
                    code=run.leaf(entry.code, "note", f"land_use_legend[{i}].code"),
                    name=name,
                    category=category,
                )
            )
    else:
        raise TypeError(f"no assembly for {type(response).__name__}")
    result.issues = run.issues
    result.stats = summarise(result)
    return result
