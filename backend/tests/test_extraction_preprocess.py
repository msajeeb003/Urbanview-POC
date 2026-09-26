"""PDF pre-processing (core.extraction.preprocess / chunking / manifest) on a generated planning
PDF (tests/pdf_synthetic.py): text blocks and words with boxes in reading order, a parameter
table with its column headers (wrapped cells joined), a table continued on the next page with its
header repeated, scanned / blank pages flagged and never filled in, OCR only through a configured
backend, diacritics and Cyrillic unchanged, sections from headings and table headers, chunks
within a budget that never split a table, citations carrying page and box into the extraction
contract, page images, the manifest and its cache key. Also the acceptance run over the POC
documents when the client files are on this machine."""

from __future__ import annotations

import unicodedata
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from core.extraction import cases as C  # noqa: E402
from core.extraction.chunking import (  # noqa: E402
    SectionRules,
    chunk_pages,
    detect_sections,
    heading_level,
    plan_chunks,
)
from core.extraction.manifest import (  # noqa: E402
    build_manifest,
    dump_pages,
    is_current,
    load_pages,
)
from core.extraction.normalise import Conventions  # noqa: E402
from core.extraction.pages import render_pages  # noqa: E402
from core.extraction.preprocess import (  # noqa: E402
    PREPROCESS_VERSION,
    PreprocessOptions,
    RawBlock,
    RawLine,
    detect_script,
    extract_pages,
    iter_page_images,
)
from core.extraction.response import ParcelsResponse  # noqa: E402
from core.extraction.schema import StatedValue  # noqa: E402
from core.extraction.textmatch import find_in, fold  # noqa: E402
from core.extraction.validate import assemble  # noqa: E402
from tests import pdf_synthetic as S  # noqa: E402

RULES = SectionRules.from_profile("podgorica")
OPTIONS = PreprocessOptions()
POC = Path(__file__).resolve().parents[2] / "docs" / "gis" / "source"


@pytest.fixture(scope="module")
def pdf() -> bytes:
    return S.planning_pdf()


@pytest.fixture(scope="module")
def doc(pdf):
    pages = extract_pages(pdf)
    detect_sections(pages, RULES)
    return pages


def _chunk_for(chunks, page: int):
    return next(c for c in chunks if c.pages == [page])


# --- pages ---------------------------------------------------------------------------------------


def test_every_page_has_its_text_as_blocks_and_words_with_boxes(doc):
    assert doc.page_count == 6 and doc.version == PREPROCESS_VERSION and len(doc.sha256) == 64
    page = doc.page(1)
    assert (page.width, page.height, page.rotation) == (595.0, 842.0, 0)
    assert page.text == "\n".join(b.text for b in page.blocks)
    texts = [b.text for b in page.blocks]
    assert texts[:3] == [S.RUNNING, S.HEADING_1, "Parametri su dati po urbanističkim parcelama."]
    assert texts[-1] == S.NOTE  # reading order: the note below the table comes last
    for block in page.blocks:
        x0, y0, x1, y1 = block.bbox
        assert 0 <= x0 < x1 <= page.width and 0 <= y0 < y1 <= page.height
        start, end = block.words
        words = page.words[start:end]
        assert words and fold("".join(w.text for w in words)) == fold(block.text)
    # PDF points with the origin bottom-left: the running header sits highest on the page
    assert page.blocks[0].bbox[1] > page.blocks[-1].bbox[3]


def test_the_parameter_table_comes_out_with_its_column_headers(doc):
    [table] = doc.page(1).tables
    assert table.id == "p1t1" and table.method == "lines_strict" and table.header_rows == 1
    assert table.columns == S.HEADERS  # "Površin" + "a UP" joined: a word wrapped in its cell
    rows = [[c.text for c in row] for row in table.rows[1:]]
    assert rows[0] == ["A", "UP 1", "1906.09", *S.ROWS_1[0][3:]]  # the wrapped number too
    assert rows[1:] == S.ROWS_1[1:]
    assert all(c.bbox is not None and c.id == f"r1c{j}" for j, c in enumerate(table.rows[1]))
    assert {b.table for b in doc.page(1).blocks if "UP 2" in b.text} == {"p1t1"}  # row blocks


