"""Celery application.

Queues: ``extraction`` (LLM document extraction), ``geo`` (geometry processing), ``publish``,
``email`` and ``default`` (misc), so a big GIS job never blocks a small extraction. Every task is
a :class:`jobs.base.JobTask` fed with ``(job_id, municipality_id)``; nothing in ``jobs`` knows
Podgorica specifically. ``CELERY_TASK_ALWAYS_EAGER=true`` runs tasks inline (tests).

Run a worker:  celery -A jobs.celery_app worker --loglevel=info \\
                   -Q default,extraction,geo,publish,email
Monitor:       celery -A jobs.celery_app flower --port=5555
               (docker compose --profile monitoring up flower)
"""

from __future__ import annotations

from celery import Celery
from kombu import Queue

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
        "jobs.tasks.email",
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
    task_queues=[
        Queue("default"),
        Queue("extraction"),
        Queue("geo"),
        Queue("publish"),
        Queue("email"),
    ],
    task_routes={
        "jobs.tasks.extraction.*": {"queue": "extraction"},
        "jobs.tasks.ingestion.*": {"queue": "geo"},
        "jobs.tasks.publish.*": {"queue": "publish"},
        "jobs.tasks.email.*": {"queue": "email"},
    },
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=True,
    broker_connection_retry_on_startup=True,
    result_expires=60 * 60 * 24,
)
