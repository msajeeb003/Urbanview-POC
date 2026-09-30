"""Evaluating a model against the contract (``python -m core.extraction eval``).

- :func:`run_absent_cases`: the "never guessed" cases (``core.extraction.cases``). A case passes
  only when the model itself returns null (``not_found``) for every absent field: a value the
  validator had to remove (``unverified``) never reaches the review queue, but it is still a
  failure of the model and counts as one.

The accuracy on the client's documents is measured by the corpus (``corpus eval``, ``evalcli``).

A run needs model credentials and costs tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.extraction.cases import ABSENT_CASES, find_leaf
from core.extraction.llm import ClaudeModel, ModelUsage, StructuredModel
from core.extraction.prompts import DocumentContext
from core.extraction.run import run_task
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
