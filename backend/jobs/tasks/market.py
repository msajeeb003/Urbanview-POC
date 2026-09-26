"""Market-data normalisation on the ``extraction`` queue: ``import_market_data``.

Target ``market_import`` (``POST /v1/admin/market/imports`` or ``/listings``): the import's table
or pasted listings, as recorded, through ``core.market`` (rules, then the LLM where the rules
cannot map a sheet or place an area name, ``MARKET_NORMALISE_LLM``) into ``market_data`` rows
waiting for review, in one transaction with the import's report. Nothing reaches the panel here:
only an approved review decision writes an assumptions version. The LLM interprets (columns,
area names, labels); every figure is read and computed by code.

The body reports the model and tokens so the base task records the estimated cost.
``core.extraction.llm.ModelUnavailable`` / ``ModelRateLimited`` become retries with backoff; a
refusal or an invalid answer does not fail the import (the rules' result stands and the report
says what the LLM could not do). A normalisation that fails hard marks the import ``failed``.

The module stays import-light (the API imports it to dispatch); the body imports what it uses.
"""

from __future__ import annotations

import logging
from typing import Any

from jobs.base import JobContext, JobResult, JobTask, RateLimited, TransientError
from jobs.celery_app import celery_app

log = logging.getLogger("urbanview.jobs.market")

_config: dict[str, Any] = {}


def configure_market(
    *,
    database_url: str | None = None,
    settings: Any | None = None,
    model_factory: Any | None = None,
) -> None:
    """Override what the task would build from the settings (tests); ``None`` resets a key."""
    for key, value in (
        ("database_url", database_url),
        ("settings", settings),
        ("model_factory", model_factory),
    ):
        if value is None:
            _config.pop(key, None)
        else:
            _config[key] = value


async def _import_market_data(job: JobContext) -> JobResult:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from core.extraction.llm import ModelRateLimited, ModelUnavailable
    from core.market.pipeline import MarketImporter, model_from_settings
    from core.municipality import load_profile
    from jobs.cost import cost_for

    settings = _config.get("settings")
    if settings is None:
        from core.config import get_settings

        settings = get_settings()
    import_id = int(job.payload.get("import_id") or job.target_id or 0)
    model_factory = _config.get("model_factory") or (lambda: model_from_settings(settings))
    engine = create_async_engine(
        _config.get("database_url") or settings.database_url, poolclass=NullPool
    )
    municipality = load_profile(job.municipality_id)
    importer = MarketImporter(
        async_sessionmaker(engine, expire_on_commit=False),
        municipality_id=job.municipality_id,
        municipality_name=municipality.name,
        settings=settings,
        model_factory=model_factory,
    )
    try:
        try:
            result = await importer.normalise(import_id, job_id=job.id)
        except ModelRateLimited as exc:
            raise RateLimited(str(exc)) from exc
        except ModelUnavailable as exc:
            raise TransientError(str(exc)) from exc
        except (TransientError, TimeoutError, ConnectionError):
            raise
        except Exception as exc:
            await importer.mark_failed(import_id, f"{type(exc).__name__}: {exc}", job_id=job.id)
            raise
    finally:
        await engine.dispose()
    log.info(
        "import_market_data done",
        extra={"job_id": job.id, "import_id": import_id, "items": result.get("items")},
    )
    cost: Any = None
    llm = result.get("llm")
    if llm and llm.get("calls"):
        tokens_in = llm["input_tokens"] + llm["cache_read_tokens"] + llm["cache_write_tokens"]
        cost = cost_for(llm["model"], tokens_in, llm["output_tokens"])
    return JobResult(result=result, cost=cost)


@celery_app.task(bind=True, base=JobTask, name="jobs.tasks.market.import_market_data")
def import_market_data(self: JobTask, job_id: int, municipality_id: str) -> dict:
    """Lifecycle-tracked normalisation of one market-data import into the review queue."""
    return self.execute(job_id, municipality_id, _import_market_data)
