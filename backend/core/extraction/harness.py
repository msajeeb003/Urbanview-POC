"""The corpus evaluation harness: run the extraction pipeline on the corpus documents and score it.

The same code the ``extract_document`` job runs: the PDF stage (``extract_pages``, sections, the
chunk plan), :func:`core.extraction.steps.plan_steps` and :func:`~core.extraction.steps.read_step`
per step (land-use legend first, then the rest concurrently), then
:func:`core.extraction.scoring.score_document` against the gold set.

- **Reply cache.** Every model reply is stored under the request's hash (model, system blocks,
  user message, schema) in the cache folder (git-ignored: it holds document text). An unchanged
  request is never paid for twice, and ``--replay`` scores from the cache alone (no key, no cost:
  what CI runs when prompts did not change).
- **Cost.** Tokens per document from the replies (cached replies count what they cost when they
  were made), priced from the published per-million-token rates (USD).
- **Pages cache.** The PDF stage's page data per checksum and ``PREPROCESS_VERSION``.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.extraction.chunking import SectionRules, detect_sections, plan_chunks
from core.extraction.corpus import CorpusDocument, read_source
from core.extraction.llm import ModelError, ModelReply, ModelUsage, StructuredModel
from core.extraction.manifest import dump_pages, load_pages
from core.extraction.normalise import Conventions
from core.extraction.preprocess import (
    PREPROCESS_VERSION,
    DocumentPages,
    PreprocessOptions,
    extract_pages,
)
from core.extraction.prompts import PROMPT_VERSION, DocumentContext, SystemBlock
from core.extraction.steps import StepOutcome, plan_steps, read_step
from core.extraction.validate import legend_map

# USD per million tokens (input, output), Anthropic first-party list prices; cache writes are
# billed at 1.25x input and cache reads at 0.1x input.
PRICES_USD: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def cost_usd(model: str | None, usage: ModelUsage) -> float | None:
    prices = next((p for name, p in PRICES_USD.items() if model and model.startswith(name)), None)
    if prices is None:
        return None
    price_in, price_out = prices
    return round(
        (
            usage.input_tokens * price_in
            + usage.cache_write_tokens * price_in * 1.25
            + usage.cache_read_tokens * price_in * 0.1
            + usage.output_tokens * price_out
        )
        / 1_000_000,
        4,
    )


class NotCached(ModelError):
    """Replay mode: the request was never answered (prompts changed since the cached run)."""


class CachingModel:
    """A model whose replies are stored by request hash (thread-safe counters)."""

    def __init__(
        self, inner: StructuredModel | None, cache_dir: Path, *, name: str, replay: bool = False
    ) -> None:
        self.inner = inner
        self.cache_dir = cache_dir
        self.name = name
        self.replay = replay
        self.lock = threading.Lock()
        self.hits = 0
        self.live = 0

    def key(self, system: Sequence[SystemBlock], user: str, schema: dict[str, Any]) -> str:
        body = json.dumps(
            {
                "model": self.name,
                "system": [b.text for b in system],
                "user": user,
                "schema": schema,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def complete(
        self, *, system: Sequence[SystemBlock], user: str, schema: dict[str, Any]
    ) -> ModelReply:
        key = self.key(system, user, schema)
        path = self.cache_dir / key[:2] / f"{key}.json"
        if path.is_file():
            stored = json.loads(path.read_text(encoding="utf-8"))
            with self.lock:
                self.hits += 1
            return ModelReply(
                data=stored["data"],
                model=stored["model"],
                usage=ModelUsage(**stored["usage"]),
                request_id=stored.get("request_id"),
            )
        if self.replay or self.inner is None:
            raise NotCached(f"no cached reply for request {key[:12]} (run with --live)")
        reply = self.inner.complete(system=system, user=user, schema=schema)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "data": reply.data,
                    "model": reply.model,
                    "usage": {
                        "input_tokens": reply.usage.input_tokens,
                        "output_tokens": reply.usage.output_tokens,
                        "cache_read_tokens": reply.usage.cache_read_tokens,
                        "cache_write_tokens": reply.usage.cache_write_tokens,
                    },
                    "request_id": reply.request_id,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.lock:
            self.live += 1
        return reply


def document_pages(doc: CorpusDocument, cache_dir: Path, *, municipality: str) -> DocumentPages:
    """The PDF stage's page data (sections detected), cached per checksum and version."""
    path = cache_dir / "pages" / f"{doc.sha256}-{PREPROCESS_VERSION}.json.gz"
    if path.is_file():
        return load_pages(path.read_bytes())
    pages = extract_pages(read_source(doc), options=PreprocessOptions())
    detect_sections(pages, SectionRules.from_profile(municipality))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dump_pages(pages))
    return pages


@dataclass(slots=True)
class DocumentRun:
    document: str
    steps: list[StepOutcome]
    seconds: float
    usage: ModelUsage = field(default_factory=ModelUsage)
    model_version: str | None = None
    cost_usd: float | None = None
    live_requests: int = 0
    cached_requests: int = 0

    @property
    def results(self):
        return [s.result for s in self.steps if s.result is not None]

    @property
    def failed(self) -> list[StepOutcome]:
        return [s for s in self.steps if s.status == "failed"]


def run_document(
    doc: CorpusDocument,
    pages: DocumentPages,
    *,
    model: CachingModel,
    municipality: str,
    prompt_version: str = PROMPT_VERSION,
    only_pages: set[int] | None = None,
    concurrency: int = 4,
    low_confidence: float = 0.7,
    call_retries: int = 2,
    progress: bool = True,
) -> DocumentRun:
    chunks = plan_chunks(pages, PreprocessOptions())
    steps = plan_steps(chunks)
    if only_pages is not None:
        steps = [(c, t) for c, t in steps if set(c.pages) & only_pages]
    context = DocumentContext(id=1, name=doc.name, type=doc.type)
    conventions = Conventions.from_profile(municipality)
    started = time.monotonic()
    hits_before, live_before = model.hits, model.live
    done: list[StepOutcome] = []
    legend: dict[str, str] = {}

    def one(step: tuple[Any, str]) -> StepOutcome:
        chunk, task = step
        outcome = read_step(
            chunk,
            task,
            pages,
            context,
            model=model,
            municipality_id=municipality,
            legend=legend,
            conventions=conventions,
            low_confidence=low_confidence,
            prompt_version=prompt_version,
            call_retries=call_retries,
        )
        if progress:
            print(
                f"  {doc.id} {chunk.id} p{chunk.pages[0]} {task}: {outcome.status}"
                f" ({outcome.seconds:.0f}s)",
                flush=True,
            )
        return outcome

    legend_steps = [s for s in steps if s[1] == "land_use_legend"]
    for step in legend_steps:
        outcome = one(step)
        done.append(outcome)
        if outcome.result is not None:
            legend.update(legend_map(outcome.result.land_use_legend))
    rest = [s for s in steps if s[1] != "land_use_legend"]
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        done.extend(pool.map(one, rest))
    usage = ModelUsage()
    for outcome in done:
        usage = usage + outcome.usage
    version = next((s.model_version for s in done if s.model_version), None)
    return DocumentRun(
        document=doc.id,
        steps=done,
        seconds=time.monotonic() - started,
        usage=usage,
        model_version=version,
        cost_usd=cost_usd(version or model.name, usage),
        live_requests=model.live - live_before,
        cached_requests=model.hits - hits_before,
    )
