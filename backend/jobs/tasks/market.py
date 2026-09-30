"""Market-data normalisation on the ``extraction`` queue: ``import_market_data``.

Target ``market_import`` (``POST /v1/admin/market/imports``): the import's table, as recorded,
through ``core.market``'s rules into ``market_data`` rows waiting for review, in one
transaction with the import's report (no AI: every mapping is a rule, every figure is read and
computed by code). Nothing reaches the panel here: only an approved review decision writes an
assumptions version. A normalisation that fails hard marks the import ``failed``.

The module stays import-light (the API imports it to dispatch); the body imports what it uses.
"""

from __future__ import annotations

import logging
from typing import Any

from jobs.base import JobContext, JobResult, JobTask, TransientError
from jobs.celery_app import celery_app

log = logging.getLogger("urbanview.jobs.market")

_config: dict[str, Any] = {}


def configure_market(
    *,
    database_url: str | None = None,
    settings: Any | None = None,
) -> None:
    """Override what the task would build from the settings (tests); ``None`` resets a key."""
    for key, value in (
        ("database_url", database_url),
        ("settings", settings),
    ):
        if value is None:
            _config.pop(key, None)
        else:
            _config[key] = value


async def _import_market_data(job: JobContext) -> JobResult:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from core.market.pipeline import MarketImporter
    from core.municipality import load_profile

    settings = _config.get("settings")
    if settings is None:
        from core.config import get_settings

        settings = get_settings()
    import_id = int(job.payload.get("import_id") or job.target_id or 0)
    engine = create_async_engine(
        _config.get("database_url") or settings.database_url, poolclass=NullPool
    )
    municipality = load_profile(job.municipality_id)
    importer = MarketImporter(
        async_sessionmaker(engine, expire_on_commit=False),
        municipality_id=job.municipality_id,
        municipality_name=municipality.name,
        settings=settings,
    )
    try:
        try:
            result = await importer.normalise(import_id, job_id=job.id)
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
    return JobResult(result=result)


@celery_app.task(bind=True, base=JobTask, name="jobs.tasks.market.import_market_data")
def import_market_data(self: JobTask, job_id: int, municipality_id: str) -> dict:
    """Lifecycle-tracked normalisation of one market-data import into the review queue."""
    return self.execute(job_id, municipality_id, _import_market_data)
