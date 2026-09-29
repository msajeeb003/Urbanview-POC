"""GIS ingestion on the ``geo`` queue: ``process_geometry`` and ``import_zones``.

``process_geometry`` (``POST /v1/admin/files/{id}/jobs/geo``, payload ``{"file_id": …}``, target
``file``; the idempotency key carries the file's SHA-256, so the same content is processed once
while a job runs): a GIS drawing of a planning document (a QGIS redraw or the plan's official
GIS) is reprojected, snapped to the cadastral base, validated and staged as the document's
geometry (``jobs.geometry.GeometryRunner``); a PDF drawing gets the PDF stage (its scanned pages
flagged for a QGIS redraw) and a clear answer of what it needs, since a vector sheet is
georeferenced from control points by staff (``python -m core.gis.georef``).

``import_zones`` (``POST /v1/admin/zones/import``, target ``file``): a zone GeoPackage from
QGIS validated and staged by ``core.zones`` (``jobs.geometry.import_zones``).

Both write STAGING only; ``jobs.tasks.publish`` applies what is staged. What a job cannot do is a
final failure (``GeometryError``: never retried) whose message the Data sources list shows.
Cadastral bulk extracts have their own import (``python -m core.cadastre``).
"""

from __future__ import annotations

import logging
from typing import Any

from jobs.base import JobContext, JobResult, JobTask
from jobs.celery_app import celery_app

log = logging.getLogger("urbanview.jobs.ingestion")

_config: dict[str, Any] = {}


def configure_ingestion(
    *,
    database_url: str | None = None,
    storage: Any | None = None,
    ogr_factory: Any | None = None,
    preprocess: Any | None = None,
) -> None:
    """Override what the geo tasks would build from the settings (tests); ``None`` resets."""
    for key, value in (
        ("database_url", database_url),
        ("storage", storage),
        ("ogr_factory", ogr_factory),
        ("preprocess", preprocess),
    ):
        if value is None:
            _config.pop(key, None)
        else:
            _config[key] = value


def _resources() -> tuple[str, Any]:
    from core.config import get_settings

    settings = get_settings()
    storage = _config.get("storage")
    if storage is None:
        from core.storage import ObjectStorage

        storage = ObjectStorage(settings)
    return _config.get("database_url") or settings.database_url, storage


async def _process_geometry(job: JobContext) -> JobResult:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from jobs.geometry import GeometryRunner
    from jobs.tasks.extraction import run_preprocess

    file_id = int(job.payload.get("file_id") or job.file_id or job.target_id or 0)
    database_url, storage = _resources()

    async def preprocess(fid: int) -> dict[str, Any]:
        override = _config.get("preprocess")
        if override is not None:
            return await override(fid)
        return await run_preprocess(job.municipality_id, fid)

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        runner = GeometryRunner(
            async_sessionmaker(engine, expire_on_commit=False),
            storage,
            municipality_id=job.municipality_id,
            ogr_factory=_config.get("ogr_factory"),
            preprocess=preprocess,
        )
        result = await runner.run(file_id, job_id=job.id)
    finally:
        await engine.dispose()
    log.info("process_geometry done", extra={"job_id": job.id, "file_id": file_id})
    return JobResult(result=result)


async def _import_zones(job: JobContext) -> JobResult:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from jobs.geometry import import_zones

    file_id = int(job.payload.get("file_id") or job.file_id or job.target_id or 0)
    database_url, storage = _resources()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        result = await import_zones(
            async_sessionmaker(engine, expire_on_commit=False),
            storage,
            municipality_id=job.municipality_id,
            file_id=file_id,
            dry_run=bool(job.payload.get("dry_run")),
            job_id=job.id,
        )
    finally:
        await engine.dispose()
    log.info("import_zones done", extra={"job_id": job.id, "file_id": file_id})
    return JobResult(result=result)


@celery_app.task(bind=True, base=JobTask, name="jobs.tasks.ingestion.process_geometry")
def process_geometry(self: JobTask, job_id: int, municipality_id: str) -> dict:
    """Lifecycle-tracked geometry job for one stored file into STAGING."""
    return self.execute(job_id, municipality_id, _process_geometry)


@celery_app.task(bind=True, base=JobTask, name="jobs.tasks.ingestion.import_zones")
def import_zones_task(self: JobTask, job_id: int, municipality_id: str) -> dict:
    """Lifecycle-tracked zone GeoPackage import into STAGING."""
    return self.execute(job_id, municipality_id, _import_zones)
