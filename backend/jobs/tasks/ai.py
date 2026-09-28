"""The admin console's AI connection test on the ``extraction`` queue: ``ai_check``.

Target ``ai_settings`` (``POST /v1/admin/ai/check``, or a key saved in the console): the worker
resolves the Anthropic API key the way the extraction and market jobs do (the server
environment's ``ANTHROPIC_API_KEY``, else the key saved in the console,
``core.extraction.credentials``) and sends one minimal Messages API call with it
(``core.extraction.connection``). The job **succeeds** whenever a check ran: ``result.status``
carries the outcome (``ok``, ``no_key``, ``invalid_key``, ``no_credit`` ...), with the key's
fingerprint (source, last four characters, when a console key was saved), the model asked and
the model that answered, latency and tokens; a check that used tokens reports its cost. Only an
internal error fails the job (one attempt: the enqueue sets ``max_attempts = 1``).

The key never leaves the process: it is not logged, not in the payload, and every string of the
result is scrubbed of it. The module stays import-light (the API imports it to dispatch); the
body imports what it uses.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from jobs.base import JobContext, JobResult, JobTask
from jobs.celery_app import celery_app

log = logging.getLogger("urbanview.jobs.ai")

_config: dict[str, Any] = {}

NO_SDK_DETAIL = "The worker has no Anthropic SDK (the backend 'ai' extra)."


def configure_ai_check(
    *,
    database_url: str | None = None,
    settings: Any | None = None,
    client_factory: Callable[..., Any] | None = None,
    resolver: Callable[[JobContext, Any], Awaitable[Any]] | None = None,
) -> None:
    """Override what the task would build from the settings (tests); ``None`` resets a key.

    ``client_factory(api_key=, base_url=, timeout_seconds=, max_retries=)`` returns an SDK
    client; ``resolver(job, settings)`` returns a ``ResolvedKey``."""
    for key, value in (
        ("database_url", database_url),
        ("settings", settings),
        ("client_factory", client_factory),
        ("resolver", resolver),
    ):
        if value is None:
            _config.pop(key, None)
        else:
            _config[key] = value


async def _resolve(job: JobContext, settings: Any) -> Any:
    resolver = _config.get("resolver")
    if resolver is not None:
        return await resolver(job, settings)
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from core.extraction.credentials import resolve_anthropic_key

    engine = create_async_engine(
        _config.get("database_url") or settings.database_url, poolclass=NullPool
    )
    try:
        return await resolve_anthropic_key(
            async_sessionmaker(engine, expire_on_commit=False),
            settings,
            municipality_id=job.municipality_id,
        )
    finally:
        await engine.dispose()


async def _ai_check(job: JobContext) -> JobResult:
    from core.app_secrets import scrub
    from core.extraction.connection import CHECK_TIMEOUT_SECONDS, check_connection
    from core.extraction.credentials import missing_key_message
    from core.extraction.llm import anthropic_client
    from jobs.cost import cost_for

    settings = _config.get("settings")
    if settings is None:
        from core.config import get_settings

        settings = get_settings()
    base_url_host = urlsplit(settings.anthropic_base_url).hostname or settings.anthropic_base_url
    resolved = await _resolve(job, settings)
    base: dict[str, Any] = {
        "trigger": job.payload.get("trigger", "manual"),
        "key_source": resolved.source,
        "key_last4": resolved.last4,
        "key_set_at": resolved.set_at.isoformat() if resolved.set_at is not None else None,
        "model_requested": settings.extraction_model,
        "base_url_host": base_url_host,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    if resolved.api_key is None:
        return JobResult(
            result={**base, "status": "no_key", "detail_en": missing_key_message(resolved)}
        )
    factory = _config.get("client_factory") or anthropic_client
    try:
        client = factory(
            api_key=resolved.api_key,
            base_url=settings.anthropic_base_url,
            timeout_seconds=CHECK_TIMEOUT_SECONDS,
            max_retries=0,
        )
    except ImportError:
        return JobResult(result={**base, "status": "error", "detail_en": NO_SDK_DETAIL})
    outcome = await asyncio.to_thread(
        check_connection, client, model=settings.extraction_model, base_url_host=base_url_host
    )
    result = {
        key: scrub(value, resolved.api_key) if isinstance(value, str) else value
        for key, value in {**base, **asdict(outcome)}.items()
    }
    cost = None
    if outcome.input_tokens or outcome.output_tokens:
        cost = cost_for(
            outcome.model_answered or settings.extraction_model,
            outcome.input_tokens,
            outcome.output_tokens,
        )
    log.info(
        "ai_check done",
        extra={
            "job_id": job.id,
            "status": outcome.status,
            "key_source": resolved.source,
            "latency_ms": outcome.latency_ms,
        },
    )
    return JobResult(result=result, cost=cost)


@celery_app.task(bind=True, base=JobTask, name="jobs.tasks.ai.ai_check")
def ai_check(self: JobTask, job_id: int, municipality_id: str) -> dict:
    """Lifecycle-tracked connection test of the Anthropic API with the resolved key."""
    return self.execute(job_id, municipality_id, _ai_check)
