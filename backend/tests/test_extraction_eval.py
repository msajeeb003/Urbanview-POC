"""The corpus evaluation: scoring outcomes, merged table cells, the regression check."""

from __future__ import annotations

import json

import pytest

from core.extraction import evalcli
from core.extraction.chunking import merged_from, render_table
from core.extraction.corpus import GoldValue, load_corpus
from core.extraction.harness import cost_usd
from core.extraction.llm import ModelUsage
from core.extraction.preprocess import DocumentPages, GridCell, PageTable
from core.extraction.schema import MissingValue, SourceRef, Stated, StatedValue
from core.extraction.scoring import compare


def stated(value, printed: str, page: int = 3, confidence: float = 0.9) -> StatedValue:
    return StatedValue(
        value=value,
        raw_text=printed,
        source=SourceRef(document_id=1, page=page),
        confidence=confidence,
        extraction_method="table",
        stated=Stated(value=printed),
    )


def gold(value, printed: str, page: int = 3) -> GoldValue:
    return GoldValue(printed=printed, value=value, page=page, text=printed)


@pytest.mark.parametrize(
    ("g", "leaf", "outcome", "page_ok"),
    [
        (gold(1906.09, "1906.09"), stated(1906.09, "1906.09"), "exact", True),
        (gold(1906.09, "1906.09"), stated(1906.09, "1906,09"), "tolerance", True),
        (gold(1906.09, "1906.09"), stated(1906.9, "1906.9"), "wrong", True),
        (gold(1906.09, "1906.09"), stated(1906.09, "1906.09", page=4), "exact", False),
        (gold("stanovanje", "stanovanje"), stated("Stanovanje", "Stanovanje"), "tolerance", True),
        (gold(1906.09, "1906.09"), MissingValue(reason="not_found"), "missing", None),
        (gold(1906.09, "1906.09"), MissingValue(reason="unverified"), "missing", None),
        (None, stated(40.0, "0.4"), "false_value", None),
        (None, MissingValue(reason="not_found"), "correct_blank", None),
        (None, None, "correct_blank", None),
    ],
)
def test_compare_outcomes(g, leaf, outcome, page_ok):
    assert compare(g, leaf) == (outcome, page_ok)


def test_deferred_gold_values():
    deferred = GoldValue(status="deferred", printed="", value=None, page=1, text="definisaće se")
    assert compare(deferred, stated(7.0, "7"))[0] == "false_value"  # a guess where it is deferred
    assert compare(deferred, MissingValue(reason="not_found"))[0] == "missing"
    assert compare(deferred, MissingValue(reason="deferred"))[0] == "exact"


def _table() -> PageTable:
    def c(row: int, col: int, text: str, box):
        return GridCell(id=f"r{row}c{col}", text=text, bbox=box)

    return PageTable(
        id="p6t1",
        bbox=(0, 0, 500, 600),
        method="lines",
        header_rows=1,
        columns=["Blok", "Broj UP", "Površina UP"],
        rows=[
            [
                c(0, 0, "Blok", (0, 510, 50, 530)),
                c(0, 1, "Broj UP", (50, 510, 100, 530)),
                c(0, 2, "Površina UP", (100, 510, 150, 530)),
            ],
            [
                c(1, 0, "D", (0, 483, 50, 510)),
                c(1, 1, "UP 51(a)", (50, 483, 100, 510)),
                c(1, 2, "16273.23", (100, 413, 150, 510)),
            ],
            [
                c(2, 0, "D", (0, 440, 50, 483)),
                c(2, 1, "UP 51(b)", (50, 440, 100, 483)),
                c(2, 2, "", None),
            ],
            [
                c(3, 0, "D", (0, 397, 50, 440)),
                c(3, 1, "UP 52", (50, 397, 100, 440)),
                c(3, 2, "", (100, 397, 150, 440)),
            ],
        ],
    )


def test_merged_cells_carry_to_the_rows_they_span():
    table = _table()
    assert merged_from(table, 2, 2) == 1  # printed once over UP 51(a) and UP 51(b)
    assert merged_from(table, 3, 2) is None  # its own (empty) cell: nothing stated
    assert merged_from(table, 1, 2) is None
    doc = DocumentPages(sha256="0" * 64, page_count=1, version="t", pages=[])
    view = render_table(table, doc)
    assert "r2 | c0: D | c1: UP 51(b) | c2: ^r1" in view
    assert "r3 | c0: D | c1: UP 52\n" in view


def test_cost_from_list_prices():
    usage = ModelUsage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000)
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(2.0 + 1.0 + 0.2)
    assert cost_usd("some-other-model", usage) is None


def _record(accuracy: float, hallucinated: int = 0, wrong_page: int = 0) -> dict:
    return {
        "documents": [
            {
                "document": "novi-grad-1-2",
                "accuracy": accuracy,
                "hallucinated": hallucinated,
                "wrong_page": wrong_page,
            }
        ]
    }


def test_regression_check(tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"documents": {"novi-grad-1-2": {"accuracy": 0.95, "wrong_page": 0}}})
    )
    assert evalcli.check(_record(0.95), baseline) == 0
    assert evalcli.check(_record(0.947), baseline) == 0  # within the half-point slack
    assert evalcli.check(_record(0.93), baseline) == 1
    assert evalcli.check(_record(0.99, hallucinated=1), baseline) == 1
    assert evalcli.check(_record(0.99, wrong_page=1), baseline) == 1


def test_corpus_manifest_and_gold_sets_agree():
    corpus = load_corpus()
    assert corpus.ids == ["novi-grad-1-2", "stara-varos"]
    for doc_id in corpus.ids:
        data = json.loads((evalcli.CORPUS_DIR / doc_id / "gold.json").read_text(encoding="utf-8"))
        assert data["sha256"] == corpus.get(doc_id).sha256
        keys = [p["key"] for p in data["parcels"]]
        assert len(keys) == len(set(keys)) and keys
        for parcel in data["parcels"]:
            assert set(parcel["values"]) == set(corpus.fields)
