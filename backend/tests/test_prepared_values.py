"""Prepared planning values (``core.extraction.prepared``): the data built from a gold set and
the PDF stage's grids, and the committed data files against the corpus gold sets."""

from __future__ import annotations

import hashlib

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
from core.extraction.prepared import (
    PLANNING_FIELDS,
    build_prepared,
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


@pytest.mark.parametrize("document_id", load_corpus().ids)
def test_the_committed_data_files_state_what_the_gold_sets_state(document_id):
    """``prepared build`` after a gold set changes: the data file must follow it."""
    corpus = load_corpus()
    doc = corpus.get(document_id)
    gold = load_gold(document_id)
    prepared, _ = read_prepared(prepared_path(corpus.municipality, document_id))

    assert prepared.source.sha256 == doc.sha256 == gold.sha256
    assert [p.key for p in prepared.parcels] == [p.key for p in gold.parcels]
    for ours, theirs in zip(prepared.parcels, gold.parcels, strict=True):
        stated = {
            name: value
            for name in PLANNING_FIELDS
            if (value := theirs.values.get(name)) is not None and value.status == "stated"
        }
        assert set(ours.values) == set(stated), theirs.number
        for name, value in stated.items():
            mine = ours.values[name]
            assert (mine.value, mine.unit, mine.page, mine.printed, mine.cell) == (
                value.value,
                value.unit,
                value.page,
                value.printed,
                value.cell,
            ), (theirs.number, name)
            assert mine.bbox is not None and mine.note
