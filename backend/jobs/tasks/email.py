"""Transactional e-mail: ``send_email`` on the ``email`` queue, one job per ``email_log`` row.

Payload ``{"template", "email_log_id", "order_id"?, "user_id"?}``: ids only. The body loads the
row, resolves the recipient and the facts from the order or the staff user at send time
(``core.mail.repository``), applies the sending policy (``core.mail.policy``), renders the
template, sends over SMTP and records the outcome on the row: ``sent`` with the provider's
message id, ``suppressed`` with the reason, ``failed`` with the error. Transient provider errors
(connection, timeout, 4xx) are raised as ``TransientError`` so the base task retries with
backoff; the row keeps the attempt count and the last error. A magic-link job mints the
single-use token itself (hash in ``staff_login_tokens``, the raw token only in the e-mail).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from api.services.order_mail import (
    OrderFacts,
    magic_link_context,
    order_delivered_context,
    order_status_url,
    payment_instructions_context,
)
from core.auth import generate_token, hash_token
from core.mail import (
    EmailMessage,
    MailPermanentError,
    MailTransientError,
    build_mime,
    decide,
    new_message_id,
    render,
)
from core.mail.repository import EmailLogRow, EmailRepository, SqlEmailRepository
from core.payments import BankTransferProvider
from jobs.base import JobContext, JobResult, JobTask, TransientError
from jobs.celery_app import celery_app

log = logging.getLogger("urbanview.jobs.email")

_config: dict[str, Any] = {}


def configure_email(
    *,
    database_url: str | None = None,
    transport: Any | None = None,
    storage: Any | None = None,
    settings: Any | None = None,
    repository: EmailRepository | None = None,
) -> None:
    """Override what the task would build from the settings (tests); ``None`` resets a key."""
    for key, value in (
        ("database_url", database_url),
        ("transport", transport),
        ("storage", storage),
        ("settings", settings),
        ("repository", repository),
    ):
        if value is None:
            _config.pop(key, None)
        else:
            _config[key] = value


def _settings() -> Any:
    if "settings" in _config:
        return _config["settings"]
    from core.config import get_settings

    return get_settings()


async def resolve_context(
    template: str,
    row: EmailLogRow,
    repo: EmailRepository,
    settings: Any,
    storage: Any,
    clock: Any,
) -> tuple[str, dict[str, Any]]:
    """The recipient address and the template context, from the order / staff user."""
    support = settings.order_support_email
    if template in ("payment_instructions", "order_delivered"):
        if row.order_id is None:
            raise MailPermanentError(f"{template} needs an order")
        order = await repo.load_order(row.order_id)
        if order is None:
            raise MailPermanentError(f"order {row.order_id} no longer exists")
        facts = OrderFacts(
            reference=order.reference,
            first_name=order.first_name,
            parcel_label=order.parcel_label,
            document_name=order.document_name,
            price_eur=order.price_eur,
            turnaround_business_days=order.turnaround_business_days,
            expected_by=order.expected_by,
            status_url=order_status_url(settings.order_public_base_url, order.reference),
            support_email=support,
            currency=order.currency,
        )
        if template == "payment_instructions":
            provider = BankTransferProvider(
                beneficiary=settings.order_bank_beneficiary,
                iban=settings.order_bank_iban,
                bank_name=settings.order_bank_name,
                swift=settings.order_bank_swift,
            )
            instructions = provider.instructions(
                reference=order.reference, amount_eur=order.price_eur
            )
            return order.email, payment_instructions_context(facts, instructions)
        if not order.report_key:
            raise MailPermanentError(f"order {order.reference} has no report to deliver")
        expires_in = settings.order_report_link_expires_seconds
        url = storage.presigned_get_url(
            order.report_key, expires_in, content_type="application/pdf", inline=False
        )
        return order.email, order_delivered_context(
            facts, url, clock() + timedelta(seconds=expires_in)
        )
    if template == "magic_link":
        if row.user_id is None:
            raise MailPermanentError("magic_link needs a staff user")
        user = await repo.load_staff_user(row.user_id)
        if user is None or not user.is_active:
            raise MailPermanentError(f"staff user {row.user_id} is missing or inactive")
        token = generate_token()
        expires_at = clock() + timedelta(seconds=settings.magic_link_expires_seconds)
        await repo.create_login_token(user.id, hash_token(token), expires_at, row.id)
        login_url = f"{settings.admin_base_url.rstrip('/')}/login?token={token}"
        return user.email, magic_link_context(
            email=user.email,
            login_url=login_url,
            expires_minutes=max(1, settings.magic_link_expires_seconds // 60),
            support_email=support,
        )
    raise MailPermanentError(f"unknown e-mail template {template!r}")


async def deliver(
    job: JobContext,
    repo: EmailRepository,
    transport: Any,
    settings: Any,
    storage: Any,
    clock: Any,
) -> JobResult:
    log_id = int(job.payload["email_log_id"])
    row = await repo.load_log(log_id)
    if row is None:
        raise MailPermanentError(f"email_log row {log_id} does not exist")
    if row.status in ("sent", "bounced", "suppressed"):
        return JobResult(result={"email_log_id": log_id, "status": row.status, "skipped": True})
    template = row.template
    recipient, context = await resolve_context(template, row, repo, settings, storage, clock)
    rendered = render(template, context, app_name=settings.mail_app_name)
    decision = decide(
        app_env=settings.app_env.value,
        smtp_host=settings.smtp_host,
        to=recipient,
        allowlist=settings.mail_allowlist,
    )
    if not decision.send:
        await repo.mark_suppressed(log_id, decision.reason or "suppressed", rendered.subject)
        log.info("email %s suppressed (%s)", log_id, decision.reason)
        return JobResult(
            result={"email_log_id": log_id, "status": "suppressed", "reason": decision.reason}
        )
    mime = build_mime(
        EmailMessage(
            to=[recipient],
            subject=rendered.subject,
            text=rendered.text,
            html=rendered.html,
            reply_to=settings.mail_reply_to or support_address(settings),
            headers={"X-UrbanView-Template": template},
        ),
        sender=settings.smtp_from,
        message_id=new_message_id(settings.smtp_from),
    )
    try:
        receipt = await asyncio.to_thread(transport.send, mime)
    except MailTransientError as exc:
        error = f"{type(exc).__name__}: {exc}"
        if job.attempts >= job.max_attempts:
            await repo.mark_failed(log_id, error, rendered.subject)
        else:
            await repo.mark_attempt(log_id, error, rendered.subject)
        raise TransientError(str(exc)) from exc
    except MailPermanentError as exc:
        await repo.mark_failed(log_id, f"{type(exc).__name__}: {exc}", rendered.subject)
        raise
    await repo.mark_sent(
        log_id,
        provider_message_id=receipt.provider_message_id,
        response=receipt.response,
        subject=rendered.subject,
    )
    return JobResult(
        result={
            "email_log_id": log_id,
            "status": "sent",
            "template": template,
            "provider_message_id": receipt.provider_message_id,
        }
    )


def support_address(settings: Any) -> str:
    return settings.order_support_email


async def _send_email(job: JobContext) -> JobResult:
    from datetime import UTC, datetime

    settings = _settings()
    transport = _config.get("transport")
    if transport is None:
        from core.mail import SmtpTransport

        transport = SmtpTransport.from_settings(settings) if settings.smtp_host else None
    storage = _config.get("storage")
    if storage is None:
        from core.storage import ObjectStorage

        storage = ObjectStorage(settings)
    repo = _config.get("repository")
    engine = None
    if repo is None:
        engine = create_async_engine(
            _config.get("database_url") or settings.database_url, poolclass=NullPool
        )
        repo = SqlEmailRepository(
            async_sessionmaker(engine, expire_on_commit=False), job.municipality_id
        )
    try:
        return await deliver(
            job, repo, transport, settings, storage, clock=lambda: datetime.now(UTC)
        )
    finally:
        if engine is not None:
            await engine.dispose()


@celery_app.task(bind=True, base=JobTask, name="jobs.tasks.email.send_email")
def send_email(self: JobTask, job_id: int, municipality_id: str) -> dict:
    """Lifecycle-tracked delivery of one transactional e-mail (retried on provider trouble)."""
    return self.execute(job_id, municipality_id, _send_email)
