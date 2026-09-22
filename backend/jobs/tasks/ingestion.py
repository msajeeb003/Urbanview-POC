"""GIS ingestion (build plan P1, GIS track). Placeholder tasks; pipeline shape is fixed here.

Per planning document, for a given ``municipality_id``:
1. fetch the PDF from object storage (``{municipality_id}/planning-documents/...``);
2. classify vector vs scanned (P0 gate 1 decides extraction vs manual redraw);
3. extract geometry (pymupdf + shapely) or import the redrawn GIS file (GDAL);
4. reproject from the profile's ``source_crs_epsg`` to ``serving_crs_epsg``;
5. load staging tables keyed by (municipality_id, document_id): cadastral parcels and planned
   urban parcels in SEPARATE tables, never merged; zones; blocks;
6. QA report (topology, area totals, unmatched parcels) for the reviewer.

Vector tiles (tippecanoe -> PMTiles) are rebuilt by ``jobs.tasks.publish`` once data is approved.
"""

from __future__ import annotations

import logging

from jobs.celery_app import celery_app

log = logging.getLogger("urbanview.jobs.ingestion")


@celery_app.task(bind=True)
def ingest_planning_document(self, document_id: int, municipality_id: str) -> dict:
    log.info(
        "ingest_planning_document requested",
        extra={
            "document_id": document_id,
            "municipality_id": municipality_id,
            "task_id": self.request.id,
        },
    )
    return {
        "status": "not_implemented",
        "document_id": document_id,
        "municipality_id": municipality_id,
    }


@celery_app.task(bind=True)
def import_cadastral_extract(self, extract_id: int, municipality_id: str) -> dict:
    """Load a cadastral bulk extract (UZN / eKatastar / eMapa) into the cadastral parcel table."""
    log.info(
        "import_cadastral_extract requested",
        extra={
            "extract_id": extract_id,
            "municipality_id": municipality_id,
            "task_id": self.request.id,
        },
    )
    return {
        "status": "not_implemented",
        "extract_id": extract_id,
        "municipality_id": municipality_id,
    }
