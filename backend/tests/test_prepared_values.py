"""Prepared planning values (``core.extraction.prepared``): the data built from a gold set and
the PDF stage's grids, and the committed data files against the corpus gold sets."""

from __future__ import annotations

import hashlib
import re

import pytest

from core.extraction.corpus import (
    ColumnSpec,
    CorpusDocument,
    GoldValue,
    HeaderSpec,
    Labelling,
    build_gold,
    load_corpus,
    load_gold,
)
from core.extraction.normalise import Conventions, parcel_key
from core.extraction.prepared import (
    PLANNING_FIELDS,
    PreparedParcel,
    PreparedValue,
    build_prepared,
    merge_building_rows,
    prepared_path,
    read_prepared,
    save_prepared,
)

FIELDS = ["block_ref", *PLANNING_FIELDS]


def synthetic_document(pdf: bytes) -> CorpusDocument:
    """The corpus entry of ``tests.pdf_synthetic.planning_pdf`` (its parameter table's columns)."""
    return CorpusDocument(
        id="dup-test",
        name="Izmjene i dopune DUP-a „Test” u Podgorici",
        type="DUP",
        parameters="plans/DUP Test - parameters.pdf",
        sha256=hashlib.sha256(pdf).hexdigest(),
        pages=6,
        labelling=Labelling(
            number=HeaderSpec(header="broj up"),
            block=HeaderSpec(header="blok"),
            columns=[
                ColumnSpec(field="planned_parcel_area_m2", header="povrsina up"),
                ColumnSpec(field="land_use", header="namjena", unit="words"),
                ColumnSpec(field="max_floors", header="spratnost", unit="words"),
                ColumnSpec(field="max_site_coverage_pct", header="indeks zauzetosti", unit="ratio"),
                ColumnSpec(field="max_far", header="indeks izgra", match="prefix"),
            ],
        ),
    )


def synthetic_prepared(pdf: bytes | None = None):
    """(document, gold, pages) of the synthetic plan: what ``prepared build`` starts from."""
    from core.extraction.preprocess import extract_pages
    from tests.pdf_synthetic import planning_pdf

    pdf = pdf or planning_pdf()
    doc = synthetic_document(pdf)
    pages = extract_pages(pdf)
    gold = build_gold(doc, pages, FIELDS, municipality="podgorica", labelled_by="test")
    return doc, gold, pages


def test_the_stated_values_come_with_their_cell_page_and_column():
    pytest.importorskip("pymupdf")
    doc, gold, pages = synthetic_prepared()
    prepared, problems = build_prepared(
        doc, gold, pages, municipality="podgorica", prepared_on="2026-10-01"
    )

    assert problems == []
    assert [p.number for p in prepared.parcels] == ["UP 1", "UP 2", "UP 3", "UP 4", "UP 5"]
    assert prepared.value_count == 25 and prepared.fields == [
        "planned_parcel_area_m2",
        "land_use",
        "max_site_coverage_pct",
        "max_far",
        "max_floors",
    ]
    assert (prepared.source.file, prepared.source.sha256) == (
        "DUP Test - parameters.pdf",
        doc.sha256,
    )
    first = prepared.parcels[0]
    assert (first.key, first.block_ref, first.page) == ("1", "A", 1)
    area = first.values["planned_parcel_area_m2"]
    assert (area.value, area.printed, area.page) == (1906.09, "1906.09", 1)  # wrapped in its cell
    assert area.note == "UP 1 – Površina UP"
    assert first.values["max_site_coverage_pct"].value == 40.0  # printed as the ratio 0,40
    assert first.values["max_site_coverage_pct"].unit == "%"
    assert first.values["max_far"].value == 2.4
    assert first.values["max_floors"].value == "Po+P+6"  # the notation stays the value
    assert first.values["land_use"].value == "stanovanje sa djelatnostima"
    page = pages.pages[0]
    for value in first.values.values():  # the box of the cell, inside the page
        x0, y0, x1, y1 = value.bbox
        assert 0 <= x0 < x1 <= page.width and 0 <= y0 < y1 <= page.height
    # the table continues on page 2 without its header: the column names are the first page's
    fourth = prepared.parcels[3]
    assert fourth.page == 2 and fourth.values["max_far"].note == "UP 4 – Indeks izgrađenosti"
    assert "no model" in prepared.basis and prepared.prepared_on == "2026-10-01"


