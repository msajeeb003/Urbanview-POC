"""The extract_document job without a database: step order, change detection, the idempotency
key, staging unmatched targets, and one step with the validation retry (the error fed back),
transient retries with backoff and a refusal. The PostGIS path (runs, items, superseding,
idempotency through the API) is ``tests/integration/test_extraction_job_postgis.py``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.extraction.chunking import ChunkPlan, SectionRules, detect_sections, plan_chunks
from core.extraction.llm import ModelRefused, ModelUnavailable, ScriptedModel
from core.extraction.preprocess import PreprocessOptions, extract_pages
from core.extraction.prompts import PROMPT_VERSION, DocumentContext
from core.extraction.run import FEEDBACK, run_task
from core.extraction.runs import run_dedupe_key
from core.extraction.schema import ExtractionResult
from core.extraction.staging import TARGET_STAGED, TARGET_UNMATCHED, to_staging_rows
from core.extraction.steps import backoff_seconds
from jobs.base import TransientError
from jobs.extraction_runner import ExtractionRunner, ordered_steps, value_change
from tests.extraction_script import Transcriber, absent

pymupdf = pytest.importorskip("pymupdf")

OPTIONS = PreprocessOptions()
CONTEXT = DocumentContext(id=7, name="DUP Test", type="DUP")


@pytest.fixture(scope="module")
def doc():
    from tests.pdf_synthetic import planning_pdf

    pages = extract_pages(planning_pdf(), options=OPTIONS)
    detect_sections(pages, SectionRules.from_profile("podgorica"))
    return pages


@pytest.fixture(scope="module")
def chunks(doc):
    return plan_chunks(doc, OPTIONS)


def runner(model, **kwargs) -> tuple[ExtractionRunner, list[float]]:
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def preprocess(file_id: int) -> None:  # pragma: no cover - not reached here
        raise AssertionError("no PDF stage in unit tests")

    return (
        ExtractionRunner(
            None,  # type: ignore[arg-type] - _step touches no database
            None,
            municipality_id="podgorica",
            model=model,
            model_name="claude-test-model",
            options=OPTIONS,
            preprocess=preprocess,
            sleep=sleep,
            clock=lambda: datetime(2026, 10, 2, tzinfo=UTC),
            **kwargs,
        ),
        sleeps,
    )


def plan(chunk_id: str, tasks: list[str], page: int = 1) -> ChunkPlan:
    return ChunkPlan(id=chunk_id, pages=[page], tasks=tasks, tokens=10, priority=0)


def test_steps_read_the_legend_first_and_keep_the_plan_order():
    steps = ordered_steps(
        [
            plan("c001", ["parameter_table", "block"]),
            plan("c002", ["land_use_legend"]),
            plan("c003", []),
            plan("c004", ["infrastructure", "land_use_legend"]),
        ]
    )
    assert [(c.id, t) for c, t in steps] == [
        ("c002", "land_use_legend"),
        ("c004", "land_use_legend"),
        ("c001", "document"),  # no chunk suggested it: the first page reads the title
        ("c001", "parameter_table"),
        ("c001", "block"),
        ("c004", "infrastructure"),
    ]


def test_backoff_doubles_up_to_the_cap():
    assert [backoff_seconds(i, 2.0, 30.0) for i in range(6)] == [2, 4, 8, 16, 30, 30]


def test_change_against_the_previous_runs_item():
    row = {"value_number": 2.4, "value_text": None, "unit": None}
    previous = {
        "review_state": "approved",
        "value_number": 2.4,
        "value_text": None,
        "unit": None,
        "amended_value_number": None,
        "amended_value_text": None,
        "amended_unit": None,
    }
    assert value_change(None, row) == "new"
    assert value_change(previous, row) == "same"
    assert value_change({**previous, "value_number": 1.8}, row) == "changed"
    assert (
        value_change(
            {
                **previous,
                "review_state": "amended",
                "amended_value_number": 2.4,
                "value_number": 9.0,
            },
            row,
        )
        == "same"
    )  # the amended value counts
    assert value_change({**previous, "unit": "%"}, row) == "changed"
    text_row = {"value_number": None, "value_text": "Stanovanje  sa djelatnostima", "unit": None}
    text_prev = {**previous, "value_number": None, "value_text": "stanovanje sa djelatnostima"}
    assert value_change(text_prev, text_row) == "same"


def test_the_idempotency_key_names_content_and_versions():
    assert run_dedupe_key(12, "ab" * 32, "claude-sonnet-5") == (
        f"extract_document:document:12:sha256:{'ab' * 32}:model:claude-sonnet-5"
        f":prompt:{PROMPT_VERSION}:schema:1.0"
    )


def _parcel_result(doc, chunks) -> ExtractionResult:
    from core.extraction.chunking import chunk_pages

    model = Transcriber()
    return run_task(
        "parameter_table",
        model=model,
        municipality_id="podgorica",
        document=CONTEXT,
        pages=chunk_pages(doc, chunks[0]),
    ).result


def test_unmatched_parcels_are_staged_as_flagged_text_references(doc, chunks):
    result = _parcel_result(doc, chunks)
    assert [p.parcel_key for p in result.urban_parcels] == ["1", "2", "3"]
    skipped = to_staging_rows(result, extracted_by="llm:x", parcel_ids={}, block_ids={})
    assert skipped.rows == []  # the default: reported, not staged
    assert "parcel_not_in_geometry" in {u.reason for u in skipped.unstaged}
    staged = to_staging_rows(
        result,
        extracted_by="llm:x",
        parcel_ids={"1": 101},
        block_ids={},
        unmatched="stage",
        staged_parcels={"2"},
        run_id=5,
    )
    by_parcel = {}
    for row in staged.rows:
        by_parcel.setdefault(row["target_label"], row)
    assert by_parcel["UP 1"]["urban_parcel_id"] == 101
    assert TARGET_UNMATCHED not in by_parcel["UP 1"]["flags"]
    assert by_parcel["UP 2"]["urban_parcel_id"] is None
    assert TARGET_STAGED in by_parcel["UP 2"]["flags"]
    assert TARGET_UNMATCHED in by_parcel["UP 3"]["flags"]
    assert {r["target_key"] for r in staged.rows} == {"1", "2", "3"}
    assert {r["run_id"] for r in staged.rows} == {5}
    fields = {r["field_key"] for r in staged.rows if r["target_label"] == "UP 1"}
    assert fields == {
        "planned_parcel_area_m2",
        "land_use",
        "max_floors",
        "max_site_coverage_pct",
        "max_far",
    }


def test_run_task_feeds_the_validation_error_back(doc, chunks):
    from core.extraction.chunking import chunk_pages

    model = ScriptedModel(lambda system, user, schema: absent(schema))
    run_task(
        "block",
        model=model,
        municipality_id="podgorica",
        document=CONTEXT,
        pages=chunk_pages(doc, chunks[0]),
        feedback="blocks: field required",
    )
    user = model.calls[0][1]
    assert user.endswith(FEEDBACK.format(error="blocks: field required"))


async def test_a_step_is_asked_again_once_with_the_error_then_given_up(doc, chunks):
    model = Transcriber(invalid_pages=[1])
    extraction, _ = runner(model)
    outcome = await extraction._step(chunks[0], "parameter_table", doc, CONTEXT, {})
    assert outcome.status == "failed" and outcome.attempts == 2
    assert "invalid answer" in outcome.error and outcome.result is None
    first, second = (call[2] for call in model.calls)
    assert "rejected by the validator" not in first
    assert "rejected by the validator" in second and "urban_parcels" in second
    assert outcome.usage.input_tokens == 2000  # both rejected answers are counted


async def test_transient_errors_are_retried_with_backoff_then_raised(doc, chunks):
    flaky = Transcriber(fail_first=2, failure=lambda: ModelUnavailable("529 overloaded"))
    extraction, sleeps = runner(flaky, call_retries=2, retry_base_seconds=1.5)
    outcome = await extraction._step(chunks[0], "parameter_table", doc, CONTEXT, {})
    assert outcome.status == "done" and outcome.attempts == 3 and sleeps == [1.5, 3.0]
    assert [p.parcel_key for p in outcome.result.urban_parcels] == ["1", "2", "3"]

    down = Transcriber(fail_first=10, failure=lambda: ModelUnavailable("529 overloaded"))
    extraction, sleeps = runner(down, call_retries=1)
    with pytest.raises(TransientError, match="c001 parameter_table"):
        await extraction._step(chunks[0], "parameter_table", doc, CONTEXT, {})
    assert len(down.calls) == 2 and len(sleeps) == 1


async def test_a_refusal_fails_the_step_without_a_retry(doc, chunks):
    refusing = Transcriber(fail_first=1, failure=lambda: ModelRefused("declined"))
    extraction, sleeps = runner(refusing)
    outcome = await extraction._step(chunks[0], "block", doc, CONTEXT, {})
    assert outcome.status == "failed" and outcome.attempts == 1 and sleeps == []
    assert outcome.error == "ModelRefused: declined"


def test_the_synthetic_plan_reads_four_pages(chunks):
    steps = ordered_steps(chunks)
    assert {c.pages[0] for c, _ in steps} == {1, 2, 4, 6}  # 3 is a scan, 5 is blank
    assert len(steps) == 10  # 9 suggested + the document task on page 1
