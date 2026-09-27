"""Publish jobs on the ``publish`` queue: ``publish_approved`` (one active run per municipality)
and ``refresh_heatmaps`` (the current version's heatmaps and tiles again, when another assumptions
version applies).

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

from core.parcel_links import LinkRules
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


def _price_breaks(municipality_id: str) -> tuple[float, ...]:
    """The profile's €/m² bands of the sale-price heatmap (place data)."""
    from core.municipality import UnknownMunicipalityError, load_profile

    try:
        return tuple(load_profile(municipality_id).price_band_breaks_eur_m2)
    except UnknownMunicipalityError:
        return ()


def _timezone(municipality_id: str) -> str:
    """The profile's time zone: the cells use the assumptions that apply on its local date."""
    from core.municipality import UnknownMunicipalityError, load_profile

    try:
        return load_profile(municipality_id).timezone
    except UnknownMunicipalityError:
        return "UTC"


def _pipeline(engine: Any, municipality_id: str) -> PublishPipeline:
    settings = _settings()
    storage = _config.get("storage")
    if storage is None:
        from core.storage import ObjectStorage

        storage = ObjectStorage(settings)
    tile_builder = _config.get("tile_builder") or TippecanoeTileBuilder(
        settings.tippecanoe_bin, settings.tile_join_bin
    )
    return PublishPipeline(
        async_sessionmaker(engine, expire_on_commit=False),
        storage=storage,
        tile_builder=tile_builder,
        municipality_id=municipality_id,
        link_rules=LinkRules.from_settings(settings),
        price_breaks=_price_breaks(municipality_id),
        keep_versions=settings.publish_keep_versions,
        min_zoom=settings.tiles_min_zoom,
        max_zoom=settings.tiles_max_zoom,
        tmp_dir=settings.publish_tmp_dir,
        timezone=_timezone(municipality_id),
    )


def _engine() -> Any:
    settings = _settings()
    return create_async_engine(
        _config.get("database_url") or settings.database_url, poolclass=NullPool
    )


async def _publish_approved(job: JobContext) -> JobResult:
    engine = _engine()
    try:
        return await _pipeline(engine, job.municipality_id).run(job)
    finally:
        await engine.dispose()


async def _refresh_heatmaps(job: JobContext) -> JobResult:
    engine = _engine()
    try:
        return await _pipeline(engine, job.municipality_id).refresh(job)
    finally:
        await engine.dispose()


@celery_app.task(bind=True, base=JobTask, name="jobs.tasks.publish.publish_approved")
def publish_approved(self: JobTask, job_id: int, municipality_id: str) -> dict:
    """Lifecycle-tracked publish run for one municipality."""
    return self.execute(job_id, municipality_id, _publish_approved)


@celery_app.task(bind=True, base=JobTask, name="jobs.tasks.publish.refresh_heatmaps")
def refresh_heatmaps(self: JobTask, job_id: int, municipality_id: str) -> dict:
    """The current version's heatmaps and tiles again (another assumptions version applies)."""
    return self.execute(job_id, municipality_id, _refresh_heatmaps)
