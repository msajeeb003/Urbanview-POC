"""Celery application.

Queues: ``default`` (misc), ``gis`` (ingestion), ``extraction`` (AI), ``publish``.
Every task takes ``municipality_id`` explicitly; nothing in ``jobs`` knows Podgorica specifically.

Run a worker:  celery -A jobs.celery_app worker --loglevel=info -Q default,gis,extraction,publish
"""

from __future__ import annotations

from celery import Celery

from core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "urbanview",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "jobs.tasks.system",
        "jobs.tasks.ingestion",
        "jobs.tasks.extraction",
        "jobs.tasks.publish",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    task_routes={
        "jobs.tasks.ingestion.*": {"queue": "gis"},
        "jobs.tasks.extraction.*": {"queue": "extraction"},
        "jobs.tasks.publish.*": {"queue": "publish"},
    },
    result_expires=60 * 60 * 24,
)
