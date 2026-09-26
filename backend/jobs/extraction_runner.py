"""The ``extract_document`` job body: one extraction run of a document version's file.

1. **Run record** (``extraction_runs``, created by the API when it queued the job; created here
   for a job that has none): queued -> **extracting** -> **ready_for_review** | **failed**. A run
   that already finished is reported as it is (a re-delivered message changes nothing).
2. **Pages**: the file's pre-processing manifest (``stored_files.preprocess``) when it is current
   for the checksum, else the PDF stage runs first (``jobs.preprocessing.PreprocessRunner``); the
   page data comes from the bucket. Scanned pages nobody could read are skipped and listed.
3. **Steps**: every chunk of the manifest's plan that suggests extraction tasks, once per task
   (``land_use_legend`` first, so later steps classify legend codes), in reading priority. Chunks
   without planning content are counted, not read. One structured request per step
   (``core.extraction.run.run_task``: prompt set ``PROMPT_VERSION``, schema ``SCHEMA_VERSION``,
   validation into the canonical contract).
4. **Failures**: a transient model error (overload, timeout, 429) is retried in place with
   exponential backoff (``EXTRACTION_CALL_RETRIES``), then the job itself retries (the base task's
   backoff) and resumes: every finished step is checkpointed (``extraction_run_chunks``) and
   never paid for twice. An answer that does not fit the schema is asked again once with the
   validator's error; a second failure, a refusal or a bad request fails that step only, which is
   recorded with its pages; the rest of the file carries on. A run in which every step failed,
   or with nothing readable at all, fails.
5. **Items** (one transaction, at the end, so the queue never sees half a run): the steps'
   results through ``core.extraction.staging.to_staging_rows`` into
   ``planning_parameter_extractions`` (``pending_review``, one row per value with parameter,
   target, value, unit, raw text, page, bbox, confidence, method, flags, prompt / schema versions
   and ``extracted_by = llm:<model>``). Targets are matched to the document's urban parcels and
   blocks by their printed number / label; one that matches no geometry stays a text reference
   flagged ``target_unmatched`` (``target_staged`` when it matches geometry the GIS job staged
   but nobody published). Each item links to the previous run's item for the same target and
   field (``previous_item_id``, ``change`` new | same | changed), and what this run replaces is
   **superseded, never deleted**: every open item of an older version of the document, or of a
   run over another file of the same version; of a run over the same file (a newer prompt,
   schema or model), only the pending items: decisions already taken stay.
6. **Record**: the run row (pages processed / skipped / failed, steps, items written, flagged
   low_confidence and unmatched, items superseded, tokens, estimated cost, model version), the
   job's progress and result (``GET /v1/admin/jobs/{id}``), and ``audit_log`` rows
   ``extraction.start`` / ``extraction.finish``.

Nothing here writes the serving tables: the review queue decides, the publish job serves.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.services.audit import write_audit
from core.extraction.chunking import ChunkPlan
from core.extraction.llm import (
    ModelUsage,
    StructuredModel,
)
from core.extraction.manifest import PreprocessManifest, is_current
from core.extraction.normalise import Conventions, block_key, parcel_key
from core.extraction.preprocess import PREPROCESS_VERSION, DocumentPages, PreprocessOptions
from core.extraction.prompts import PROMPT_VERSION, DocumentContext
from core.extraction.runs import insert_run
from core.extraction.schema import ExtractionResult, Flag
from core.extraction.staging import TARGET_STAGED, TARGET_UNMATCHED, to_staging_rows
from core.extraction.steps import StepOutcome, TransientStepError, plan_steps, read_step
from core.extraction.validate import legend_map
from jobs.base import JobContext, RateLimited, TransientError
from jobs.cost import cost_for
from jobs.preprocessing import load_document_pages, read_manifest

log = logging.getLogger("urbanview.jobs.extraction")

ACTOR = "worker:extract_document"
REPEATED = "repeated_in_run"  # the run read the same target and field more than once

# --- SQL ------------------------------------------------------------------------------------------

RUN_BY_JOB_SQL = text("SELECT * FROM extraction_runs WHERE job_id = :job_id")
DOCUMENT_SQL = text(
    """
    SELECT d.id, d.name, d.type, d.file_id, d.version, COALESCE(d.lineage_id, d.id) AS lineage_id,
           f.sha256, f.kind, f.preprocess
    FROM planning_documents d
    LEFT JOIN stored_files f ON f.id = d.file_id
    WHERE d.municipality_id = :m AND d.id = :id
    """
)
FILE_MANIFEST_SQL = text("SELECT preprocess FROM stored_files WHERE id = :id")
START_SQL = text(
    """
    UPDATE extraction_runs
    SET status = 'extracting', started_at = COALESCE(started_at, :at), error = NULL,
        finished_at = NULL, preprocess_version = :preprocess_version
    WHERE id = :id
    """
)
FAIL_SQL = text(
    """
    UPDATE extraction_runs
    SET status = 'failed', error = :error, finished_at = :at, summary = CAST(:summary AS jsonb)
    WHERE id = :id AND status <> 'ready_for_review'
    """
)
# A run restarted after it failed (a manual retry) reads its failed steps again; an automatic
# retry after a transient error keeps them (they already had their validation retry).
CLEAR_FAILED_STEPS_SQL = text(
    "DELETE FROM extraction_run_chunks WHERE run_id = :id AND status = 'failed'"
)
STEPS_SQL = text(
    """
    SELECT chunk_id, task, pages, status, attempts, error, result, model_version, tokens_in,
           tokens_out
    FROM extraction_run_chunks WHERE run_id = :run_id ORDER BY id
    """
)
SAVE_STEP_SQL = text(
    """
    INSERT INTO extraction_run_chunks (run_id, chunk_id, task, pages, status, attempts, error,
                                       result, model_version, tokens_in, tokens_out)
    VALUES (:run_id, :chunk_id, :task, CAST(:pages AS jsonb), :status, :attempts, :error,
            CAST(:result AS jsonb), :model_version, :tokens_in, :tokens_out)
    ON CONFLICT (run_id, chunk_id, task) DO NOTHING
    """
)
PROGRESS_SQL = text(
    """
    UPDATE extraction_runs
    SET chunks_total = :chunks_total, chunks_done = :chunks_done, chunks_failed = :chunks_failed,
        tokens_in = :tokens_in, tokens_out = :tokens_out,
        model_version = COALESCE(:model_version, model_version)
    WHERE id = :id
    """
)
PARCELS_SQL = text(
    "SELECT id, urban_parcel_number FROM urban_parcels "
    "WHERE municipality_id = :m AND document_id = :document_id"
)
BLOCKS_SQL = text(
    """
    SELECT b.id, b.block_ref FROM urban_blocks b
    WHERE b.municipality_id = :m
      AND (b.id IN (SELECT u.block_id FROM urban_parcels u WHERE u.document_id = :document_id)
           OR EXISTS (SELECT 1 FROM planning_documents d
                      WHERE d.id = :document_id AND d.coverage_geom IS NOT NULL
                        AND ST_Intersects(d.coverage_geom, b.geom)))
    """
)
STAGED_SQL = text(
    """
    SELECT s.layer_id, s.properties ->> 'urban_parcel_number' AS number,
           s.properties ->> 'block_ref' AS block_ref
    FROM staging_geometry s
    JOIN geometry_batches g ON g.id = s.batch_id AND g.status = 'staged'
    WHERE s.municipality_id = :m
      AND ((s.layer_id = 'urban_parcels' AND s.properties ->> 'document_id' = :document_ref)
           OR s.layer_id = 'urban_blocks')
    """
)
PREVIOUS_SQL = text(
    """
    SELECT DISTINCT ON (e.entity_type, e.target_key, e.field_key)
           e.id, e.entity_type, e.target_key, e.field_key, e.review_state::text AS review_state,
           e.value_text, e.value_number, e.unit, e.amended_value_text, e.amended_value_number,
           e.amended_unit
    FROM planning_parameter_extractions e
    JOIN planning_documents d ON d.id = e.document_id
    WHERE e.municipality_id = :m AND COALESCE(d.lineage_id, d.id) = :lineage_id
      AND e.target_key IS NOT NULL AND e.superseded_at IS NULL
      AND e.run_id IS DISTINCT FROM :run_id
    ORDER BY e.entity_type, e.target_key, e.field_key, e.id DESC
    """
)
INSERT_ITEM_SQL = text(
    """
    INSERT INTO planning_parameter_extractions (
        municipality_id, document_id, entity_type, urban_parcel_id, block_id, zone_id, field_key,
        parameter_key, value_text, value_number, unit, raw_text, confidence, source_page,
        source_bbox, source_note, extracted_by, review_state, schema_version, prompt_version,
        extraction_method, flags, payload, job_id, run_id, target_label, target_key,
        previous_item_id, change)
    VALUES (
        :municipality_id, :document_id, :entity_type, :urban_parcel_id, :block_id, :zone_id,
        :field_key, :parameter_key, :value_text, :value_number, :unit, :raw_text, :confidence,
        :source_page, CAST(:source_bbox AS jsonb), :source_note, :extracted_by,
        CAST(:review_state AS review_state), :schema_version, :prompt_version,
        :extraction_method, CAST(:flags AS jsonb), CAST(:payload AS jsonb), :job_id, :run_id,
        :target_label, :target_key, :previous_item_id, :change)
    """
)
# What this run replaces: every open item of an older version of the document or of a run over
# another file of this version; of a run over the same file only the pending items.
SUPERSEDE_SQL = text(
    """
    UPDATE planning_parameter_extractions e
    SET superseded_by_run_id = :run_id, superseded_at = :at
    FROM extraction_runs r, planning_documents d
    WHERE r.id = e.run_id AND d.id = e.document_id AND e.municipality_id = :m
      AND e.run_id <> :run_id AND e.superseded_at IS NULL AND e.published_value_id IS NULL
      AND r.lineage_id = :lineage_id
      AND ((e.document_id = :document_id
            AND (r.file_sha256 <> :sha256 OR e.review_state = 'pending_review'))
           OR (e.document_id <> :document_id AND d.version < :version))
    RETURNING e.id, e.run_id
    """
)
SUPERSEDE_RUNS_SQL = text(
    "UPDATE extraction_runs SET superseded_by_run_id = :run_id "
    "WHERE id = ANY(:ids) AND superseded_by_run_id IS NULL"
)
FINISH_SQL = text(
    """
    UPDATE extraction_runs
    SET status = 'ready_for_review', finished_at = :at, error = NULL,
        model_version = COALESCE(:model_version, model_version),
        pages_total = :pages_total, pages_processed = CAST(:pages_processed AS jsonb),
        pages_skipped = CAST(:pages_skipped AS jsonb), pages_failed = CAST(:pages_failed AS jsonb),
        chunks_total = :chunks_total, chunks_done = :chunks_done, chunks_failed = :chunks_failed,
        items_written = :items_written, items_low_confidence = :items_low_confidence,
        items_unmatched = :items_unmatched, items_superseded = :items_superseded,
        tokens_in = :tokens_in, tokens_out = :tokens_out,
        estimated_cost_eur = :estimated_cost_eur, summary = CAST(:summary AS jsonb)
    WHERE id = :id
    """
)


class ExtractionFailed(Exception):
    """The run cannot produce anything to review (no file, changed file, nothing readable,
    every step failed): the run and the job end ``failed``."""


# --- steps ----------------------------------------------------------------------------------------


@dataclass(slots=True)
class Tally:
    """What the run has read so far (checkpointed steps plus this attempt's)."""

    steps: list[StepOutcome] = field(default_factory=list)

    @property
    def usage(self) -> ModelUsage:
        total = ModelUsage()
        for step in self.steps:
            total = total + step.usage
        return total

    @property
    def model_version(self) -> str | None:
        return next((s.model_version for s in reversed(self.steps) if s.model_version), None)

    def chunks(self) -> tuple[set[str], set[str]]:
        """(chunks with a successful step, chunks with a failed step)."""
        done = {s.chunk_id for s in self.steps if s.status == "done"}
        failed = {s.chunk_id for s in self.steps if s.status == "failed"}
        return done, failed


def ordered_steps(chunks: Sequence[ChunkPlan]) -> list[tuple[ChunkPlan, str]]:
    """The steps of a plan (``core.extraction.steps.plan_steps``)."""
    return plan_steps(chunks)


def value_change(previous: Mapping[str, Any] | None, row: Mapping[str, Any]) -> str:
    """new | same | changed: the item against the previous run's item for its target."""
    if previous is None:
        return "new"
    amended = previous["review_state"] == "amended"
    number = previous["amended_value_number"] if amended else previous["value_number"]
    text_value = previous["amended_value_text"] if amended else previous["value_text"]
    unit = (previous["amended_unit"] if amended else None) or previous["unit"]
    if row["value_number"] is not None or number is not None:
        if row["value_number"] is None or number is None:
            return "changed"
        close = abs(float(row["value_number"]) - float(number)) <= 1e-9 * max(
            1.0, abs(float(number))
        )
        return "same" if close and (row["unit"] or None) == (unit or None) else "changed"

    def norm(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    return "same" if norm(row["value_text"]) == norm(text_value) else "changed"


# --- the runner -----------------------------------------------------------------------------------


class ExtractionRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        storage: Any,
        *,
        municipality_id: str,
        model: StructuredModel,
        model_name: str,
        options: PreprocessOptions,
        preprocess: Callable[[int], Awaitable[Any]],
        low_confidence: float = 0.7,
        call_retries: int = 2,
        retry_base_seconds: float = 2.0,
        retry_max_seconds: float = 30.0,
        max_steps: int = 400,
        prompt_version: str = PROMPT_VERSION,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.municipality_id = municipality_id
        self.model = model
        self.model_name = model_name
        self.options = options
        self.preprocess = preprocess
        self.low_confidence = low_confidence
        self.call_retries = call_retries
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.max_steps = max_steps
        self.prompt_version = prompt_version
        self.clock = clock
        self.sleep = sleep
        self.conventions = Conventions.from_profile(municipality_id)

    # --- lifecycle --------------------------------------------------------------------------------

    async def run(self, job: JobContext) -> tuple[dict[str, Any], ModelUsage, str | None]:
        """The run summary, the tokens spent and the model version (for the job's cost)."""
        document_id = int(job.payload.get("document_id") or job.document_id or job.target_id or 0)
        run = await self._run_row(job, document_id)
        if run["status"] == "ready_for_review":  # a re-delivered message: nothing to redo
            summary = dict(run["summary"] or {})
            return summary, ModelUsage(), run["model_version"]
        await self._start(run, job)
        try:
            return await self._execute(run, job, document_id)
        except TransientError as exc:
            if job.attempts >= job.max_attempts:
                await self._fail(run, job, f"{type(exc).__name__}: {exc}")
            raise
        except Exception as exc:
            await self._fail(run, job, f"{type(exc).__name__}: {exc}")
            raise

    async def _run_row(self, job: JobContext, document_id: int) -> Mapping[str, Any]:
        async with self.session_factory() as session:
            row = (await session.execute(RUN_BY_JOB_SQL, {"job_id": job.id})).mappings().first()
            if row is None:
                run_id = await insert_run(
                    session,
                    municipality_id=self.municipality_id,
                    document_id=document_id,
                    job_id=job.id,
                    model=self.model_name,
                    prompt_version=self.prompt_version,
                )
                if run_id is None:
                    raise ExtractionFailed(f"document {document_id} has no stored file")
                await session.commit()
                row = (await session.execute(RUN_BY_JOB_SQL, {"job_id": job.id})).mappings().one()
        return row

    async def _audit(
        self,
        session: AsyncSession,
        action: str,
        run: Mapping[str, Any],
        details: Mapping[str, Any],
    ) -> None:
        await write_audit(
            session,
            municipality_id=self.municipality_id,
            actor=ACTOR,
            action=action,
            entity_type="extraction_run",
            entity_id=run["id"],
            details={"document_id": run["document_id"], "job_id": run["job_id"], **details},
        )

    async def _start(self, run: Mapping[str, Any], job: JobContext) -> None:
        async with self.session_factory() as session:
            if run["status"] == "failed":
                await session.execute(CLEAR_FAILED_STEPS_SQL, {"id": run["id"]})
            await session.execute(
                START_SQL,
                {"id": run["id"], "at": self.clock(), "preprocess_version": PREPROCESS_VERSION},
            )
            await self._audit(
                session,
                "extraction.start",
                run,
                {
                    "attempt": job.attempts,
                    "file_sha256": run["file_sha256"],
                    "model": run["model"],
                    "prompt_version": run["prompt_version"],
                    "schema_version": run["schema_version"],
                },
            )
            await session.commit()

    async def _fail(self, run: Mapping[str, Any], job: JobContext, error: str) -> None:
        summary = {"run_id": run["id"], "status": "failed", "error": error}
        async with self.session_factory() as session:
            await session.execute(
                FAIL_SQL,
                {
                    "id": run["id"],
                    "error": error[:4000],
                    "at": self.clock(),
                    "summary": json.dumps(summary),
                },
            )
            await self._audit(
                session, "extraction.finish", run, {"status": "failed", "error": error[:500]}
            )
            await session.commit()
        log.warning("extraction run %s failed: %s", run["id"], error, extra={"job_id": job.id})

    async def _report(self, job: JobContext, progress: dict[str, Any]) -> None:
        if job.report is not None:
            await job.report(progress)

    # --- the run ----------------------------------------------------------------------------------

    async def _pages(self, document: Mapping[str, Any]) -> tuple[PreprocessManifest, DocumentPages]:
        manifest = read_manifest(document["preprocess"])
        if not is_current(manifest, document["sha256"], self.options):
            await self.preprocess(int(document["file_id"]))  # the PDF stage, cached by checksum
            async with self.session_factory() as session:
                raw = (
                    await session.execute(FILE_MANIFEST_SQL, {"id": document["file_id"]})
                ).scalar_one()
            manifest = read_manifest(raw)
        if manifest is None:
            raise ExtractionFailed("the file has no pre-processing manifest")
        pages = await asyncio.to_thread(load_document_pages, self.storage, manifest)
        return manifest, pages

    async def _execute(
        self, run: Mapping[str, Any], job: JobContext, document_id: int
    ) -> tuple[dict[str, Any], ModelUsage, str | None]:
        async with self.session_factory() as session:
            document = (
                (
                    await session.execute(
                        DOCUMENT_SQL, {"m": self.municipality_id, "id": document_id}
                    )
                )
                .mappings()
                .first()
            )
        if document is None or document["file_id"] is None:
            raise ExtractionFailed(f"document {document_id} has no stored file")
        if document["kind"] != "planning_document":
            raise ExtractionFailed(f"the file of document {document_id} is not a planning PDF")
        if document["sha256"] != run["file_sha256"]:
            raise ExtractionFailed("the document's file changed since the extraction was queued")

        manifest, pages = await self._pages(document)
        skipped_pages = list(manifest.summary.unread_pages)
        steps = ordered_steps(manifest.chunks)
        if len(steps) > self.max_steps:
            raise ExtractionFailed(
                f"{len(steps)} chunk steps planned: more than EXTRACTION_MAX_CHUNKS "
                f"({self.max_steps})"
            )
        if not steps and skipped_pages and len(skipped_pages) == len(manifest.pages):
            raise ExtractionFailed(
                f"no readable page: {len(skipped_pages)} scanned page(s) need OCR or manual entry"
            )

        tally = Tally(steps=await self._checkpoints(run["id"]))
        done_keys = {(s.chunk_id, s.task) for s in tally.steps}
        legend: dict[str, str] = {}
        for step in tally.steps:
            if step.result is not None:
                legend.update(legend_map(step.result.land_use_legend))
        context = DocumentContext(id=document_id, name=document["name"], type=document["type"])
        chunk_ids = {chunk.id for chunk, _ in steps}
        for chunk, task in steps:
            if (chunk.id, task) in done_keys:
                continue
            await self._report(job, self._progress(tally, len(steps), chunk.id, task))
            outcome = await self._step(chunk, task, pages, context, legend)
            await self._checkpoint(run["id"], outcome, tally, len(chunk_ids))
            if outcome.result is not None and task == "land_use_legend":
                legend.update(legend_map(outcome.result.land_use_legend))
        await self._report(job, self._progress(tally, len(steps), None, None))

        succeeded = [s for s in tally.steps if s.status == "done"]
        if steps and not succeeded:
            errors = "; ".join(sorted({s.error or "?" for s in tally.steps}))[:1000]
            raise ExtractionFailed(f"every chunk failed ({len(tally.steps)} steps): {errors}")
        summary = await self._write(run, document, manifest, steps, tally, skipped_pages)
        return summary, tally.usage, tally.model_version

    # --- one step ---------------------------------------------------------------------------------

    async def _step(
        self,
        chunk: ChunkPlan,
        task: str,
        doc: DocumentPages,
        context: DocumentContext,
        legend: Mapping[str, str],
    ) -> StepOutcome:
        try:
            return await asyncio.to_thread(
                read_step,
                chunk,
                task,
                doc,
                context,
                model=self.model,
                municipality_id=self.municipality_id,
                legend=dict(legend),
                conventions=self.conventions,
                low_confidence=self.low_confidence,
                prompt_version=self.prompt_version,
                call_retries=self.call_retries,
                retry_base_seconds=self.retry_base_seconds,
                retry_max_seconds=self.retry_max_seconds,
                sleep=self.sleep,
            )
        except TransientStepError as exc:
            raise (RateLimited if exc.rate_limited else TransientError)(str(exc)) from exc

    # --- checkpoints ------------------------------------------------------------------------------

    async def _checkpoints(self, run_id: int) -> list[StepOutcome]:
        async with self.session_factory() as session:
            rows = (await session.execute(STEPS_SQL, {"run_id": run_id})).mappings().all()
        out = []
        for r in rows:
            result = ExtractionResult.model_validate(r["result"]) if r["result"] else None
            out.append(
                StepOutcome(
                    r["chunk_id"],
                    r["task"],
                    list(r["pages"] or []),
                    r["status"],
                    int(r["attempts"]),
                    ModelUsage(
                        input_tokens=int(r["tokens_in"]), output_tokens=int(r["tokens_out"])
                    ),
                    result=result,
                    error=r["error"],
                    model_version=r["model_version"],
                )
            )
        return out

    async def _checkpoint(
        self, run_id: int, outcome: StepOutcome, tally: Tally, chunks_total: int
    ) -> None:
        tally.steps.append(outcome)
        done, failed = tally.chunks()
        usage = tally.usage
        async with self.session_factory() as session:
            await session.execute(
                SAVE_STEP_SQL,
                {
                    "run_id": run_id,
                    "chunk_id": outcome.chunk_id,
                    "task": outcome.task,
                    "pages": json.dumps(outcome.pages),
                    "status": outcome.status,
                    "attempts": outcome.attempts,
                    "error": (outcome.error or None) and outcome.error[:4000],
                    "result": outcome.result.model_dump_json() if outcome.result else None,
                    "model_version": outcome.model_version,
                    "tokens_in": outcome.usage.tokens_in,
                    "tokens_out": outcome.usage.output_tokens,
                },
            )
            await session.execute(
                PROGRESS_SQL,
                {
                    "id": run_id,
                    "chunks_total": chunks_total,
                    "chunks_done": len(done),
                    "chunks_failed": len(failed),
                    "tokens_in": usage.tokens_in,
                    "tokens_out": usage.output_tokens,
                    "model_version": outcome.model_version,
                },
            )
            await session.commit()

    @staticmethod
    def _progress(
        tally: Tally, steps_total: int, chunk: str | None, task: str | None
    ) -> dict[str, Any]:
        return {
            "step": "extracting" if chunk else "writing_items",
            "steps_total": steps_total,
            "steps_done": sum(1 for s in tally.steps if s.status == "done"),
            "steps_failed": sum(1 for s in tally.steps if s.status == "failed"),
            "chunk": chunk,
            "task": task,
        }

    # --- items ------------------------------------------------------------------------------------

    async def _targets(
        self, session: AsyncSession, document_id: int
    ) -> tuple[dict[str, int], dict[str, int], set[str], set[str]]:
        abbreviation = self.conventions.parcel_abbreviation
        words = self.conventions.block_label_words
        params = {"m": self.municipality_id, "document_id": document_id}
        parcels: dict[str, int] = {}
        for r in (await session.execute(PARCELS_SQL, params)).mappings():
            key = parcel_key(r["urban_parcel_number"], abbreviation)
            if key:
                parcels.setdefault(key, int(r["id"]))
        blocks: dict[str, int] = {}
        for r in (await session.execute(BLOCKS_SQL, params)).mappings():
            key = block_key(r["block_ref"], words)
            if key:
                blocks.setdefault(key, int(r["id"]))
        staged_parcels: set[str] = set()
        staged_blocks: set[str] = set()
        staged = await session.execute(
            STAGED_SQL, {"m": self.municipality_id, "document_ref": str(document_id)}
        )
        for r in staged.mappings():
            if r["layer_id"] == "urban_parcels" and r["number"]:
                key = parcel_key(r["number"], abbreviation)
            elif r["layer_id"] == "urban_blocks" and r["block_ref"]:
                key = block_key(r["block_ref"], words)
                if key:
                    staged_blocks.add(key)
                continue
            else:
                continue
            if key:
                staged_parcels.add(key)
        return parcels, blocks, staged_parcels, staged_blocks

    async def _write(
        self,
        run: Mapping[str, Any],
        document: Mapping[str, Any],
        manifest: PreprocessManifest,
        steps: Sequence[tuple[ChunkPlan, str]],
        tally: Tally,
        skipped_pages: list[int],
    ) -> dict[str, Any]:
        document_id = int(document["id"])
        model_version = tally.model_version or run["model"]
        now = self.clock()
        async with self.session_factory() as session:
            parcels, blocks, staged_parcels, staged_blocks = await self._targets(
                session, document_id
            )
            rows: list[dict[str, Any]] = []
            unstaged: Counter[str] = Counter()
            missing: Counter[str] = Counter()
            issues = 0
            for step in tally.steps:
                if step.result is None:
                    continue
                plan = to_staging_rows(
                    step.result,
                    extracted_by=f"llm:{model_version}",
                    parcel_ids=parcels,
                    block_ids=blocks,
                    job_id=run["job_id"],
                    run_id=run["id"],
                    unmatched="stage",
                    staged_parcels=staged_parcels,
                    staged_blocks=staged_blocks,
                )
                rows.extend(plan.rows)
                unstaged.update(u.reason for u in plan.unstaged)
                missing.update(plan.missing)
                issues += len(step.result.issues)
            seen = Counter((r["entity_type"], r["target_key"], r["field_key"]) for r in rows)
            previous = {
                (p["entity_type"], p["target_key"], p["field_key"]): p
                for p in (
                    await session.execute(
                        PREVIOUS_SQL,
                        {
                            "m": self.municipality_id,
                            "lineage_id": document["lineage_id"],
                            "run_id": run["id"],
                        },
                    )
                ).mappings()
            }
            changes: Counter[str] = Counter()
            for row in rows:
                key = (row["entity_type"], row["target_key"], row["field_key"])
                if seen[key] > 1:
                    row["flags"] = [*row["flags"], REPEATED]
                prior = previous.get(key)
                row["previous_item_id"] = prior["id"] if prior is not None else None
                row["change"] = value_change(prior, row)
                changes[row["change"]] += 1
                await session.execute(
                    INSERT_ITEM_SQL,
                    {
                        **row,
                        "source_bbox": json.dumps(row["source_bbox"])
                        if row["source_bbox"] is not None
                        else None,
                        "flags": json.dumps(row["flags"]),
                        "payload": json.dumps(row["payload"], ensure_ascii=False),
                    },
                )
            superseded = (
                await session.execute(
                    SUPERSEDE_SQL,
                    {
                        "m": self.municipality_id,
                        "run_id": run["id"],
                        "at": now,
                        "lineage_id": document["lineage_id"],
                        "document_id": document_id,
                        "sha256": document["sha256"],
                        "version": document["version"],
                    },
                )
            ).all()
            replaced_runs = sorted({int(r[1]) for r in superseded})
            if replaced_runs:
                await session.execute(
                    SUPERSEDE_RUNS_SQL, {"run_id": run["id"], "ids": replaced_runs}
                )

            usage = tally.usage
            cost = cost_for(model_version, usage.tokens_in, usage.output_tokens)
            done_chunks, failed_chunks = tally.chunks()
            failed = [
                {"page": page, "chunk": s.chunk_id, "task": s.task, "error": (s.error or "")[:300]}
                for s in tally.steps
                if s.status == "failed"
                for page in s.pages
            ]
            processed = sorted({p for s in tally.steps if s.status == "done" for p in s.pages})
            low = sum(1 for r in rows if str(Flag.low_confidence) in r["flags"])
            unmatched = sum(1 for r in rows if TARGET_UNMATCHED in r["flags"])
            staged_targets = sum(1 for r in rows if TARGET_STAGED in r["flags"])
            summary = {
                "run_id": run["id"],
                "status": "ready_for_review",
                "document_id": document_id,
                "file_id": document["file_id"],
                "file_sha256": document["sha256"],
                "model": run["model"],
                "model_version": model_version,
                "prompt_version": run["prompt_version"],
                "schema_version": run["schema_version"],
                "preprocess_version": manifest.version,
                "pages_total": len(manifest.pages),
                "pages_processed": processed,
                "pages_skipped": skipped_pages,
                "pages_failed": failed,
                "pages_failed_count": len({f["page"] for f in failed}),
                "chunks_planned": len(manifest.chunks),
                "chunks_read": len({c.id for c, _ in steps}),
                "chunks_without_planning_content": sum(1 for c in manifest.chunks if not c.tasks),
                "steps_total": len(steps),
                "steps_done": sum(1 for s in tally.steps if s.status == "done"),
                "steps_failed": sum(1 for s in tally.steps if s.status == "failed"),
                "llm_requests": sum(s.attempts for s in tally.steps),
                "items_written": len(rows),
                "items_low_confidence": low,
                "items_unmatched": unmatched,
                "items_staged_targets": staged_targets,
                "items_by_change": dict(changes),
                "items_superseded": len(superseded),
                "superseded_runs": replaced_runs,
                "unstaged": dict(unstaged),
                "missing": dict(missing),
                "issues": issues,
                "tokens_in": usage.tokens_in,
                "tokens_out": usage.output_tokens,
                "estimated_cost_eur": cost.estimated_cost_eur,
            }
            await session.execute(
                FINISH_SQL,
                {
                    "id": run["id"],
                    "at": now,
                    "model_version": tally.model_version,
                    "pages_total": len(manifest.pages),
                    "pages_processed": json.dumps(processed),
                    "pages_skipped": json.dumps(skipped_pages),
                    "pages_failed": json.dumps(failed),
                    "chunks_total": len({c.id for c, _ in steps}),
                    "chunks_done": len(done_chunks),
                    "chunks_failed": len(failed_chunks),
                    "items_written": len(rows),
                    "items_low_confidence": low,
                    "items_unmatched": unmatched,
                    "items_superseded": len(superseded),
                    "tokens_in": usage.tokens_in,
                    "tokens_out": usage.output_tokens,
                    "estimated_cost_eur": cost.estimated_cost_eur,
                    "summary": json.dumps(summary, default=str),
                },
            )
            await self._audit(
                session,
                "extraction.finish",
                run,
                {
                    "status": "ready_for_review",
                    "items_written": len(rows),
                    "items_low_confidence": low,
                    "items_unmatched": unmatched,
                    "items_superseded": len(superseded),
                    "pages_failed": sorted({f["page"] for f in failed}),
                    "pages_skipped": skipped_pages,
                    "tokens_in": usage.tokens_in,
                    "tokens_out": usage.output_tokens,
                },
            )
            await session.commit()
        return summary
