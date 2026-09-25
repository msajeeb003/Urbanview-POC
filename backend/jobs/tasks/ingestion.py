"""GIS ingestion (build plan P1, GIS track): ``process_geometry`` on the ``geo`` queue.

The job the admin API queues with ``POST /v1/admin/files/{id}/jobs/geo`` (payload
``{"file_id": …}``, target ``file``; the idempotency key carries the file's SHA-256, so the same
content is processed once). When the GIS track lands, the body will, for a stored GIS file or
vector PDF:

1. fetch the object from the private bucket (``stored_files.object_key``);
2. classify vector vs scanned (P0 gate 1 decides extraction vs manual redraw);
3. extract geometry (pymupdf + shapely) or import the redrawn GIS file (GDAL);
4. reproject from the profile's ``source_crs_epsg`` to ``serving_crs_epsg``;
5. load STAGING tables keyed by (municipality_id, file_id): cadastral parcels and planned urban
   parcels in SEPARATE tables, never merged; zones; blocks; document coverage;
6. produce a QA report (topology, area totals, unmatched parcels) for the reviewer.

Cadastral bulk extracts (UZN / eKatastar / eMapa) get their own import job. Vector tiles
(tippecanoe -> PMTiles) are rebuilt by ``jobs.tasks.publish`` once data is approved.
"""

from __future__ import annotations

import logging

from jobs.base import JobContext, JobResult, JobTask
from jobs.celery_app import celery_app

log = logging.getLogger("urbanview.jobs.ingestion")


def _process_geometry(job: JobContext) -> JobResult:
    log.info(
        "process_geometry requested",
        extra={"job_id": job.id, "file_id": job.file_id, "attempt": job.attempts},
    )
    raise NotImplementedError(
        "geometry extraction lands with the GIS track item; no layers were produced"
    )


@celery_app.task(bind=True, base=JobTask, name="jobs.tasks.ingestion.process_geometry")
def process_geometry(self: JobTask, job_id: int, municipality_id: str) -> dict:
    """Lifecycle-tracked geometry job for one stored file into STAGING."""
    return self.execute(job_id, municipality_id, _process_geometry)
