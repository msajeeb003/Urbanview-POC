"""One extraction request end to end: prompt -> model -> response model -> validated contract.

Pure apart from the model call, so the job, the evaluation and the tests share it. The response
is parsed against the task's response model; a response that does not fit is
:class:`~core.extraction.llm.ModelOutputInvalid` (never repaired by guessing). The extraction job
asks once more with ``feedback`` (the validator's error appended to the user message) before it
records the chunk as failed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from pydantic import ValidationError

from core.extraction.llm import ModelOutputInvalid, ModelReply, StructuredModel
from core.extraction.normalise import Conventions
from core.extraction.pages import PageInput
from core.extraction.prompts import (
    PROMPT_VERSION,
    DocumentContext,
    PromptBundle,
    build_prompt,
    response_model,
)
from core.extraction.schema import ExtractionResult, TaskKind
from core.extraction.validate import assemble

FEEDBACK = (
    "\n\nYour previous answer to this request was rejected by the validator:\n{error}\n"
    "Answer again for the same pages with the same instructions, exactly in the response schema."
)
FEEDBACK_MAX_CHARS = 2000


@dataclass(frozen=True, slots=True)
class TaskRun:
    result: ExtractionResult
    prompt: PromptBundle
    reply: ModelReply


def run_task(
    task: TaskKind,
    *,
    model: StructuredModel,
    municipality_id: str,
    document: DocumentContext,
    pages: Sequence[PageInput],
    low_confidence: float = 0.7,
    legend: Mapping[str, str] | None = None,
    prompt_version: str = PROMPT_VERSION,
    conventions: Conventions | None = None,
    feedback: str | None = None,
) -> TaskRun:
    prompt = build_prompt(
        task,
        municipality_id=municipality_id,
        document=document,
        pages=pages,
        version=prompt_version,
    )
    if feedback:
        error = feedback[:FEEDBACK_MAX_CHARS]
        prompt = replace(prompt, user=prompt.user + FEEDBACK.format(error=error))
    reply = model.complete(system=prompt.system, user=prompt.user, schema=prompt.schema)
    try:
        response = response_model(prompt.response_name).model_validate(reply.data)
    except ValidationError as exc:
        raise ModelOutputInvalid(
            f"the response does not fit {prompt.response_name}: {exc}",
            usage=reply.usage,
            model=reply.model,
        ) from exc
    if hasattr(response, "to_legacy"):  # a compact answer (prompt set 1.1 onwards)
        response = response.to_legacy()
    result = assemble(
        task,
        response,
        pages=pages,
        document_id=document.id,
        municipality_id=municipality_id,
        conventions=conventions or Conventions.from_profile(municipality_id),
        prompt_version=prompt.prompt_version,
        model=reply.model,
        low_confidence=low_confidence,
        legend=legend,
    )
    return TaskRun(result=result, prompt=prompt, reply=reply)