def test_a_table_running_onto_the_next_page_repeats_its_header(doc):
    [table] = doc.page(2).tables
    assert (table.header_rows, table.continues, table.header_from) == (0, "p1t1", "p1t1")
    assert table.columns == S.HEADERS
    assert [[c.text for c in row] for row in table.rows] == S.ROWS_2
    view = chunk_pages(doc, _chunk_for(plan_chunks(doc, OPTIONS), 2))[0].view
    assert (
        "continues p1t1; columns from p1t1 on page 1: c0 Blok | c1 Broj UP | c2 Površina UP" in view
    )
    assert "r0 | c0: B | c1: UP 4 | c2: 640,00" in view


def test_diacritics_and_cyrillic_survive_unchanged(doc):
    page = doc.page(1)
    assert S.DIACRITICS in page.text and "građevinska" in page.text
    assert S.CYRILLIC in doc.page(4).text and S.HEADING_4 in doc.page(4).text
    for p in doc.pages:
        assert unicodedata.is_normalized("NFC", p.text)
    assert (page.script, doc.page(4).script, doc.page(3).script) == ("latin", "mixed", "none")
    assert detect_script(S.CYRILLIC) == "cyrillic" and detect_script(S.NOTE) == "latin"
    assert fold("Инфраструктура") == fold("Infrastruktura")  # keywords match in either script


def test_a_scanned_page_is_flagged_and_never_filled_in(doc):
    scan, blank = doc.page(3), doc.page(5)
    assert (scan.scanned, scan.scanned_reason, scan.method) == (True, "no_text_layer", "none")
    assert scan.text == "" and scan.blocks == [] and scan.image_coverage == 1.0
    assert (blank.blank, blank.scanned, blank.method) == (True, False, "none")
    chunks = plan_chunks(doc, OPTIONS)
    assert {n for c in chunks for n in c.pages} == {1, 2, 4, 6}  # nothing made up for 3 and 5
    summary = build_manifest(
        doc, chunks, options=OPTIONS, page_data_key="k", created_at=datetime.now(UTC)
    ).summary
    assert (summary.scanned_pages, summary.unread_pages, summary.blank_pages) == ([3], [3], [5])
    assert summary.vector_pages == 4 and summary.ocr_pages == []


def test_an_image_page_with_a_stray_label_is_a_scan_but_a_drawing_is_not():
    document = pymupdf.open()
    page = document.new_page()
    scan = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 80), False)
    scan.clear_with(220)
    page.insert_image(page.rect, pixmap=scan)
    page.insert_text((40, 40), "7", fontsize=8)  # a page number stamped on a scan
    drawing = document.new_page()
    for i in range(20):
        drawing.draw_line((40, 40 + 20 * i), (500, 60 + 20 * i))
    pages = extract_pages(document.tobytes())
    stamped, lines = pages.pages
    assert (stamped.scanned, stamped.scanned_reason) == (True, "low_text_density")
    assert stamped.method == "none" and stamped.text == ""  # the stray label is not read either
    assert (lines.scanned, lines.blank, lines.method) == (False, False, "none")


class FakeOcr:
    name = "fake"

    def __init__(self) -> None:
        self.read_pages: list[int] = []

    def read(self, page):
        self.read_pages.append(page.number + 1)
        box = (40.0, 60.0, 260.0, 74.0)
        text = "Skenirana strana: UP 9 indeks zauzetosti 0,35"
        words = [(w, box) for w in text.split()]
        return [RawBlock(bbox=box, lines=[RawLine(text=text, bbox=box, words=words)])]


def test_ocr_runs_only_through_a_configured_backend(pdf):
    ocr = FakeOcr()
    pages = extract_pages(pdf, ocr=ocr)
    assert ocr.read_pages == [3]  # only the scan
    scan = pages.page(3)
    assert (scan.scanned, scan.method) == (True, "ocr") and "UP 9" in scan.text
    detect_sections(pages, RULES)
    chunks = plan_chunks(pages, OPTIONS)
    [page_input] = chunk_pages(pages, _chunk_for(chunks, 3))
    assert page_input.method == "ocr" and page_input.words
    summary = build_manifest(
        pages, chunks, options=OPTIONS, page_data_key="k", created_at=datetime.now(UTC)
    ).summary
    assert (summary.scanned_pages, summary.ocr_pages, summary.unread_pages) == ([3], [3], [])


