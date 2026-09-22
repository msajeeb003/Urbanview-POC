"""AI extraction (build plan P1, AI track). Placeholder tasks; the rules are fixed here.

- ``extract_planning_parameters``: pymupdf text/tables -> LLM structured output -> one row per
  planning value with a source reference (document_id, page, snippet) and review state
  ``pending_review``. Nothing extracted is ever published without expert approval (100% review).
- ``normalise_market_data``: interprets Monstat statistics and client-provided price ranges into
  engine input values (land, sale, construction rates per zone) — reference values only.

The LLM never performs the financial arithmetic; that is the deterministic formula engine.
"""

from __future__ import annotations

import logging

from jobs.celery_app import celery_app

log = logging.getLogger("urbanview.jobs.extraction")


@celery_app.task(bind=True)
def extract_planning_parameters(self, document_id: int, municipality_id: str) -> dict:
    log.info(
        "extract_planning_parameters requested",
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
        "review_state": "pending_review",
    }


@celery_app.task(bind=True)
def normalise_market_data(self, source_id: str, municipality_id: str) -> dict:
    log.info(
        "normalise_market_data requested",
        extra={
            "source_id": source_id,
            "municipality_id": municipality_id,
            "task_id": self.request.id,
        },
    )
    return {"status": "not_implemented", "source_id": source_id, "municipality_id": municipality_id}