def test_blank_deferred_and_untyped_values_are_not_prepared():
    pytest.importorskip("pymupdf")
    doc, gold, pages = synthetic_prepared()
    one, two, three = gold.parcels[:3]
    one.values["max_far"] = GoldValue(
        status="deferred", printed="", value=None, page=1, text="definisaće se konkursom"
    )
    two.values["land_use"] = None
    three.values["max_far"].value = "prema uslovima"  # a number field the reader could not type
    gold.parcels[3].values["max_floors"].cell = "r99c4"

    prepared, problems = build_prepared(doc, gold, pages, municipality="podgorica")

    assert "max_far" not in prepared.parcels[0].values
    assert "land_use" not in prepared.parcels[1].values
    assert "max_far" not in prepared.parcels[2].values
    floors = prepared.parcels[3].values["max_floors"]
    assert floors.value == "P+3" and floors.bbox is None  # kept, without a box
    assert len(problems) == 2
    assert "is not a number" in problems[0] and "not in the page's grid" in problems[1]


def test_a_data_file_reads_back_with_a_stable_checksum(tmp_path):
    pytest.importorskip("pymupdf")
    doc, gold, pages = synthetic_prepared()
    prepared, _ = build_prepared(
        doc, gold, pages, municipality="podgorica", prepared_on="2026-10-01"
    )

    path = save_prepared(prepared, tmp_path)
    again, checksum = read_prepared(path)

    assert path == prepared_path("podgorica", "dup-test", tmp_path)
    assert again == prepared
    assert checksum == hashlib.sha256(path.read_bytes()).hexdigest()
    assert save_prepared(again, tmp_path).read_bytes() == path.read_bytes()


def _value(value, page=1, box=(10.0, 20.0, 30.0, 40.0), note=None, unit=None):
    return PreparedValue(
        value=value, unit=unit, printed=str(value), page=page, bbox=box, cell="r1c1", note=note
    )


def test_building_rows_become_one_parcel():
    """Rows "UP 7(a)", "UP 7(b)", "UP 7(c)": the buildings of UP 7. The parcel takes what the rows
    share; the floors, different per building, are listed as printed with the cells' common box."""
    shared = {
        "planned_parcel_area_m2": _value(1200.5, unit="m²", note="{n} – Površina UP"),
        "land_use": _value("školstvo", note="{n} – Namjena objekta"),
        "max_far": _value(0.5, note="{n} – Indeks izgrađenosti"),
    }

    def row(number, floors, box):
        values = {
            name: value.model_copy(update={"note": value.note.format(n=number)})
            for name, value in shared.items()
        }
        if floors:
            values["max_floors"] = _value(floors, box=box, note=f"{number} – Spratnost objekta")
        return PreparedParcel(
            number=number,
            key=number.lower(),
            page=1,
            block_ref="D",
            values=values,
        )

    rows = [
        row("UP 6", "P+2", (50.0, 300.0, 90.0, 320.0)),
        row("UP 7(a)", "Po+P+3", (50.0, 260.0, 90.0, 280.0)),
        row("UP 7(b)", "Pv", (50.0, 220.0, 90.0, 240.0)),
        row("UP 7(c)", "", (0.0, 0.0, 0.0, 0.0)),  # this building states no floors
        row("UP 8 a", "P+1", (50.0, 180.0, 90.0, 200.0)),  # a parcel of its own, not a building
    ]

    merged, problems = merge_building_rows(rows, r"^(UP\s*\d+)\(([a-z])\)$", "UP")

    assert problems == []
    assert [(p.number, p.key) for p in merged] == [
        ("UP 6", "up 6"),
        ("UP 7", "7"),
        ("UP 8 a", "up 8 a"),
    ]
    assert merged[0] is rows[0] and merged[2] is rows[4]  # other rows are left as they are
    seven = merged[1]
    assert (seven.page, seven.block_ref) == (1, "D")
    assert list(seven.values) == ["planned_parcel_area_m2", "land_use", "max_far", "max_floors"]
    area = seven.values["planned_parcel_area_m2"]
    assert (area.value, area.unit, area.cell, area.note) == (
        1200.5,
        "m²",
        "r1c1",
        "UP 7 – Površina UP",
    )
    assert seven.values["land_use"].note == "UP 7 – Namjena objekta"
    floors = seven.values["max_floors"]
    assert floors.value == floors.printed == "(a) Po+P+3, (b) Pv"
    assert (floors.unit, floors.cell, floors.page) == (None, None, 1)
    assert floors.bbox == (50.0, 220.0, 90.0, 280.0)  # the two cells together
    assert floors.note == "UP 7(a), (b) – Spratnost objekta"

    # a number that differs from building to building is no parcel value; rows on two pages
    # are listed without a box
    rows[2].values["max_far"] = _value(0.8, note="UP 7(b) – Indeks izgrađenosti")
    rows[2].values["max_floors"] = _value("Pv", page=2, note="UP 7(b) – Spratnost objekta")
    merged, problems = merge_building_rows(rows, r"^(UP\s*\d+)\(([a-z])\)$", "UP")
    assert "max_far" not in merged[1].values and merged[1].values["max_floors"].bbox is None
    assert len(problems) == 2 and "different numbers" in problems[0] and "one page" in problems[1]


