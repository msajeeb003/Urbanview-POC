"""The API side of transactional e-mail: queue a send, read the log, record a bounce.

``queue`` inserts the ``email_log`` row (``queued``) and one ``send_email`` job for it (payload:
template and ids, never an address or a body); the worker does the rendering, the policy check,
the SMTP send and the outcome (``core.mail``, ``jobs.tasks.email``). A queue outage marks the row
``failed`` instead of failing the caller (an order is never lost over mail). Bounces reported by
the provider (webhook later, ``POST /v1/admin/email-log/{id}/bounce`` today) set ``bounced`` and
show up on the order as an alert.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.email import EmailLogList, EmailLogOut
from api.services.audit import write_audit
from core.auth import Principal
from core.errors import ConflictError, NotFoundError, ServiceUnavailableError
from core.mail.templates import TEMPLATES
from jobs.enqueue import JobDispatcher, enqueue_job

EMAIL_LOG_JSON = """jsonb_build_object(
    'id', e.id, 'template', e.template, 'to_email', e.to_email, 'order_id', e.order_id,
    'user_id', e.user_id, 'status', e.status, 'attempts', e.attempts, 'subject', e.subject,
    'provider_message_id', e.provider_message_id, 'error', e.error,
    'suppressed_reason', e.suppressed_reason, 'bounce_reason', e.bounce_reason, 'job_id', e.job_id,
    'created_at', e.created_at, 'sent_at', e.sent_at, 'bounced_at', e.bounced_at,
    'updated_at', e.updated_at)"""

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
ROW_SQL = text(
    f"SELECT {EMAIL_LOG_JSON} AS row FROM email_log e WHERE e.id = :id AND e.municipality_id = :m"
)
LIST_SQL = text(
    f"""
    SELECT {EMAIL_LOG_JSON} AS row, count(*) OVER () AS total
    FROM email_log e
    WHERE e.municipality_id = :m
      AND (CAST(:order_id AS bigint) IS NULL OR e.order_id = CAST(:order_id AS bigint))
      AND (CAST(:user_id AS bigint) IS NULL OR e.user_id = CAST(:user_id AS bigint))
      AND (CAST(:status AS text) IS NULL OR e.status = CAST(:status AS text))
      AND (CAST(:template AS text) IS NULL OR e.template = CAST(:template AS text))
    ORDER BY e.id DESC
    LIMIT :limit OFFSET :offset
    """
)
BOUNCE_SQL = text(
    """
    UPDATE email_log
    SET status = 'bounced', bounce_reason = :reason, bounced_at = now(), updated_at = now()
    WHERE id = :id
    """
)


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def email_log_out(row: Mapping[str, Any]) -> EmailLogOut:
    data = dict(row)
    for key in ("created_at", "sent_at", "bounced_at", "updated_at"):
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

    async def get(self, log_id: int) -> EmailLogOut:
        async with self.session_factory() as session:
            row = (
                await session.execute(ROW_SQL, {"id": log_id, "m": self.municipality_id})
            ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"No e-mail log entry {log_id}", details={"email_log_id": log_id})
        return email_log_out(row)

    async def list(
        self,
        *,
        order_id: int | None = None,
        user_id: int | None = None,
        status: str | None = None,
        template: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> EmailLogList:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        LIST_SQL,
                        {
                            "m": self.municipality_id,
                            "order_id": order_id,
                            "user_id": user_id,
                            "status": status,
                            "template": template,
                            "limit": limit,
                            "offset": offset,
                        },
                    )
                )
                .mappings()
                .all()
            )
        return EmailLogList(
            items=[email_log_out(r["row"]) for r in rows],
            total=int(rows[0]["total"]) if rows else 0,
            limit=limit,
            offset=offset,
        )

    async def mark_bounced(self, principal: Principal, log_id: int, reason: str) -> EmailLogOut:
        current = await self.get(log_id)
        if current.status != "sent":
            raise ConflictError(
                f"Only a sent e-mail can bounce; entry {log_id} is {current.status}",
                details={"email_log_id": log_id, "status": current.status},
            )
        async with self.session_factory() as session:
            await session.execute(BOUNCE_SQL, {"id": log_id, "reason": reason})
            await write_audit(
                session,
                municipality_id=self.municipality_id,
                principal=principal,
                action="email.bounce",
                entity_type="email_log",
                entity_id=log_id,
                details={
                    "template": current.template,
                    "order_id": current.order_id,
                    "user_id": current.user_id,
                    "provider_message_id": current.provider_message_id,
                },
                before={"status": "sent"},
                after={"status": "bounced"},
                note=reason,
            )
            await session.commit()
        return await self.get(log_id)
