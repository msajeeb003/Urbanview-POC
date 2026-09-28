"""AI extraction (build plan P1, AI track) on the ``extraction`` queue: ``preprocess_file`` and
``extract_document``.

``preprocess_file`` (``POST /v1/admin/files/{id}/jobs/preprocess``, target ``file``): the PDF
pre-processing stage, ``jobs.preprocessing.PreprocessRunner``: pages, tables, scanned pages and the
chunk plan persisted as a manifest on the file record, cached by checksum, plus page images per
registered document. The extraction job starts from its manifest.

``extract_document`` (``POST /v1/admin/documents/{id}/jobs/extract``, target ``document``): one
extraction run of the document version's file, ``jobs.extraction_runner.ExtractionRunner``: the
manifest (the PDF stage first when it is missing or stale), every chunk the plan marks as planning
content through its tasks (``core.extraction.run.run_task``: prompt set ``PROMPT_VERSION``,
structured output, validation into the contract), targets matched to the document's geometry,
and the items written to ``planning_parameter_extractions`` (STAGING, ``pending_review``) in one
transaction at the end, linked to the run (``extraction_runs``) and to the previous run's items;
what the run replaces is superseded, never deleted. Nothing extracted is ever published without
expert approval (100% review); publishing is a separate job. The LLM never performs the
financial arithmetic, nor any other: it transcribes, code normalises.

The extraction body reports the model and token counts through :class:`jobs.base.JobResult` /
:func:`jobs.cost.cost_for`, so the base task stores the estimated cost on the job; the result is
the run summary (``GET /v1/admin/jobs/{id}``). Transient model errors are retried in the job with
backoff, then by the job (``TransientError`` / ``RateLimited``), resuming from the checkpointed
steps; a step whose answer does not fit is asked once more with the error, then recorded as
failed without stopping the rest.

The module stays import-light (the API imports it to dispatch); the bodies import what they use.
"""

from __future__ import annotations

import logging
from typing import Any

from jobs.base import JobContext, JobResult, JobTask
from jobs.celery_app import celery_app

log = logging.getLogger("urbanview.jobs.extraction")

_config: dict[str, Any] = {}


def configure_preprocess(
    *,
    database_url: str | None = None,
    storage: Any | None = None,
    settings: Any | None = None,
    ocr: Any | None = None,
) -> None:
    """Override what the pre-processing task would build from the settings (tests); ``None``
    resets a key."""
    for key, value in (
        ("database_url", database_url),
        ("storage", storage),
        ("settings", settings),
        ("ocr", ocr),
    ):
        if value is None:
            _config.pop(key, None)
        else:
            _config[key] = value


async def _preprocess_file(job: JobContext) -> JobResult:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from core.extraction.chunking import SectionRules
    from core.extraction.preprocess import PreprocessOptions, ocr_from_settings
    from jobs.preprocessing import PreprocessRunner

    settings = _config.get("settings")
    if settings is None:
        from core.config import get_settings

        settings = get_settings()
    storage = _config.get("storage")
    if storage is None:
        from core.storage import ObjectStorage

        storage = ObjectStorage(settings)
    file_id = int(job.payload.get("file_id") or job.file_id or job.target_id or 0)
    engine = create_async_engine(
        _config.get("database_url") or settings.database_url, poolclass=NullPool
    )
    try:
        runner = PreprocessRunner(
            async_sessionmaker(engine, expire_on_commit=False),
            storage,
            municipality_id=job.municipality_id,
            options=PreprocessOptions.from_settings(settings),
            image_dpi=settings.preprocess_page_image_dpi,
            image_max_pixels=settings.preprocess_page_image_max_pixels,
            serve_images=settings.preprocess_serve_page_images,
            ocr=_config.get("ocr") or ocr_from_settings(settings),
            rules=SectionRules.from_profile(job.municipality_id),
        )
        result = await runner.run(file_id, force=bool(job.payload.get("force")))
    finally:
        await engine.dispose()
    log.info(
        "preprocess_file done",
        extra={"job_id": job.id, "file_id": file_id, "cached": result["cached"]},
    )
    return JobResult(result=result)