def _expected_parcels(doc, gold, abbreviation):
    """What a committed data file must hold for a gold set: one entry per row, the building rows
    of a parcel (``[document.prepared] building_rows``) as that one parcel."""
    rule = re.compile(doc.prepared.building_rows) if doc.prepared.building_rows else None
    out: list[tuple[str, list[tuple[str | None, object]]]] = []
    index: dict[str, int] = {}
    for parcel in gold.parcels:
        found = rule.match(parcel.number) if rule else None
        if found is None:
            out.append((parcel.key, [(None, parcel)]))
            continue
        key = parcel_key(found.group(1), abbreviation)
        if key not in index:
            index[key] = len(out)
            out.append((key, []))
        out[index[key]][1].append((found.group(2), parcel))
    return out


@pytest.mark.parametrize("document_id", load_corpus().ids)
def test_the_committed_data_files_state_what_the_gold_sets_state(document_id):
    """``prepared build`` after a gold set changes: the data file must follow it."""
    corpus = load_corpus()
    doc = corpus.get(document_id)
    gold = load_gold(document_id)
    prepared, _ = read_prepared(prepared_path(corpus.municipality, document_id))
    abbreviation = Conventions.from_profile(corpus.municipality).parcel_abbreviation
    expected = _expected_parcels(doc, gold, abbreviation)

    assert prepared.source.sha256 == doc.sha256 == gold.sha256
    assert [p.key for p in prepared.parcels] == [key for key, _ in expected]
    for ours, (_, rows) in zip(prepared.parcels, expected, strict=True):
        stated: dict[str, list[tuple[str | None, object]]] = {}
        for mark, theirs in rows:
            for name in PLANNING_FIELDS:
                value = theirs.values.get(name)
                if value is not None and value.status == "stated":
                    stated.setdefault(name, []).append((mark, value))
        assert set(ours.values) == set(stated), ours.number
        for name, values in stated.items():
            mine = ours.values[name]
            _, value = values[0]
            if len({(v.value, v.unit) for _, v in values}) > 1:
                # the buildings of one parcel state it differently: listed per building
                listing = ", ".join(f"({mark}) {v.printed}" for mark, v in values)
                assert (mine.value, mine.unit, mine.page, mine.printed, mine.cell) == (
                    listing,
                    None,
                    value.page,
                    listing,
                    None,
                ), (ours.number, name)
            else:
                assert (mine.value, mine.unit, mine.page, mine.printed, mine.cell) == (
                    value.value,
                    value.unit,
                    value.page,
                    value.printed,
                    value.cell,
                ), (ours.number, name)
            assert mine.bbox is not None and mine.note and mine.note.startswith(ours.number)


def test_novi_grads_building_rows_are_three_parcels():
    """UP 51, 81 and 85 are one parcel each on the drawing; the table lists their buildings."""
    corpus = load_corpus()
    prepared, _ = read_prepared(prepared_path(corpus.municipality, "novi-grad-1-2"))
    by_number = {p.number: p for p in prepared.parcels}

    assert not [n for n in by_number if "(" in n]
    assert len(prepared.parcels) == 93 and prepared.value_count == 450
    school = by_number["UP 51"]
    assert school.key == "51" and school.block_ref == "D"
    assert {k: v.value for k, v in school.values.items()} == {
        "planned_parcel_area_m2": 16273.23,
        "land_use": "školstvo i soc.zaštita",
        "max_site_coverage_pct": 20.0,
        "max_far": 0.5,
        "max_floors": "(a) Po+P+3, (b) Pv, (c) P+1",
    }
    assert school.values["max_floors"].note == "UP 51(a), (b), (c) – Spratnost objekta"
    assert by_number["UP 81"].values["max_floors"].value == (
        "(a) Po+P+4, (b) Po+P+4, (c) Po+P+4, (d) Po+P+1"
    )
    assert by_number["UP 85"].values["max_floors"].value == (
        "(a) P+1, (b) P, (c) P+1, (d) P, (e) P+1, (f) P"
    )
    assert by_number["UP 85"].values["planned_parcel_area_m2"].value == 16545.83
    assert {"UP 82 a", "UP 82 b", "UP 82 c"} <= set(by_number)  # parcels of their own
