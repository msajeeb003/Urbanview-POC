"""Evaluating a model against the contract (``python -m core.extraction eval``).

- :func:`run_absent_cases`: the "never guessed" cases (``core.extraction.cases``). A case passes
  only when the model itself returns null (``not_found``) for every absent field: a value the
  validator had to remove (``unverified``) never reaches the review queue, but it is still a
  failure of the model and counts as one.
- :func:`run_sample`: every hand-labelled page through the model, scored against its labels
  (matched / missed / wrong / extra, and values removed because their text was not on the page).
  Every value the model returns carries a page that contains its raw text: the validator keeps
  no other.

A live run needs Anthropic credentials and costs tokens; ``--dry-run`` replays the faithful
responses through the same pipeline instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.extraction.cases import ABSENT_CASES, find_leaf
from core.extraction.llm import ClaudeModel, ModelUsage, StructuredModel
from core.extraction.prompts import DocumentContext
from core.extraction.run import run_task
from core.extraction.sample import Score, compare, load_labels, pages_for, result_from_labels
from core.extraction.schema import Leaf, MissingValue, StatedValue


def model_from_settings() -> ClaudeModel:
    from core.config import get_settings

    settings = get_settings()
    key = settings.anthropic_api_key
    return ClaudeModel(
        settings.extraction_model,
        api_key=key.get_secret_value() if key else None,
        effort=settings.extraction_effort,
        adaptive_thinking=settings.extraction_adaptive_thinking,
        max_tokens=settings.extraction_max_tokens,
        refusal_fallback=settings.extraction_refusal_fallback,
        timeout_seconds=settings.extraction_timeout_seconds,
        base_url=settings.anthropic_base_url,
    )


def describe(leaf: Leaf | None) -> str:
    if leaf is None:
        return "entity missing"
    if isinstance(leaf, MissingValue):
        return leaf.reason
    return f"stated: {leaf.stated.value}"


def _same(leaf: Leaf | None, expected: float | str) -> bool:
    if not isinstance(leaf, StatedValue):
        return False
    if isinstance(expected, float):
        return isinstance(leaf.value, float) and abs(leaf.value - expected) < 1e-9
    return " ".join(str(leaf.value).split()).casefold() == expected.casefold()


@dataclass(slots=True)
class CaseOutcome:
    case: str
    trap: str
    passed: bool
    absent: dict[str, str]
    stated: dict[str, bool]
    issues: list[str] = field(default_factory=list)
    usage: ModelUsage = field(default_factory=ModelUsage)


def run_absent_cases(
    model: StructuredModel,
    *,
    municipality_id: str = "podgorica",
    low_confidence: float = 0.7,
) -> list[CaseOutcome]:
    outcomes = []
    for case in ABSENT_CASES:
        run = run_task(
            case.task,
            model=model,
            municipality_id=municipality_id,
            document=DocumentContext(id=1, name="Test document (never-guessed case)"),
            pages=case.pages,
            low_confidence=low_confidence,
        )
        absent = {where: describe(find_leaf(run.result, where)) for where in case.absent}
        outcomes.append(
            CaseOutcome(
                case=case.id,
                trap=case.trap,
                passed=all(state == "not_found" for state in absent.values()),
                absent=absent,
                stated={
                    where: _same(find_leaf(run.result, where), expected)
                    for where, expected in case.stated.items()
                },
                issues=[f"{i.path}: {i.code}" for i in run.result.issues],
                usage=run.reply.usage,
            )
        )
    return outcomes


@dataclass(slots=True)
class SampleOutcome:
    name: str
    score: Score
    usage: ModelUsage


def run_sample(
    model: StructuredModel, sample_dir: Path, docs_root: Path, *, low_confidence: float = 0.7
) -> list[SampleOutcome]:
    outcomes = []
    for path in sorted(sample_dir.glob("*.labels.json")):
        labels = load_labels(path)
        pages = pages_for(labels, docs_root)
        run = run_task(
            labels.task,
            model=model,
            municipality_id=labels.municipality,
            document=labels.document,
            pages=pages,
            low_confidence=low_confidence,
        )
        expected = result_from_labels(labels, pages)
        outcomes.append(SampleOutcome(labels.name, compare(run.result, expected), run.reply.usage))
    return outcomes
