"""Steps: one chunk read by one extraction task, with its retries. Shared by the
``extract_document`` job (``jobs.extraction_runner``) and the corpus evaluation harness
(``core.extraction.harness``), so what is evaluated is what runs.

- :func:`plan_steps`: every (chunk, task) the pre-processing plan suggests, the land-use legend
  first (later steps classify its codes), plus the document task on the first readable chunk when
  the plan has none (a parameter table's title and running header name the document).
- :func:`read_step`: one structured request through ``run_task``; transient model errors are
  retried with exponential backoff (then raised as :class:`TransientStepError` for the caller's
  own retry), an answer that does not fit is asked again once with the validator's error, a
  refusal or a bad request fails the step. Synchronous (the job runs it in a thread).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from core.extraction.chunking import ChunkPlan, chunk_pages
from core.extraction.llm import (
    ModelError,
    ModelOutputInvalid,
    ModelRateLimited,
    ModelUnavailable,
    ModelUsage,
    StructuredModel,
)
from core.extraction.normalise import Conventions
from core.extraction.preprocess import DocumentPages
from core.extraction.prompts import PROMPT_VERSION, DocumentContext
from core.extraction.run import run_task
from core.extraction.schema import ExtractionResult

TASK_ORDER = {"land_use_legend": 0}


class TransientStepError(Exception):
    """The model stayed unavailable through the step's retries; ``rate_limited`` for 429."""

    def __init__(self, message: str, *, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited


@dataclass(slots=True)
class StepOutcome:
    chunk_id: str
    task: str
    pages: list[int]
    status: str  # done | failed
    attempts: int
    usage: ModelUsage
    result: ExtractionResult | None = None
    error: str | None = None
    model_version: str | None = None
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)


def plan_steps(
    chunks: Sequence[ChunkPlan], *, document_task: bool = True
) -> list[tuple[ChunkPlan, str]]:
    """Every (chunk, task) to read: the land-use legend first, else in plan order; the document
    task on the first readable chunk when no chunk suggests it."""
    steps = [(chunk, task) for chunk in chunks for task in chunk.tasks]
    if document_task and chunks and not any(task == "document" for _, task in steps):
        first = min(chunks, key=lambda c: (c.pages[0], c.id))
        steps.insert(0, (first, "document"))
    return sorted(steps, key=lambda s: TASK_ORDER.get(s[1], 1))  # stable: plan order kept


def backoff_seconds(attempt: int, base: float, cap: float) -> float:
    return min(cap, base * (2**attempt))


def read_step(
    chunk: ChunkPlan,
    task: str,
    doc: DocumentPages,
    context: DocumentContext,
    *,
    model: StructuredModel,
    municipality_id: str,
    legend: Mapping[str, str] | None = None,
    conventions: Conventions | None = None,
    low_confidence: float = 0.7,
    prompt_version: str = PROMPT_VERSION,
    call_retries: int = 2,
    retry_base_seconds: float = 2.0,
    retry_max_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> StepOutcome:
    pages = chunk_pages(doc, chunk)
    usage = ModelUsage()
    attempts = 0
    feedback: str | None = None
    error = "no answer"
    started = time.monotonic()
    for _ in range(2):  # the answer, then once more with the validator's error
        for transient_try in range(call_retries + 1):
            attempts += 1
            try:
                run = run_task(
                    task,  # type: ignore[arg-type]
                    model=model,
                    municipality_id=municipality_id,
                    document=context,
                    pages=pages,
                    low_confidence=low_confidence,
                    legend=dict(legend or {}),
                    prompt_version=prompt_version,
                    conventions=conventions,
                    feedback=feedback,
                )
            except (ModelRateLimited, ModelUnavailable) as exc:
                if transient_try >= call_retries:
                    raise TransientStepError(
                        f"{chunk.id} {task}: {exc}",
                        rate_limited=isinstance(exc, ModelRateLimited),
                    ) from exc
                sleep(backoff_seconds(transient_try, retry_base_seconds, retry_max_seconds))
                continue
            except ModelOutputInvalid as exc:
                if exc.usage is not None:
                    usage = usage + exc.usage
                error = f"invalid answer: {exc}"
                feedback = str(exc)
                break  # ask once more with the error, then give the step up
            except ModelError as exc:  # refused, bad request, authentication: not retried
                return StepOutcome(
                    chunk.id,
                    task,
                    chunk.pages,
                    "failed",
                    attempts,
                    usage,
                    error=f"{type(exc).__name__}: {exc}",
                    seconds=time.monotonic() - started,
                )
            usage = usage + run.reply.usage
            return StepOutcome(
                chunk.id,
                task,
                chunk.pages,
                "done",
                attempts,
                usage,
                result=run.result,
                model_version=run.reply.model,
                seconds=time.monotonic() - started,
            )
    return StepOutcome(
        chunk.id,
        task,
        chunk.pages,
        "failed",
        attempts,
        usage,
        error=error,
        seconds=time.monotonic() - started,
    )