def test_glyph_shifted_cad_labels_are_decoded(doc):
    page = doc.page(6)
    assert page.glyph_decoded and "površine" in page.text and S.SHIFTED not in page.text
    assert [b.decoded for b in page.blocks] == [False, True]


# --- sections and chunks -------------------------------------------------------------------------


def test_sections_come_from_headings_and_table_headers(doc):
    one, two = doc.page(1), doc.page(2)
    assert [(h.text, h.level, h.section) for h in one.headings] == [(S.HEADING_1, 1, "regulation")]
    assert {"urban_parcels", "regulation"} <= set(one.tables[0].sections)  # Broj UP, indeks
    assert "infrastructure" in two.sections and two.headings[0].section == "infrastructure"
    assert doc.page(4).headings[0].section == "infrastructure"  # a Cyrillic heading
    assert all(h.text != S.RUNNING for p in doc.pages for h in p.headings)  # running header
    assert doc.page(3).sections == [] and doc.page(5).sections == []
    assert [heading_level(t) for t in ("4. A", "4.2 A", "4.2.1. A", "IV. A", "a) b", "A")] == [
        1,
        2,
        3,
        1,
        3,
        None,
    ]


def test_chunks_are_one_per_page_planning_first_and_never_split_a_table(doc):
    chunks = plan_chunks(doc, OPTIONS)
    assert [c.pages for c in chunks] == [[1], [2], [4], [6]]
    assert all(c.priority == 0 and not c.over_budget for c in chunks)
    first = chunks[0]
    assert first.tables == ["p1t1"] and "parameter_table" in first.tasks
    assert first.blocks == ["p1b1", "p1b2", "p1b3"] + [
        b.id for b in doc.page(1).blocks if b.text == S.NOTE
    ]
    small = plan_chunks(doc, replace(OPTIONS, chunk_token_budget=40))
    page_one = [c for c in small if c.pages == [1]]
    assert len(page_one) > 1
    [with_table] = [c for c in page_one if c.tables]
    assert with_table.tables == ["p1t1"] and with_table.over_budget  # whole, even over budget
    covered = [b for c in page_one for b in c.blocks]
    assert sorted(covered) == sorted(b.id for b in doc.page(1).blocks if b.table is None)


def test_chunk_pages_carry_page_and_box_into_the_extraction_contract(doc):
    [page] = chunk_pages(doc, _chunk_for(plan_chunks(doc, OPTIONS), 2))
    assert page.page == 2 and "UP 4" in page.text and "640,00" in page.text
    assert page.words and all(len(w.bbox) == 4 for w in page.words)
    [grid] = page.tables
    cell = page.cell("r0c2", "p2t1")
    assert grid.id == "p2t1" and cell is not None and cell.text == "640,00"
    assert render_pages([page]).startswith("=== Page 2 ===\n")
    response = ParcelsResponse.model_validate(
        {
            "urban_parcels": [
                C.parcel(
                    C.v("UP 4", page=2, row="UP 4", table="p2t1", cell="r0c1"),
                    area=C.v("640,00", page=2, unit="m2", row="UP 4", table="p2t1", cell="r0c2"),
                    max_site_coverage_pct=C.v(
                        "0,45", page=2, unit="ratio", row="UP 4", column="Indeks zauzetosti"
                    ),
                )
            ]
        }
    )
    result = assemble(
        "parameter_table",
        response,
        pages=[page],
        document_id=3,
        municipality_id="podgorica",
        conventions=Conventions.from_profile("podgorica"),
        prompt_version="1.0",
    )
    area = C.find_leaf(result, "parcel:4/planned_parcel_area_m2")
    assert isinstance(area, StatedValue) and area.value == 640.0
    assert area.source.page == 2 and area.source.bbox == cell.bbox  # the cell's box
    coverage = C.find_leaf(result, "parcel:4/rules.max_site_coverage_pct")
    assert isinstance(coverage, StatedValue) and coverage.source.bbox is not None
    assert find_in(page.text, coverage.raw_text) and result.issues == []


