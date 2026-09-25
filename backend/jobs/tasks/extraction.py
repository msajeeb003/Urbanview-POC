"""AI extraction (build plan P1, AI track): ``extract_document`` on the ``extraction`` queue.

The job the admin API queues with ``POST /v1/admin/documents/{id}/jobs/extract`` (payload
``{"document_id": …}``, target ``document``). When the AI track lands, the body will: read the
document version's PDF from the private bucket, run text + tables through the LLM with structured
output, and write one ``planning_parameter_extractions`` row (STAGING) per planning value with its
source reference (document, page, bbox, raw text, confidence) and ``review_state =
pending_review``. Nothing extracted is ever published without expert approval (100% review);
publishing is a separate job. The LLM never performs the financial arithmetic.

The body reports the model and token counts through :class:`jobs.base.JobResult` /
:func:`jobs.cost.cost_for`, so the base task stores the estimated cost on the job. Provider
timeouts and 429s are raised as :class:`jobs.base.TransientError` / :class:`RateLimited` and
retried with backoff; malformed documents fail hard. Market-data normalisation (Monstat
statistics and client-provided price ranges into per-zone engine inputs) will be a job of the same
family.
"""

from __future__ import annotations

import logging

from jobs.base import JobContext, JobResult, JobTask
from jobs.celery_app import celery_app

log = logging.getLogger("urbanview.jobs.extraction")


def _extract_document(job: JobContext) -> JobResult:
    log.info(
        "extract_document requested",
        extra={"job_id": job.id, "document_id": job.document_id, "attempt": job.attempts},
    )
    raise NotImplementedError("LLM extraction lands with the AI track item; nothing was extracted")


@celery_app.task(bind=True, base=JobTask, name="jobs.tasks.extraction.extract_document")
def extract_document(self: JobTask, job_id: int, municipality_id: str) -> dict:
    """Lifecycle-tracked extraction of one document version into STAGING."""
    return self.execute(job_id, municipality_id, _extract_document)