def configure_extraction(
    *,
    database_url: str | None = None,
    storage: Any | None = None,
    settings: Any | None = None,
    model: Any | None = None,
    sleep: Any | None = None,
) -> None:
    """Override what the extraction task would build from the settings (tests: a scripted
    model, fake storage, no backoff sleeps); ``None`` resets a key."""
    for key, value in (
        ("x_database_url", database_url),
        ("x_storage", storage),
        ("x_settings", settings),
        ("x_model", model),
        ("x_sleep", sleep),
    ):
        if value is None:
            _config.pop(key, None)
        else:
            _config[key] = value


async def _extract_document(job: JobContext) -> JobResult:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from core.extraction.chunking import SectionRules
    from core.extraction.preprocess import PreprocessOptions, ocr_from_settings
    from jobs.cost import cost_for
    from jobs.extraction_runner import ExtractionRunner
    from jobs.preprocessing import PreprocessRunner

    settings = _config.get("x_settings") or _config.get("settings")
    if settings is None:
        from core.config import get_settings

        settings = get_settings()
    storage = _config.get("x_storage") or _config.get("storage")
    if storage is None:
        from core.storage import ObjectStorage

        storage = ObjectStorage(settings)
    model = _config.get("x_model")
    if model is None:
        from core.extraction.evaluate import model_from_settings

        model = model_from_settings()
    database_url = (
        _config.get("x_database_url") or _config.get("database_url") or settings.database_url
    )
    engine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    options = PreprocessOptions.from_settings(settings)
    preprocess = PreprocessRunner(
        sessions,
        storage,
        municipality_id=job.municipality_id,
        options=options,
        image_dpi=settings.preprocess_page_image_dpi,
        image_max_pixels=settings.preprocess_page_image_max_pixels,
        serve_images=settings.preprocess_serve_page_images,
        ocr=_config.get("ocr") or ocr_from_settings(settings),
        rules=SectionRules.from_profile(job.municipality_id),
    )
    runner_kwargs: dict[str, Any] = {}
    if _config.get("x_sleep") is not None:
        runner_kwargs["sleep"] = _config["x_sleep"]
    runner = ExtractionRunner(
        sessions,
        storage,
        municipality_id=job.municipality_id,
        model=model,
        model_name=settings.extraction_model,
        options=options,
        preprocess=preprocess.run,
        low_confidence=settings.extraction_low_confidence,
        call_retries=settings.extraction_call_retries,
        retry_base_seconds=settings.extraction_retry_base_seconds,
        retry_max_seconds=settings.extraction_retry_max_seconds,
        max_steps=settings.extraction_max_chunks,
        **runner_kwargs,
    )
    try:
        summary, usage, model_version = await runner.run(job)
    finally:
        await engine.dispose()
    log.info(
        "extract_document done",
        extra={
            "job_id": job.id,
            "run_id": summary.get("run_id"),
            "items": summary.get("items_written"),
        },
    )
    cost = cost_for(
        model_version or settings.extraction_model, usage.tokens_in, usage.output_tokens
    )
    return JobResult(result=summary, cost=cost)


@celery_app.task(bind=True, base=JobTask, name="jobs.tasks.extraction.preprocess_file")
def preprocess_file(self: JobTask, job_id: int, municipality_id: str) -> dict:
    """Lifecycle-tracked pre-processing of one stored PDF (cached by checksum)."""
    return self.execute(job_id, municipality_id, _preprocess_file)


@celery_app.task(bind=True, base=JobTask, name="jobs.tasks.extraction.extract_document")
def extract_document(self: JobTask, job_id: int, municipality_id: str) -> dict:
    """Lifecycle-tracked extraction of one document version into STAGING."""
    return self.execute(job_id, municipality_id, _extract_document)
