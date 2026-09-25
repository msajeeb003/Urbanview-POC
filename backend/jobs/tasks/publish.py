"""Publish job: ``publish_approved`` on the ``publish`` queue (one active run per municipality).

The body is ``jobs.publish_pipeline.PublishPipeline`` (approved review items and staged geometry
→ a new serving version → parcel links → heatmap cells → PMTiles archive → pointer flip). The
worker builds its own engine per run (no pool to share with the API); tests inject a fake
storage and tile builder with :func:`configure_publish`.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jobs.base import JobContext, JobResult, JobTask
from jobs.celery_app import celery_app
from jobs.publish_pipeline import PublishPipeline
from jobs.tiles import TippecanoeTileBuilder

log = logging.getLogger("urbanview.jobs.publish")

_config: dict[str, Any] = {}


def configure_publish(
    *,
    database_url: str | None = None,
    storage: Any | None = None,
    tile_builder: Any | None = None,
    settings: Any | None = None,
) -> None:
    """Override what the task would build from the settings (tests); ``None`` resets a key."""
    for key, value in (
        ("database_url", database_url),
        ("storage", storage),
        ("tile_builder", tile_builder),
        ("settings", settings),
    ):
        if value is None:
            _config.pop(key, None)
        else:
            _config[key] = value


def _settings() -> Any:
    if "settings" in _config:
        return _config["settings"]
    from core.config import get_settings

    return get_settings()


async def _publish_approved(job: JobContext) -> JobResult:
    settings = _settings()
    storage = _config.get("storage")
    if storage is None:
        from core.storage import ObjectStorage

        storage = ObjectStorage(settings)
    tile_builder = _config.get("tile_builder") or TippecanoeTileBuilder(
        settings.tippecanoe_bin, settings.tile_join_bin
    )
    engine = create_async_engine(
        _config.get("database_url") or settings.database_url, poolclass=NullPool
    )
    try:
        pipeline = PublishPipeline(
            async_sessionmaker(engine, expire_on_commit=False),
            storage=storage,
            tile_builder=tile_builder,
            municipality_id=job.municipality_id,
            min_overlap_m2=settings.locate_min_overlap_m2,
            min_overlap_fraction=settings.locate_min_overlap_fraction,
            keep_versions=settings.publish_keep_versions,
            min_zoom=settings.tiles_min_zoom,
            max_zoom=settings.tiles_max_zoom,
            tmp_dir=settings.publish_tmp_dir,
        )
        return await pipeline.run(job)
    finally:
        await engine.dispose()


@celery_app.task(bind=True, base=JobTask, name="jobs.tasks.publish.publish_approved")
def publish_approved(self: JobTask, job_id: int, municipality_id: str) -> dict:
    """Lifecycle-tracked publish run for one municipality."""
    return self.execute(job_id, municipality_id, _publish_approved)