# --- page images, manifest ------------------------------------------------------------------------


def test_page_images_are_png_and_large_sheets_are_capped(pdf):
    images = list(iter_page_images(pdf, dpi=72, max_pixels=10**9))
    assert [n for n, _, _ in images] == [1, 2, 3, 4, 5, 6]
    assert all(png.startswith(b"\x89PNG\r\n\x1a\n") and dpi == 72 for _, png, dpi in images)
    _, _, capped = next(iter_page_images(pdf, dpi=300, max_pixels=100_000))
    assert capped < 300


def test_the_manifest_round_trips_and_keys_the_cache(doc):
    chunks = plan_chunks(doc, OPTIONS)
    manifest = build_manifest(
        doc, chunks, options=OPTIONS, page_data_key="k", created_at=datetime.now(UTC)
    )
    assert [c.id for c in manifest.chunks] == [c.id for c in chunks]
    assert manifest.tables[1].header_from == "p1t1" and manifest.pages[2].scanned
    assert manifest.summary.sections["infrastructure"] == [2, 4, 6]
    assert load_pages(dump_pages(doc)) == doc
    assert is_current(manifest, doc.sha256, OPTIONS)
    assert not is_current(manifest, "0" * 64, OPTIONS)
    assert not is_current(manifest, doc.sha256, replace(OPTIONS, chunk_token_budget=100))
    assert not is_current(None, doc.sha256, OPTIONS)


def test_selected_pages_only(pdf):
    pages = extract_pages(pdf, pages=[2])
    assert pages.page_count == 1 and pages.pages[0].number == 2
    assert pages.pages[0].tables[0].continues is None  # nothing before it was read


# --- the POC documents (client files; this machine only) --------------------------------------

NOVI_GRAD = "02 Novi Grad 1 i 2/Novi Grad 1 i 2 - Urban parcels - Planning parameters.pdf"
STARA_VAROS = "03 Stara Varoš/03 Stara Varoš - Urban parcels - Planning parameters.pdf"


@pytest.mark.skipif(not (POC / NOVI_GRAD).is_file(), reason="client documents not on this machine")
def test_the_poc_parameter_tables_come_out_with_their_headers():
    novi_grad = extract_pages(POC / NOVI_GRAD)
    assert novi_grad.pages[0].tables[0].columns == [
        "Blok",
        "Broj UP",
        "Površina UP",
        "Namjena objekta",
        "Površina prizemlja objekta",
        "Spratnost objekta",
        "Bruto građevinska površina",
        "Površina stanovanja",
        "Površina poslovanja",
        "Broj stamb. jedinica",
        "Broj zaposlenih",
        "Indeks zauzetosti",
        "Indeks izgrađenosti",
    ]
    assert novi_grad.pages[1].tables[0].header_from == "p1t1"  # block A runs onto page 2
    stara_varos = extract_pages(POC / STARA_VAROS, pages=[1, 57])
    columns = stara_varos.pages[0].tables[0].columns
    assert columns[:4] == ["Zona", "broj UP", "namjena", "P parcele"]
    assert "iz - indeks zauzetosti" in columns and "Ii – indeks izgrađenosti" in columns
    for doc in (novi_grad, stara_varos):
        assert all(not p.scanned and p.blocks and p.words for p in doc.pages)
        assert all(p.script == "latin" for p in doc.pages)
    text = novi_grad.pages[0].text  # č ć š ž đ as printed ("građevinsk|a" wraps in its cell)
    assert all(word in text for word in ("građevinsk", "Površina", "rješenjem", "definisaće"))


@pytest.mark.skipif(not (POC / NOVI_GRAD).is_file(), reason="client documents not on this machine")
def test_every_poc_sheet_gives_text_with_boxes():
    for path in sorted(POC.glob("0[23]*/*.pdf")):
        pages = extract_pages(path, pages=[1])
        page = pages.pages[0]
        assert page.blocks and page.words and not page.scanned, path.name
        assert all(0 <= b.bbox[0] <= b.bbox[2] <= page.width + 1 for b in page.blocks), path.name
