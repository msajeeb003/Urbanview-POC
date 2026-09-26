"""The AI extraction contract (core.extraction): the schema and its exported JSON Schemas, the
versioned prompts, normalisation, the validator's never-guess and source rules, staging rows,
stored items staying readable, and the hand-labelled sample (when the client documents are on
this machine)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.extraction import cases as C
from core.extraction.__main__ import DEFAULT_DOCS, schema_path
from core.extraction.fields import FIELD_SPECS
from core.extraction.llm import ModelOutputInvalid, ScriptedModel
from core.extraction.normalise import (
    Conventions,
    block_key,
    classify_land_use,
    normalise_number,
    parcel_key,
    parse_date,
    parse_floors,
    parse_number,
)
from core.extraction.pages import PageInput, TableCell, TableGrid
from core.extraction.prompts import (
    PROMPT_VERSION,
    DocumentContext,
    build_prompt,
    generated_response_schemas,
    load_prompt_set,
    response_model,
)
from core.extraction.response import RESPONSE_MODELS, strict_json_schema
from core.extraction.run import run_task
from core.extraction.sample import check_label_file, load_labels, pages_for
from core.extraction.schema import (
    SCHEMA_VERSION,
    ExtractionResult,
    Flag,
    LandUseClass,
    MissingValue,
    StatedValue,
    UnsupportedSchemaVersion,
    iter_leaves,
    read_payload,
    result_json_schema,
)
from core.extraction.staging import to_staging_rows
from core.extraction.textmatch import find_in, pick_box
from core.extraction.validate import assemble, legend_map
from tests.extraction_script import absent

M = "podgorica"
CONVENTIONS = Conventions.from_profile(M)
FIXTURES = Path(__file__).parent / "fixtures" / "extraction"
SAMPLE = DEFAULT_DOCS / "extraction-sample"
CASES = {case.id: case for case in C.ABSENT_CASES}


def build(task: str, data: dict[str, Any], pages: list[PageInput], **kw: Any) -> ExtractionResult:
    response = RESPONSE_MODELS[task].model_validate(data)  # type: ignore[index]
    return assemble(
        task,  # type: ignore[arg-type]
        response,
        pages=pages,
        document_id=kw.pop("document_id", 7),
        municipality_id=M,
        conventions=CONVENTIONS,
        prompt_version=PROMPT_VERSION,
        model="scripted",
        **kw,
    )


def parcels(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"urban_parcels": list(entries), "blocks": [], "columns": []}


def assert_cited_text_on_page(result: ExtractionResult, pages: list[PageInput]) -> None:
    """Every returned value carries a page that actually contains its raw text."""
    by_number = {p.page: p for p in pages}
    for path, leaf in iter_leaves(result):
        if isinstance(leaf, StatedValue) or (
            isinstance(leaf, MissingValue) and leaf.reason == "deferred"
        ):
            assert leaf.source is not None and leaf.raw_text, path
            assert find_in(by_number[leaf.source.page].text, leaf.raw_text), path


PAGE = C.text_page(
    4,
    "Urbanistička parcela UP 14\n"
    "Maksimalni indeks zauzetosti: 0,40\n"
    "Maksimalni indeks izgrađenosti: 1,6\n"
    "Udaljenost od susjednih parcela: h/2\n"
    "Spratnost: P+70",
)
UP14 = C.v("UP 14", "Urbanistička parcela UP 14", page=4)


# --- schema files ------------------------------------------------------------------------------


def test_exported_schemas_are_current():
    """`python -m core.extraction export` output matches the models (regenerate on change)."""
    assert json.loads(schema_path().read_text(encoding="utf-8")) == result_json_schema()
    prompt_set = load_prompt_set()
    for name, schema in generated_response_schemas().items():
        assert prompt_set.response_schema(name) == schema, name


def _walk(node: Any, seen_keys: set[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, seen_keys)
    elif isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            assert node["additionalProperties"] is False
            assert sorted(node["required"]) == sorted(node["properties"])
        for key, value in node.items():
            if key in ("properties", "$defs"):
                for schema in value.values():
                    _walk(schema, seen_keys)
            else:
                seen_keys.add(key)
                _walk(value, seen_keys)


def test_response_schemas_fit_structured_outputs():
    unsupported = {"minimum", "maximum", "minLength", "maxLength", "pattern", "default", "title"}
    for name, schema in generated_response_schemas().items():
        keys: set[str] = set()
        _walk(schema, keys)
        assert not keys & unsupported, name
        # no recursion: $defs form an acyclic reference graph
        refs = {
            d: {r.split("/")[-1] for r in _refs(body)}
            for d, body in schema.get("$defs", {}).items()
        }
        assert all(_acyclic(d, refs, set()) for d in refs), name


def _acyclic(node: str, refs: dict[str, set[str]], visiting: set[str]) -> bool:
    if node in visiting:
        return False
    visiting.add(node)
    ok = all(_acyclic(r, refs, visiting) for r in refs.get(node, ()))
    visiting.discard(node)
    return ok


def _refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        out = [node["$ref"]] if "$ref" in node else []
        return out + [r for v in node.values() for r in _refs(v)]
    if isinstance(node, list):
        return [r for v in node for r in _refs(v)]
    return []


def test_the_canonical_schema_is_versioned():
    assert result_json_schema()["x-schema-version"] == SCHEMA_VERSION
    minimal = {"prompt_version": "1.0", "task": "block", "municipality_id": M, "document_id": 1}
    assert ExtractionResult.model_validate(minimal | {"schema_version": "1.4", "pages": []})
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(minimal | {"schema_version": "2.0", "pages": []})


# --- prompts -----------------------------------------------------------------------------------


def test_prompt_set_covers_every_task_with_its_response():
    prompt_set = load_prompt_set()
    assert prompt_set.version == PROMPT_VERSION
    assert prompt_set.schema_version.split(".")[0] == SCHEMA_VERSION.split(".")[0]
    assert set(prompt_set.tasks) == set(RESPONSE_MODELS)
    for task, spec in prompt_set.tasks.items():
        model = response_model(spec.response)
        answer = model.model_validate(absent(strict_json_schema(model)))
        if hasattr(answer, "to_legacy"):  # compact answers (1.1) become the task's 1.0 model
            answer = answer.to_legacy()
        assert isinstance(answer, RESPONSE_MODELS[task])  # type: ignore[index]
        assert (prompt_set.directory / spec.instructions).is_file()


@pytest.mark.parametrize("task", list(RESPONSE_MODELS))
def test_prompt_renders_rules_glossary_and_pages(task: str):
    bundle = build_prompt(
        task,  # type: ignore[arg-type]
        municipality_id=M,
        document=DocumentContext(3, "DUP Test – izmjene", "DUP"),
        pages=[C.text_page(3, "Urbanistička parcela UP 14")],
    )
    rules, task_block = bundle.system
    assert rules.cache and task_block.cache  # both system blocks are cache breakpoints
    assert "Never guess" in rules.text
    assert "not_found" in rules.text or "left out" in rules.text  # 1.0 / 1.1 wording
    assert "No arithmetic" in rules.text
    # the glossary and document types come from the municipality profile
    assert "indeks zauzetosti" in rules.text and "Po+P+6" in rules.text
    assert "UP = Urbanistički projekat" in rules.text
    assert "DUP Test – izmjene" in task_block.text and "registered as DUP" in task_block.text
    assert "=== Page 3 ===" in bundle.user and "UP 14" in bundle.user
    assert "{{" not in rules.text + task_block.text + bundle.user
    assert bundle.schema == load_prompt_set().response_schema(bundle.response_name)
    assert bundle.prompt_version == PROMPT_VERSION
    if task == "document":
        assert "one of DUP, PUP, PGR, UP" in task_block.text


def test_prompt_templates_carry_no_place_specific_words():
    """Planning terminology is profile data (BRD §8): the templates stay place-neutral."""
    words = ("podgorica", "montenegr", "zauzetost", "izgrađenost", "spratnost", "stanovanj")
    for path in load_prompt_set().directory.rglob("*.md"):
        text = path.read_text(encoding="utf-8").casefold()
        for word in words:
            assert word not in text, (path.name, word)


# --- normalisation -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("printed", "value", "rules", "hint", "ambiguous"),
    [
        ("0,4", "0.4", ("decimal_comma",), None, False),
        ("1906.09", "1906.09", (), None, False),
        ("1.906,09", "1906.09", ("grouped_number", "decimal_comma"), None, False),
        ("5705.1 7", "5705.17", (), None, False),  # a table cell wrapped over two lines
        ("40 %", "40", (), "percent", False),
        ("83553,26 m²", "83553.26", ("decimal_comma",), "m2", False),
        ("46.83 ha", "46.83", (), "ha", False),
        ("1.500", "1.500", (), None, True),
    ],
)
def test_numbers_are_parsed_as_printed(printed, value, rules, hint, ambiguous):
    parsed = parse_number(printed)
    assert parsed is not None
    assert parsed.value == Decimal(value)
    assert parsed.rules == rules and parsed.unit_hint == hint and parsed.ambiguous is ambiguous


@pytest.mark.parametrize("printed", ["h/2", "P+4", "min. 3 m", "1,2,3.4.5", "UP 12", ""])
def test_words_and_formulas_are_not_numbers(printed):
    assert parse_number(printed) is None


def _norm(printed: str, unit: str | None, key: str):
    parsed = parse_number(printed)
    assert parsed is not None
    return normalise_number(parsed, unit, FIELD_SPECS[key])  # type: ignore[arg-type]


def test_unit_normalisation_is_exact_and_named():
    exact = _norm("0.29", "ratio", "max_site_coverage_pct")
    assert (exact.value, exact.unit, exact.rules, exact.flags) == (
        29.0,
        "%",
        ("ratio_to_percent",),
        (),
    )
    assert _norm("1", "ratio", "max_site_coverage_pct").value == 100.0
    assert _norm("40", "percent", "max_site_coverage_pct").value == 40.0
    assumed = _norm("0,4", "none", "max_site_coverage_pct")
    assert assumed.value == 40.0 and Flag.unit_assumed in assumed.flags
    assert Flag.unit_assumed in _norm("40", "none", "max_site_coverage_pct").flags
    assert Flag.unit_unexpected in _norm("1.5", "ratio", "max_site_coverage_pct").flags
    wrong = _norm("3", "m", "max_site_coverage_pct")
    assert wrong.unit == "m" and Flag.unit_unexpected in wrong.flags
    hectares = _norm("46.83", "ha", "planned_parcel_area_m2")
    assert (hectares.value, hectares.unit, hectares.rules) == (468300.0, "m²", ("ha_to_m2",))
    plan = _norm("46.83", "none", "area_ha")
    assert (plan.value, plan.unit) == (46.83, "ha") and Flag.unit_assumed in plan.flags
    assert (
        _norm("2,5", "ratio", "max_far").flags == ()
        and _norm("2,5", "none", "max_far").unit is None
    )
    assert Flag.unit_assumed in _norm("12", "none", "max_height_m").flags


@pytest.mark.parametrize(
    ("notation", "below", "above", "attic"),
    [
        ("Po+P+6", 1, 7, 0),
        ("S+P+3+ M", 1, 5, 1),
        ("G+S+P+5+Pk", 2, 7, 1),
        ("2Po+P+7", 2, 8, 0),
        ("3S+P+3", 3, 4, 0),
        ("P+4+Pk", 0, 6, 1),
        ("P", 0, 1, 0),
        ("S", 1, 0, 0),
        ("5", 0, 5, 0),
    ],
)
def test_floor_notation_is_counted_by_code(notation, below, above, attic):
    count = parse_floors(notation, CONVENTIONS.floor_tokens)
    assert count is not None
    assert (count.below_ground, count.above_ground, count.attic) == (below, above, attic)
    assert count.notation == notation


@pytest.mark.parametrize("notation", ["Pv", "P+Po", "6+P", "P+X", "", "četiri sprata"])
def test_unknown_floor_notation_is_not_derived(notation):
    assert parse_floors(notation, CONVENTIONS.floor_tokens) is None


def test_land_use_wording_maps_to_classes():
    rules = CONVENTIONS.land_use_rules
    expected = {
        "stanovanje sa djelatn.": LandUseClass.residential_mixed,
        "mješovita namjena - prostor namjenjen za razradu": LandUseClass.mixed_use,
        "stanovanje": LandUseClass.residential,
        "površine saobraćajne infrastrukture": LandUseClass.traffic_infrastructure,
        "školstvo i soc.zaštita": LandUseClass.education_social,
        "centralne djelatnosti": LandUseClass.central_activities,
        "POVRŠINE ZA SPORT I REKREACIJU": LandUseClass.sport_recreation,
        "zelene površine": LandUseClass.green_space,
    }
    for wording, category in expected.items():
        assert classify_land_use(wording, rules) == category, wording
    assert classify_land_use("SS", rules) is None  # a legend code without its legend
    legend = {"ss": "površine za stanovanje srednje gustine"}
    assert classify_land_use("SS", rules, legend) == LandUseClass.residential


def test_dates_and_lookup_keys():
    assert parse_date("29.11.2012") == date(2012, 11, 29)
    assert parse_date("20. 07. 2012. godine") == date(2012, 7, 20)
    for text in ("35/12", "31.02.2012", "1.1.2012 i 2.2.2013", "novembar 2012"):
        assert parse_date(text) is None
    assert [parcel_key(p, "UP") for p in ("UP 12", "UP 51(a)", "UP 82 a", "A116/1")] == [
        "12",
        "51(a)",
        "82a",
        "a116/1",
    ]
    words = ["ukupno", "blok", "zona"]
    assert [block_key(b, words) for b in ("Blok A", "UKUPNO BLOK F", "ukupno F", "Zona B")] == [
        "a",
        "f",
        "f",
        "b",
    ]


# --- finding text on the page ------------------------------------------------------------------


def test_text_is_found_on_whole_words_ignoring_layout_and_accents():
    assert [o.text for o in find_in("UP 21 1906.09 1", "1")] == ["1"]
    assert find_in("UP 21", "UP 2") == []
    assert [o.text for o in find_in("5705.1\n7", "5705.17")] == ["5705.1 7"]
    hit = find_in("za razradu konkursnim rješenjem", "konkursnim rjesenjem")
    assert [o.text for o in hit] == ["konkursnim rješenjem"]  # the page's text, not the copy
    assert find_in('Izmjena i dopuna DUP-a „Novi Grad" u Podgorici', 'DUP-a "Novi Grad"')


def test_a_repeated_value_is_boxed_by_its_row_and_column():
    a, b = (100.0, 500.0, 110.0, 510.0), (300.0, 500.0, 310.0, 510.0)
    below = (100.0, 450.0, 110.0, 460.0)
    row, column_a = [(20.0, 500.0, 40.0, 510.0)], [(95.0, 700.0, 115.0, 710.0)]
    assert pick_box([a, below], row, []) == a
    assert pick_box([a, b], row, []) is None  # same row, no column: ambiguous
    assert pick_box([a, b], row, column_a) == a
    assert pick_box([a, b], [], []) is None


# --- never guessed ------------------------------------------------------------------------------


@pytest.mark.parametrize("case", C.ABSENT_CASES, ids=lambda c: c.id)
def test_a_field_the_page_does_not_state_comes_back_null(case):
    pages = list(case.pages)
    result = build(case.task, case.faithful, pages)
    for where in case.absent:
        leaf = C.find_leaf(result, where)
        assert isinstance(leaf, MissingValue) and leaf.reason == "not_found", where
    for where, expected in case.stated.items():
        leaf = C.find_leaf(result, where)
        assert isinstance(leaf, StatedValue), where
        assert leaf.value == expected, where
    assert result.issues == []
    assert_cited_text_on_page(result, pages)


GROUNDING_CASES = [c for c in C.ABSENT_CASES if c.id != "parking_from_neighbour"]


@pytest.mark.parametrize("case", GROUNDING_CASES, ids=lambda c: c.id)
def test_a_guessed_value_never_survives_validation(case):
    """A computed, converted or invented value is not printed where the model says it is: the
    field comes back null (unverified) and the guess is kept as an issue, never dropped."""
    pages = list(case.pages)
    result = build(case.task, case.guessing, pages)
    for where in case.absent:
        assert isinstance(C.find_leaf(result, where), MissingValue), where
    removed = [w for w in case.absent if C.find_leaf(result, w).reason == "unverified"]  # type: ignore[union-attr]
    assert removed
    assert result.issues and all(i.severity == "error" for i in result.issues)
    assert {i.code for i in result.issues} <= {"value_not_in_raw_text", "raw_text_not_on_page"}
    assert all(i.candidate and i.candidate["value"] for i in result.issues)
    assert result.stats.missing["unverified"] == len(removed)
    assert_cited_text_on_page(result, pages)


def test_a_neighbours_value_is_flagged_for_the_reviewer():
    """A real value copied onto the wrong parcel passes grounding; its position gives it away."""
    case = CASES["parking_from_neighbour"]
    result = build(case.task, case.guessing, list(case.pages))
    copied = C.find_leaf(result, "parcel:8/rules.parking_requirement")
    own = C.find_leaf(result, "parcel:7/rules.parking_requirement")
    assert isinstance(copied, StatedValue) and Flag.found_under_other_parcel in copied.flags
    assert isinstance(own, StatedValue) and Flag.found_under_other_parcel not in own.flags


# --- flags and shapes ---------------------------------------------------------------------------


def test_low_confidence_is_kept_and_flagged_at_the_configured_threshold():
    data = parcels(
        C.parcel(
            UP14,
            max_site_coverage_pct=C.v(
                "0,40", "Maksimalni indeks zauzetosti: 0,40", page=4, unit="ratio", confidence=0.5
            ),
        )
    )
    flagged = C.find_leaf(
        build("urban_parcel", data, [PAGE]), "parcel:14/rules.max_site_coverage_pct"
    )
    assert isinstance(flagged, StatedValue) and flagged.value == 40.0
    assert Flag.low_confidence in flagged.flags and flagged.confidence == 0.5
    lenient = build("urban_parcel", data, [PAGE], low_confidence=0.4)
    leaf = C.find_leaf(lenient, "parcel:14/rules.max_site_coverage_pct")
    assert isinstance(leaf, StatedValue) and Flag.low_confidence not in leaf.flags


def test_implausible_values_are_kept_and_flagged():
    page = C.text_page(2, "UP 9\nIZ 150 %\nII 45\nPovršina 0 m2\nSpratnost P+70")
    number = C.v("UP 9", page=2)
    data = parcels(
        C.parcel(
            number,
            area=C.v("0 m2", "Površina 0 m2", page=2),
            max_site_coverage_pct=C.v("150 %", "IZ 150 %", page=2),
            max_far=C.v("45", "II 45", page=2, unit="ratio"),
            max_floors=C.v("P+70", "Spratnost P+70", page=2),
        )
    )
    result = build("urban_parcel", data, [page])
    for where in (
        "planned_parcel_area_m2",
        "rules.max_site_coverage_pct",
        "rules.max_far",
        "rules.max_floors",
    ):
        leaf = C.find_leaf(result, f"parcel:9/{where}")
        assert isinstance(leaf, StatedValue) and Flag.out_of_range in leaf.flags, where


def test_confidence_outside_0_1_is_an_issue_and_reads_as_0():
    data = parcels(
        C.parcel(
            UP14, max_far=C.v("1,6", "Maksimalni indeks izgrađenosti: 1,6", page=4, confidence=1.7)
        )
    )
    result = build("urban_parcel", data, [PAGE])
    leaf = C.find_leaf(result, "parcel:14/rules.max_far")
    assert isinstance(leaf, StatedValue) and leaf.confidence == 0.0
    assert Flag.low_confidence in leaf.flags
    assert [(i.code, i.severity) for i in result.issues] == [("malformed_leaf", "warning")]


def test_a_deferred_value_keeps_the_text_that_defers_it():
    page = C.text_page(1, "A\nUP 1\n5705.1\n7\ndefinisaće se konkursnim rješenjem")
    data = parcels(
        C.parcel(
            C.v("UP 1", page=1),
            area=C.v("5705.17", page=1),
            max_far=C.deferred("definisaće se konkursnim rješenjem", page=1, row="UP 1"),
            max_floors={**C.nf(), "absent_reason": "deferred"},  # no text: cannot be checked
        )
    )
    result = build("parameter_table", data, [page])
    far = C.find_leaf(result, "parcel:1/rules.max_far")
    assert isinstance(far, MissingValue) and far.reason == "deferred"
    assert (
        far.raw_text == "definisaće se konkursnim rješenjem" and far.source and far.source.page == 1
    )
    floors = C.find_leaf(result, "parcel:1/rules.max_floors")
    assert isinstance(floors, MissingValue) and floors.reason == "unverified"
    area = C.find_leaf(result, "parcel:1/planned_parcel_area_m2")
    assert isinstance(area, StatedValue) and area.value == 5705.17 and area.raw_text == "5705.1 7"


def test_malformed_leaves_never_become_values():
    data = parcels(
        C.parcel(
            UP14,
            max_far={**C.v("1,6", page=4), "page": None},  # no page
            max_height_m={**C.nf(), "absent_reason": None},  # null without a reason
            max_site_coverage_pct={
                **C.v("0,40", "Maksimalni indeks zauzetosti: 0,40", page=4),
                "absent_reason": "not_found",
            },  # a value that also says not_found
        )
    )
    result = build("urban_parcel", data, [PAGE])
    assert C.find_leaf(result, "parcel:14/rules.max_far").reason == "unverified"  # type: ignore[union-attr]
    assert C.find_leaf(result, "parcel:14/rules.max_height_m").reason == "not_found"  # type: ignore[union-attr]
    iz = C.find_leaf(result, "parcel:14/rules.max_site_coverage_pct")
    assert isinstance(iz, MissingValue) and iz.reason == "unverified"
    codes = [(i.code, i.severity) for i in result.issues]
    assert codes.count(("malformed_leaf", "error")) == 2 and ("malformed_leaf", "warning") in codes


def test_the_page_is_corrected_only_when_the_text_is_on_exactly_one_other_page():
    two, three, four = (C.text_page(n, t) for n, t in ((2, "UP 14"), (3, "II 1,6"), (4, "II 1,6")))
    far = C.v("1,6", "II 1,6", page=2)
    corrected = build(
        "urban_parcel", parcels(C.parcel(C.v("UP 14", page=2), max_far=far)), [two, three]
    )
    leaf = C.find_leaf(corrected, "parcel:14/rules.max_far")
    assert isinstance(leaf, StatedValue) and leaf.source.page == 3
    assert Flag.page_corrected in leaf.flags
    ambiguous = build(
        "urban_parcel", parcels(C.parcel(C.v("UP 14", page=2), max_far=far)), [two, three, four]
    )
    assert C.find_leaf(ambiguous, "parcel:14/rules.max_far").reason == "unverified"  # type: ignore[union-attr]
    assert [i.code for i in ambiguous.issues] == ["raw_text_not_on_page"]
    unread = build(
        "urban_parcel",
        parcels(C.parcel(C.v("UP 14", page=2), max_far=C.v("1,6", "II 1,7", page=9))),
        [two],
    )
    assert [i.code for i in unread.issues] == ["page_not_in_input"]


def test_words_in_a_number_field_are_kept_as_text_and_flagged():
    data = parcels(
        C.parcel(
            UP14, setback_neighbours_m=C.v("h/2", "Udaljenost od susjednih parcela: h/2", page=4)
        )
    )
    leaf = C.find_leaf(build("urban_parcel", data, [PAGE]), "parcel:14/rules.setback_neighbours_m")
    assert isinstance(leaf, StatedValue) and leaf.value == "h/2" and leaf.unit is None
    assert Flag.text_for_numeric_field in leaf.flags


def _grid_page() -> PageInput:
    page = C.text_page(2, "Blok\nBroj UP\nIndeks zauzetosti\nA\nUP 3\n0,45\nA\nUP 4\n0,45")
    box = {
        "r0c2": (300.0, 700.0, 380.0, 710.0),
        "r1c1": (100.0, 680.0, 130.0, 690.0),
        "r1c2": (300.0, 680.0, 330.0, 690.0),
        "r2c1": (100.0, 660.0, 130.0, 670.0),
        "r2c2": (300.0, 660.0, 330.0, 670.0),
    }
    texts = [["Blok", "Broj UP", "Indeks zauzetosti"], ["A", "UP 3", "0,45"], ["A", "UP 4", "0,45"]]
    rows = tuple(
        tuple(TableCell(f"r{r}c{c}", text, box.get(f"r{r}c{c}")) for c, text in enumerate(row))
        for r, row in enumerate(texts)
    )
    return replace(page, tables=(TableGrid("p2t1", rows),))


def test_a_cited_grid_cell_gives_the_exact_box():
    page = _grid_page()
    right = C.v("0,45", page=2, unit="ratio", row="UP 4", table="p2t1", cell="r2c2")
    wrong = C.v("0,45", page=2, unit="ratio", row="UP 3", table="p2t1", cell="r1c1")
    data = parcels(
        C.parcel(C.v("UP 4", page=2, row="UP 4"), max_site_coverage_pct=right),
        C.parcel(C.v("UP 3", page=2, row="UP 3"), max_site_coverage_pct=wrong),
    )
    result = build("parameter_table", data, [page])
    up4 = C.find_leaf(result, "parcel:4/rules.max_site_coverage_pct")
    assert isinstance(up4, StatedValue) and up4.source.bbox == (300.0, 660.0, 330.0, 670.0)
    assert up4.source.table_ref and up4.source.table_ref.cell == "r2c2"
    assert up4.extraction_method == "table" and up4.value == 45.0
    up3 = C.find_leaf(result, "parcel:3/rules.max_site_coverage_pct")
    assert isinstance(up3, StatedValue) and Flag.cell_mismatch in up3.flags
    assert up3.source.table_ref and up3.source.table_ref.cell is None


def test_ocr_pages_are_marked():
    page = replace(C.text_page(4, "Urbanistička parcela UP 14\nII 1,6"), method="ocr")
    data = parcels(C.parcel(UP14, max_far=C.v("1,6", "II 1,6", page=4)))
    leaf = C.find_leaf(build("urban_parcel", data, [page]), "parcel:14/rules.max_far")
    assert isinstance(leaf, StatedValue) and leaf.extraction_method == "ocr"


def test_document_codes_and_dates_are_normalised_with_the_printed_form_kept():
    page = C.text_page(
        1,
        "Detaljni urbanistički plan „Zabjelo 8“\n"
        "Odluka o donošenju broj 01-030/12-1441 od 29.11.2012. godine\n"
        "Vrsta: katalog XYZ",
    )
    decision = "Odluka o donošenju broj 01-030/12-1441 od 29.11.2012. godine"
    data = C.document(
        name=C.v("Detaljni urbanistički plan „Zabjelo 8“"),
        document_type=C.v("dup", "Detaljni urbanistički plan"),
        status=C.v("adopted", "Odluka o donošenju"),
        decision_number=C.v("01-030/12-1441", decision),
        decision_date=C.v("29.11.2012", decision),
    )
    result = build("document", data, [page])
    doc = result.document
    assert doc is not None
    assert isinstance(doc.document_type, StatedValue) and doc.document_type.value == "DUP"
    assert isinstance(doc.status, StatedValue) and doc.status.value == "adopted"
    assert isinstance(doc.decision_date, StatedValue)
    assert doc.decision_date.value == "2012-11-29" and doc.decision_date.normalisation == [
        "date_dmy"
    ]
    assert doc.decision_date.stated.value == "29.11.2012"
    unknown = build("document", C.document(document_type=C.v("XYZ", "katalog XYZ")), [page])
    leaf = unknown.document.document_type  # type: ignore[union-attr]
    assert isinstance(leaf, StatedValue) and Flag.unknown_code in leaf.flags


def test_a_legend_names_the_class_of_a_land_use_code():
    legend_page = C.text_page(
        1, "LEGENDA\nSS površine za stanovanje srednje gustine\nMN mješovita namjena"
    )
    legend = build(
        "land_use_legend",
        {
            "entries": [
                {"code": C.v("SS"), "name": C.v("površine za stanovanje srednje gustine")},
                {"code": C.v("MN"), "name": C.v("mješovita namjena")},
            ]
        },
        [legend_page],
    )
    assert [e.category for e in legend.land_use_legend] == [
        LandUseClass.residential,
        LandUseClass.mixed_use,
    ]
    table = C.text_page(3, "A\nA99\nSS")
    data = parcels(C.parcel(C.v("A99", page=3, row="A99"), land_use=C.v("SS", page=3, row="A99")))
    with_legend = build("parameter_table", data, [table], legend=legend_map(legend.land_use_legend))
    leaf = C.find_leaf(with_legend, "parcel:a99/rules.land_use")
    assert isinstance(leaf, StatedValue) and leaf.category == LandUseClass.residential
    assert leaf.value == "SS"  # the document's wording stays the value
    without = C.find_leaf(build("parameter_table", data, [table]), "parcel:a99/rules.land_use")
    assert isinstance(without, StatedValue) and Flag.land_use_unmapped in without.flags


def test_parcels_without_a_number_and_duplicates_are_reported():
    page = C.text_page(1, "UP 5\nII 1,2")
    far = C.v("1,2", "II 1,2", page=1)
    data = parcels(
        C.parcel(C.v("UP 5", page=1), max_far=far),
        C.parcel(C.v("UP 5", page=1), max_far=far),
        C.parcel(C.nf(), max_far=far),
    )
    result = build("urban_parcel", data, [page])
    assert sorted(i.code for i in result.issues) == [
        "duplicate_parcel",
        "duplicate_parcel",
        "parcel_without_number",
    ]


# --- staging -----------------------------------------------------------------------------------


def test_staging_rows_carry_the_contract_and_stay_pending():
    case = CASES["height_from_floors"]
    result = build(case.task, case.faithful, list(case.pages), document_id=12)
    plan = to_staging_rows(
        result, extracted_by="llm:claude-opus-5", parcel_ids={"21": 501}, block_ids={}, job_id=77
    )
    assert {r["field_key"] for r in plan.rows} == {
        "planned_parcel_area_m2",
        "land_use",
        "max_floors",
        "max_site_coverage_pct",
        "max_far",
    }
    for row in plan.rows:
        assert row["review_state"] == "pending_review"
        assert (row["entity_type"], row["urban_parcel_id"], row["document_id"]) == (
            "urban_parcel",
            501,
            12,
        )
        assert row["schema_version"] == SCHEMA_VERSION and row["prompt_version"] == PROMPT_VERSION
        assert row["extraction_method"] == "table" and row["job_id"] == 77
        assert row["extracted_by"] == "llm:claude-opus-5" and row["source_page"] == 7
        stored = read_payload(row["schema_version"], row["payload"])
        assert stored.field_key == row["field_key"] and stored.urban_parcel_number == "UP 21"
    coverage = next(r for r in plan.rows if r["field_key"] == "max_site_coverage_pct")
    assert (coverage["value_number"], coverage["value_text"], coverage["unit"]) == (50.0, None, "%")
    assert coverage["source_note"] == "UP 21 – Indeks zauzetosti" and coverage["flags"] == []
    floors = next(r for r in plan.rows if r["field_key"] == "max_floors")
    assert (floors["value_text"], floors["value_number"]) == ("P+4+M", None)
    # UP 22 has no geometry yet: reported, not staged, not dropped
    reasons = {(u.path.split(".")[0], u.reason) for u in plan.unstaged}
    assert ("urban_parcels[1]", "parcel_not_in_geometry") in reasons
    assert ("urban_parcels[0]", "not_a_planning_field") in reasons  # number and block label
    assert plan.missing == {"not_found": result.stats.missing["not_found"]}


def test_document_and_block_values_stage_at_their_scope():
    title = C.text_page(
        1,
        "Detaljni urbanistički plan „Blok 5“\nMaksimalna visina objekata u zahvatu plana je 21 m.",
    )
    height = "Maksimalna visina objekata u zahvatu plana je 21 m."
    data = C.document(
        name=C.v("Detaljni urbanistički plan „Blok 5“"),
        document_type=C.v("DUP", "Detaljni urbanistički plan"),
        rules=C.rules(max_height_m=C.v("21 m", height, unit="m")),
    )
    result = build("document", data, [title])
    plan = to_staging_rows(result, extracted_by="llm:x", parcel_ids={}, block_ids={})
    [row] = plan.rows
    assert (row["entity_type"], row["field_key"], row["value_number"]) == (
        "document",
        "max_height_m",
        21.0,
    )
    assert row["urban_parcel_id"] is None and row["block_id"] is None
    assert {u.reason for u in plan.unstaged} == {"not_a_planning_field"}  # name and type

    page = C.text_page(3, "ukupno F\n38432\n0.4\n1.6")
    blocks = {
        "urban_parcels": [],
        "blocks": [
            C.block(
                C.v("ukupno F", page=3, row="ukupno F"),
                total=True,
                area=C.v("38432", page=3),
                max_site_coverage_pct=C.v("0.4", page=3, unit="ratio"),
            )
        ],
        "columns": [],
    }
    block_result = build("parameter_table", blocks, [page])
    block_plan = to_staging_rows(
        block_result, extracted_by="llm:x", parcel_ids={}, block_ids={"f": 31}
    )
    [row] = block_plan.rows
    assert (row["entity_type"], row["block_id"], row["field_key"]) == (
        "block",
        31,
        "max_site_coverage_pct",
    )
    assert "aggregate_row" in row["flags"]
    missing_block = to_staging_rows(block_result, extracted_by="llm:x", parcel_ids={}, block_ids={})
    assert not missing_block.rows
    assert ("blocks[0].rules.max_site_coverage_pct", "block_not_in_geometry") in {
        (u.path, u.reason) for u in missing_block.unstaged
    }


# --- stored items ------------------------------------------------------------------------------


def test_a_stored_item_stays_readable_across_minor_versions():
    payload = json.loads((FIXTURES / "staged-item-v1.0.json").read_text(encoding="utf-8"))
    item = read_payload("1.0", payload)
    assert item.leaf.value == 100.0 and item.leaf.stated.value == "1"
    assert item.leaf.source.table_ref and item.leaf.source.table_ref.column == "Indeks zauzetosti"
    assert read_payload("1.9", payload) == item
    for version in ("2.0", "v1"):
        with pytest.raises(UnsupportedSchemaVersion):
            read_payload(version, payload)


# --- one request end to end --------------------------------------------------------------------


def test_run_task_sends_the_prompt_and_validates_the_reply():
    case = CASES["far_from_coverage_and_floors"]
    model = ScriptedModel([case.faithful])
    run = run_task(
        case.task,
        model=model,
        municipality_id=M,
        document=DocumentContext(3, "Test document"),
        pages=case.pages,
        prompt_version="1.0",
    )
    [(system, user, schema)] = model.calls
    assert system == run.prompt.system and user == run.prompt.user
    assert schema == load_prompt_set("1.0").response_schema("ParcelsResponse")
    assert run.result.model == "scripted" and run.result.prompt_version == "1.0"
    assert isinstance(C.find_leaf(run.result, "parcel:14/rules.max_far"), MissingValue)


def test_a_reply_outside_the_response_schema_is_rejected():
    case = CASES["far_from_coverage_and_floors"]
    with pytest.raises(ModelOutputInvalid):
        run_task(
            case.task,
            model=ScriptedModel([{"urban_parcels": [{"number": "UP 14"}]}]),
            municipality_id=M,
            document=DocumentContext(3, "Test document"),
            pages=case.pages,
        )


# --- the hand-labelled sample (client documents; this machine only) --------------------------


@pytest.mark.skipif(not SAMPLE.is_dir(), reason="the sample lives with the client documents")
def test_the_hand_labelled_sample_fits_the_schema_unchanged():
    pytest.importorskip("pymupdf")
    files = sorted(SAMPLE.glob("*.labels.json"))
    pages_by_source: dict[str, set[int]] = {}
    for path in files:
        labels = load_labels(path)
        pages_by_source.setdefault(labels.source, set()).add(labels.page)
        check = check_label_file(path, DEFAULT_DOCS, write=False)
        assert check.ok, check.problems
        assert ExtractionResult.model_validate_json(check.result.model_dump_json()) == check.result
        assert_cited_text_on_page(check.result, pages_for(labels, DEFAULT_DOCS))
    assert len(pages_by_source) == 2 and all(len(p) >= 2 for p in pages_by_source.values())
