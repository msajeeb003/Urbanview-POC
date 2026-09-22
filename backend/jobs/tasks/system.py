from __future__ import annotations

from jobs.celery_app import celery_app


@celery_app.task
def ping() -> str:
    """Smoke test for the worker/broker wiring: ``ping.delay().get() == "pong"``."""
    return "pong"
