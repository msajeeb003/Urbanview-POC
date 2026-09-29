"""The API side of transactional e-mail: queue a send.

``queue`` inserts the ``email_log`` row (``queued``) and one ``send_email`` job for it (payload:
template and ids, never an address or a body); the worker does the rendering, the policy check,
the SMTP send and the outcome (``core.mail``, ``jobs.tasks.email``). A queue outage marks the row
``failed`` instead of failing the caller (an order is never lost over mail). The staff order
detail lists the order's rows (``EMAIL_LOG_JSON``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.email import EmailLogOut
from core.errors import ServiceUnavailableError
from core.mail.templates import TEMPLATES
from jobs.enqueue import JobDispatcher, enqueue_job

EMAIL_LOG_JSON = """jsonb_build_object(
    'id', e.id, 'template', e.template, 'to_email', e.to_email, 'order_id', e.order_id,
    'user_id', e.user_id, 'status', e.status, 'attempts', e.attempts, 'subject', e.subject,
    'provider_message_id', e.provider_message_id, 'error', e.error,
    'suppressed_reason', e.suppressed_reason, 'job_id', e.job_id, 'created_at', e.created_at,
    'sent_at', e.sent_at, 'updated_at', e.updated_at)"""

INSERT_SQL = text(
    """
    INSERT INTO email_log (municipality_id, order_id, user_id, to_email, template, status)
    VALUES (:m, :order_id, :user_id, :to_email, :template, 'queued') RETURNING id
    """
)
SET_JOB_SQL = text("UPDATE email_log SET job_id = :job_id, updated_at = now() WHERE id = :id")
QUEUE_FAILED_SQL = text(
    "UPDATE email_log SET status = 'failed', error = :error, updated_at = now() WHERE id = :id"
)
STATUS_SQL = text("SELECT status FROM email_log WHERE id = :id")


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def email_log_out(row: Mapping[str, Any]) -> EmailLogOut:
    data = dict(row)
    for key in ("created_at", "sent_at", "updated_at"):
        data[key] = _utc(data.get(key))
    return EmailLogOut(**data)


@dataclass(frozen=True, slots=True)
class EmailQueued:
    log_id: int
    job_id: int | None
    status: str  # queued, or the final status when the job ran inline (eager mode)


class EmailService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        dispatcher: JobDispatcher,
        municipality_id: str,
        max_attempts: int = 3,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.dispatcher = dispatcher
        self.municipality_id = municipality_id
        self.max_attempts = int(max_attempts)
        self.clock = clock

    async def queue(
        self,
        *,
        template: str,
        to: str,
        order_id: int | None = None,
        user_id: int | None = None,
        requested_by: str = "system",
        requested_by_user_id: int | None = None,
    ) -> EmailQueued:
        if template not in TEMPLATES:
            raise ValueError(f"unknown e-mail template {template!r}")
        m = self.municipality_id
        async with self.session_factory() as session:
            log_id = int(
                (
                    await session.execute(
                        INSERT_SQL,
                        {
                            "m": m,
                            "order_id": order_id,
                            "user_id": user_id,
                            "to_email": to.strip().lower(),
                            "template": template,
                        },
                    )
                ).scalar_one()
            )
            await session.commit()
        try:
            outcome = await enqueue_job(
                self.session_factory,
                self.dispatcher,
                municipality_id=m,
                job_type="send_email",
                payload={
                    "template": template,
                    "email_log_id": log_id,
                    "order_id": order_id,
                    "user_id": user_id,
                },
                target_type="email",
                target_id=log_id,
                max_attempts=self.max_attempts,
                requested_by=requested_by,
                requested_by_user_id=requested_by_user_id,
            )
        except ServiceUnavailableError as exc:
            error = f"queue unavailable: {exc}"
            async with self.session_factory() as session:
                await session.execute(QUEUE_FAILED_SQL, {"id": log_id, "error": error})
                await session.commit()
            return EmailQueued(log_id=log_id, job_id=None, status="failed")
        async with self.session_factory() as session:
            await session.execute(SET_JOB_SQL, {"id": log_id, "job_id": outcome.job_id})
            await session.commit()
            status = (await session.execute(STATUS_SQL, {"id": log_id})).scalar_one()
        return EmailQueued(log_id=log_id, job_id=outcome.job_id, status=str(status))
