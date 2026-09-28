"""The admin console's AI extraction page (``/v1/admin/ai``, role ``admin``).

What an admin needs to switch AI extraction on without a shell: whether a key is configured and
where it comes from (the server environment's ``ANTHROPIC_API_KEY`` wins over the key saved in
the console, ``core.extraction.credentials``), saving and removing the console key (encrypted,
write-only: ``core.app_secrets``), a connection test the worker runs (``ai_check`` job,
``jobs.tasks.ai``), the model settings, what AI jobs cost so far and a readiness checklist.

One statement reads everything the page shows (``STATUS_SQL``), the worker probe runs beside
it; the pure builders turn rows into the payload (unit-tested on canned rows). No builder, row
or response carries a key: only the last four characters of a key ever leave this module.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, get_args
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.admin import JobOut
from api.schemas.ai_settings import (
    ANTHROPIC_CONSOLE_BILLING_URL,
    ANTHROPIC_CONSOLE_KEYS_URL,
    AiChecklistItemOut,
    AiCheckOut,
    AiCheckStatus,
    AiKeyIn,
    AiKeyStateOut,
    AiLastRunOut,
    AiLinksOut,
    AiModelSettingsOut,
    AiStatusOut,
    AiUsageOut,
    AiUsageRowOut,
    AiWorkerOut,
)
from api.services.audit import write_audit
from api.services.jobs import JOB_JSON, JOB_SQL, job_out
from api.services.worker_probe import WorkerProbe, WorkerState
from core.app_secrets import (
    ANTHROPIC_API_KEY,
    StoredSecret,
    delete_secret,
    encrypt,
    encryption_ready,
    last4,
    load_secret,
    normalise_anthropic_key,
    readable,
    store_secret,
)
from core.auth import Principal
from core.errors import AppError, ConflictError, NotFoundError, ServiceUnavailableError
from core.extraction.credentials import ResolvedKey, env_key, missing_key_message, resolve_from
from jobs.enqueue import JobDispatcher, enqueue_job

log = logging.getLogger("urbanview.ai_settings")

__all__ = [
    "ANTHROPIC_CONSOLE_BILLING_URL",
    "ANTHROPIC_CONSOLE_KEYS_URL",
    "CHECK_TARGET",
    "USAGE_TYPES",
    "AiSettingsService",
    "build_checklist",
    "check_dedupe_key",
    "check_out",
    "key_state",
    "key_verified",
    "model_settings",
    "secret_from_row",
    "smtp_state",
    "usage_out",
]

USAGE_TYPES: tuple[str, ...] = ("extract_document", "import_market_data", "ai_check")
CHECK_TARGET = "ai_settings"
ACTIVE_JOB_STATUSES: tuple[str, ...] = ("queued", "running", "retrying")
CHECK_STATUSES: frozenset[str] = frozenset(get_args(AiCheckStatus))
DEFAULT_API_HOST = "api.anthropic.com"

USAGE_NOTE = "Estimated from LLM_PRICE_* settings for succeeded jobs; failed jobs are not priced."
ENCRYPTION_NOTE = (
    "SECRETS_ENCRYPTION_KEY is not set on the server: keys cannot be saved here (deploy.sh "
    "generates it; or use ANTHROPIC_API_KEY)."
)
INACTIVE_NOTE = "Inactive while the server environment sets a key (ANTHROPIC_API_KEY)."
NO_KEY_DETAIL = (
    "Paste a key below, or set ANTHROPIC_API_KEY in deploy/.env and recreate api and worker."
)
ENCRYPTION_MISSING_MESSAGE = (
    "Saving a key needs SECRETS_ENCRYPTION_KEY on the server (deploy/.env; deploy.sh generates "
    "it). Nothing was stored."
)
WORKER_ENV_HINT = (
    " The worker sees ANTHROPIC_API_KEY but the API does not: recreate both (up -d api worker)."
)
SOURCE_LABELS = {"server_env": "server environment", "console": "console", "none": "no key"}

LABELS = {
    "api_key": "Anthropic API key configured",
    "key_verified": "Key verified by the last connection test",
    "worker": "Worker listening on the extraction queue",
    "reviewer_account": "A reviewer or admin can sign in to review extracted values",
    "smtp": "E-mail configured for staff sign-in links",
}

STATUS_SQL = text(
    f"""
    SELECT
      (SELECT jsonb_build_object('id', s.id, 'name', s.name, 'ciphertext', s.ciphertext,
              'last4', s.last4, 'set_by', s.set_by, 'set_by_user_id', s.set_by_user_id,
              'set_at', s.set_at)
         FROM app_secrets s
        WHERE s.municipality_id = :m AND s.name = 'anthropic_api_key') AS secret,
      (SELECT {JOB_JSON} FROM pipeline_jobs j
        WHERE j.municipality_id = :m AND j.type = 'ai_check'
        ORDER BY j.requested_at DESC, j.id DESC LIMIT 1) AS check_job,
      (SELECT count(*) FROM staff_users u
        WHERE u.municipality_id = :m AND u.is_active
          AND u.role IN ('admin', 'reviewer')) AS reviewers,
      (SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY x.type), '[]'::jsonb) FROM (
         SELECT j.type, count(*) AS jobs,
                count(*) FILTER (WHERE j.status = 'succeeded') AS succeeded,
                count(*) FILTER (WHERE j.status = 'failed') AS failed,
                COALESCE(sum(j.llm_tokens_in), 0) AS tokens_in,
                COALESCE(sum(j.llm_tokens_out), 0) AS tokens_out,
                COALESCE(sum(j.estimated_cost_eur), 0) AS estimated_cost_eur,
                min(j.requested_at) AS first_requested_at,
                max(j.finished_at) AS last_finished_at
         FROM pipeline_jobs j
         WHERE j.municipality_id = :m
           AND j.type IN ('extract_document', 'import_market_data', 'ai_check')
         GROUP BY j.type) x) AS usage,
      (SELECT jsonb_build_object('job_id', j.id, 'status', j.status,
              'document_id', j.document_id, 'requested_at', j.requested_at,
              'finished_at', j.finished_at)
         FROM pipeline_jobs j
        WHERE j.municipality_id = :m AND j.type = 'extract_document'
        ORDER BY j.requested_at DESC, j.id DESC LIMIT 1) AS last_extraction
    """
)


# --- pure builders ---------------------------------------------------------------------------


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    """``YYYY-MM-DD HH:MM`` in UTC (the page's time format)."""
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def secret_from_row(row: Mapping[str, Any] | None) -> StoredSecret | None:
    """The ``secret`` object of ``STATUS_SQL`` (JSON: timestamps as strings) as a StoredSecret."""
    if not row:
        return None
    set_at = _utc(row["set_at"])
    assert set_at is not None
    return StoredSecret(
        id=int(row["id"]),
        name=row["name"],
        last4=row["last4"],
        set_by=row["set_by"],
        set_by_user_id=row.get("set_by_user_id"),
        set_at=set_at,
        ciphertext=row["ciphertext"],
    )


def key_state(settings: Any, secret: StoredSecret | None) -> tuple[AiKeyStateOut, ResolvedKey]:
    """The key the API sees (the worker resolves the same way) and the resolution behind it."""
    env = env_key(settings)
    resolved = resolve_from(settings, secret)
    note: str | None = None
    if secret is not None:
        if env is not None:
            note = INACTIVE_NOTE
        elif resolved.problem is not None:
            note = missing_key_message(resolved)
    state = AiKeyStateOut(
        configured=resolved.api_key is not None,
        source=resolved.source,
        last4=resolved.last4,
        server_env_key=env is not None,
        server_env_last4=last4(env) if env is not None else None,
        console_key_stored=secret is not None,
        console_key_last4=secret.last4 if secret is not None else None,
        console_key_active=(secret is not None and env is None and resolved.api_key is not None),
        console_key_readable=(
            readable(settings, secret.ciphertext) if secret is not None else None
        ),
        console_note_en=note,
        set_by=secret.set_by if secret is not None else None,
        set_at=secret.set_at if secret is not None else None,
    )
    return state, resolved


def _price(settings: Any, model: str) -> tuple[float, float]:
    from jobs.cost import parse_price_table

    defaults = (
        float(settings.llm_price_eur_per_mtok_input),
        float(settings.llm_price_eur_per_mtok_output),
    )
    try:
        table = parse_price_table(settings.llm_price_table)
    except ValueError:
        return defaults
    return table.get(model, defaults)


def base_url_host(settings: Any) -> str:
    return urlsplit(settings.anthropic_base_url).hostname or settings.anthropic_base_url


def model_settings(settings: Any) -> AiModelSettingsOut:
    host = base_url_host(settings)
    price_in, price_out = _price(settings, settings.extraction_model)
    return AiModelSettingsOut(
        model=settings.extraction_model,
        effort=settings.extraction_effort,
        adaptive_thinking=settings.extraction_adaptive_thinking,
        max_tokens=settings.extraction_max_tokens,
        refusal_fallback=settings.extraction_refusal_fallback,
        timeout_seconds=int(settings.extraction_timeout_seconds),
        market_model=settings.market_model or settings.extraction_model,
        market_effort=settings.market_effort,
        base_url_host=host,
        base_url_is_default=host == DEFAULT_API_HOST,
        price_input_eur_per_mtok=price_in,
        price_output_eur_per_mtok=price_out,
    )


def _same_key(source: str | None, last4_: str | None, set_at: Any, current: ResolvedKey) -> bool:
    """A fingerprint equals the current key's: source and last 4; a console key also by when it
    was saved (compared as instants, whatever the time zone of the text)."""
    if source != current.source or last4_ != current.last4:
        return False
    if current.source != "console":
        return True
    return _utc(set_at) == _utc(current.set_at)


def check_out(job: Mapping[str, Any] | None, current: ResolvedKey) -> AiCheckOut | None:
    """The latest ``ai_check`` job (``JOB_JSON``) as the page shows it."""
    if not job:
        return None
    out = job_out(job)
    result = out.result or {}
    expected = (out.payload or {}).get("expected") or {}
    if out.status in ACTIVE_JOB_STATUSES:
        status: str | None = None
        detail = None
    elif out.status == "succeeded":
        raw = result.get("status")
        status = raw if raw in CHECK_STATUSES else "error"
        detail = result.get("detail_en")
    else:  # failed / cancelled
        status = "error"
        detail = out.error or f"The test job {out.status}."
    if result:
        source, key_last4, set_at = (
            result.get("key_source"),
            result.get("key_last4"),
            result.get("key_set_at"),
        )
    else:  # still queued: the key the test was asked for
        source, key_last4, set_at = (
            expected.get("source"),
            expected.get("last4"),
            expected.get("set_at"),
        )
    return AiCheckOut(
        job=out,
        status=status,  # type: ignore[arg-type]
        detail_en=detail,
        model_requested=result.get("model_requested"),
        model_answered=result.get("model_answered"),
        latency_ms=result.get("latency_ms"),
        input_tokens=int(result.get("input_tokens") or 0),
        output_tokens=int(result.get("output_tokens") or 0),
        estimated_cost_eur=out.cost.estimated_cost_eur,
        key_source=source if source in SOURCE_LABELS else None,
        key_last4=key_last4,
        checked_at=_utc(result.get("checked_at")),
        matches_current_key=_same_key(source, key_last4, set_at, current),
    )


def key_verified(check: AiCheckOut | None, current: ResolvedKey) -> tuple[bool | None, str]:
    """The ``key_verified`` checklist item: ok only for a succeeded ``ok`` test of this key."""
    if check is None:
        return False, "Never tested: run Test connection."
    if check.job.status in ACTIVE_JOB_STATUSES:
        return None, "A connection test is running."
    if check.job.status != "succeeded":
        return False, f"The test job failed: {check.job.error or check.job.status}"
    if check.status != "ok":
        return False, check.detail_en or f"The last test ended {check.status}."
    if not check.matches_current_key:
        source = SOURCE_LABELS.get(check.key_source or "none", "no key")
        detail = f"The last test used a different key ({source}, ending {check.key_last4}): "
        detail += "test again."
        if check.key_source == "server_env" and current.source != "server_env":
            detail += WORKER_ENV_HINT
        return False, detail
    when = _stamp(check.checked_at or check.job.finished_at or check.job.requested_at)
    return (
        True,
        f"Verified {when} UTC: {check.model_answered} answered in {check.latency_ms} ms.",
    )


def smtp_state(settings: Any) -> tuple[bool, str]:
    """The ``smtp`` item, by the sending policy's rules (``core.mail.policy.decide``)."""
    if not settings.smtp_host:
        return (
            False,
            "SMTP_HOST is not set: sign-in links are not e-mailed "
            "(use python -m core.staff login-link).",
        )
    entries = [entry for entry in settings.mail_allowlist or [] if entry.strip()]
    app_env = getattr(settings.app_env, "value", settings.app_env)
    if app_env == "staging" and not entries:
        return False, "APP_ENV=staging with an empty MAIL_ALLOWLIST: no e-mail goes out."
    if entries:
        return True, f"Mail goes only to MAIL_ALLOWLIST ({len(entries)} entries)."
    return True, f"SMTP via {settings.smtp_host}:{settings.smtp_port}."


def _usage_row(type_: str, row: Mapping[str, Any] | None) -> AiUsageRowOut:
    row = row or {}
    return AiUsageRowOut(
        type=type_,  # type: ignore[arg-type]
        jobs=int(row.get("jobs") or 0),
        succeeded=int(row.get("succeeded") or 0),
        failed=int(row.get("failed") or 0),
        tokens_in=int(row.get("tokens_in") or 0),
        tokens_out=int(row.get("tokens_out") or 0),
        estimated_cost_eur=round(float(row.get("estimated_cost_eur") or 0), 4),
        last_finished_at=_utc(row.get("last_finished_at")),
    )


def usage_out(
    rows: Iterable[Mapping[str, Any]] | None, last_extraction: Mapping[str, Any] | None
) -> AiUsageOut:
    """Spend per AI job type (always the three, zeros when absent) and the total."""
    by_type = {row["type"]: row for row in rows or []}
    items = [_usage_row(type_, by_type.get(type_)) for type_ in USAGE_TYPES]
    finished = [r.last_finished_at for r in items if r.last_finished_at is not None]
    total = AiUsageRowOut(
        type="total",
        jobs=sum(r.jobs for r in items),
        succeeded=sum(r.succeeded for r in items),
        failed=sum(r.failed for r in items),
        tokens_in=sum(r.tokens_in for r in items),
        tokens_out=sum(r.tokens_out for r in items),
        estimated_cost_eur=round(sum(r.estimated_cost_eur for r in items), 4),
        last_finished_at=max(finished) if finished else None,
    )
    firsts = [
        first
        for type_ in USAGE_TYPES
        if (first := _utc((by_type.get(type_) or {}).get("first_requested_at"))) is not None
    ]
    last = None
    if last_extraction:
        requested_at = _utc(last_extraction["requested_at"])
        assert requested_at is not None
        last = AiLastRunOut(
            job_id=int(last_extraction["job_id"]),
            status=last_extraction["status"],
            document_id=last_extraction.get("document_id"),
            requested_at=requested_at,
            finished_at=_utc(last_extraction.get("finished_at")),
        )
    return AiUsageOut(
        rows=items,
        total=total,
        since=min(firsts) if firsts else None,
        last_extraction=last,
        note_en=USAGE_NOTE,
    )


def _api_key_detail(key: AiKeyStateOut) -> str:
    if key.configured and key.source == "server_env":
        return f"From the server environment (ANTHROPIC_API_KEY), ending {key.last4}."
    if key.configured and key.source == "console":
        when = _stamp(key.set_at) if key.set_at is not None else "an unknown date"
        return f"Saved in this console by {key.set_by} on {when} UTC, ending {key.last4}."
    if key.source == "console":  # saved but unusable: the reason
        return key.console_note_en or missing_key_message(ResolvedKey("none"))
    return NO_KEY_DETAIL


def build_checklist(
    *,
    key: AiKeyStateOut,
    verified: tuple[bool | None, str],
    worker: WorkerState,
    reviewers: int,
    smtp: tuple[bool, str],
) -> list[AiChecklistItemOut]:
    """The readiness checklist, always in this order: api_key, key_verified, worker,
    reviewer_account, smtp (the only optional item)."""
    worker_ok = {"ready": True, "eager": True, "no_worker": False}.get(worker.state)
    reviewer_detail = (
        f"{reviewers} active admin / reviewer account(s)."
        if reviewers >= 1
        else "No active admin or reviewer: python -m core.staff login-link --email … --create "
        "--role admin"
    )
    items: list[tuple[str, bool | None, bool, str]] = [
        ("api_key", key.configured, True, _api_key_detail(key)),
        ("key_verified", verified[0], True, verified[1]),
        ("worker", worker_ok, True, worker.detail_en or ""),
        ("reviewer_account", reviewers >= 1, True, reviewer_detail),
        ("smtp", smtp[0], False, smtp[1]),
    ]
    return [
        AiChecklistItemOut(
            key=name,  # type: ignore[arg-type]
            ok=ok,
            required=required,
            label_en=LABELS[name],
            detail_en=detail,
        )
        for name, ok, required, detail in items
    ]


def check_dedupe_key(current: ResolvedKey) -> str:
    """One active test per key: saving a new key never gets back a running test of the old."""
    set_at = current.set_at.astimezone(UTC).isoformat() if current.set_at is not None else "-"
    return f"ai_check:{CHECK_TARGET}:-:{current.source}:{current.last4 or '-'}:{set_at}"


# --- service -----------------------------------------------------------------------------------


class AiSettingsService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Any,
        dispatcher: JobDispatcher,
        municipality_id: str,
        worker_probe: WorkerProbe,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.dispatcher = dispatcher
        self.municipality_id = municipality_id
        self.worker_probe = worker_probe
        self.clock = clock

    async def _status_row(self) -> Mapping[str, Any]:
        async with self.session_factory() as session:
            return (await session.execute(STATUS_SQL, {"m": self.municipality_id})).mappings().one()

    async def status(self) -> AiStatusOut:
        row, worker = await asyncio.gather(self._status_row(), self.worker_probe.probe())
        key, current = key_state(self.settings, secret_from_row(row["secret"]))
        check = check_out(row["check_job"], current)
        checklist = build_checklist(
            key=key,
            verified=key_verified(check, current),
            worker=worker,
            reviewers=int(row["reviewers"] or 0),
            smtp=smtp_state(self.settings),
        )
        ready = encryption_ready(self.settings)
        return AiStatusOut(
            key=key,
            encryption_ready=ready,
            encryption_note_en=None if ready else ENCRYPTION_NOTE,
            model=model_settings(self.settings),
            check=check,
            worker=AiWorkerOut(
                state=worker.state,  # type: ignore[arg-type]
                workers=worker.workers,
                detail_en=worker.detail_en,
                checked_at=worker.checked_at,
            ),
            usage=usage_out(row["usage"], row["last_extraction"]),
            checklist=checklist,
            ready=all(item.ok is True for item in checklist if item.required),
            links=AiLinksOut(),
            generated_at=self.clock(),
        )

    async def set_key(self, principal: Principal, payload: AiKeyIn) -> AiStatusOut:
        """Encrypt and save the key (write-only), audit last 4 only, queue a connection test."""
        if not encryption_ready(self.settings):
            raise ConflictError(
                ENCRYPTION_MISSING_MESSAGE,
                code="encryption_key_missing",
                details={"env_var": "SECRETS_ENCRYPTION_KEY"},
            )
        try:
            value = normalise_anthropic_key(payload.api_key.get_secret_value())
        except ValueError as exc:
            raise AppError(
                "Request validation failed",
                code="validation_error",
                status_code=422,
                details=[{"loc": ["body", "api_key"], "msg": str(exc), "type": "value_error"}],
            ) from None
        env = env_key(self.settings)
        async with self.session_factory() as session:
            before = await load_secret(
                session,
                municipality_id=self.municipality_id,
                name=ANTHROPIC_API_KEY,
                for_update=True,
            )
            stored = await store_secret(
                session,
                municipality_id=self.municipality_id,
                name=ANTHROPIC_API_KEY,
                ciphertext=encrypt(self.settings, value),
                last4=last4(value),
                set_by=principal.subject,
                set_by_user_id=principal.user_id,
            )
            await write_audit(
                session,
                municipality_id=self.municipality_id,
                principal=principal,
                action="ai.key_set",
                entity_type="app_secret",
                entity_id=stored.id,
                before={
                    "configured": before is not None,
                    "last4": before.last4 if before is not None else None,
                },
                after={"configured": True, "last4": stored.last4, "active": env is None},
                details={"name": ANTHROPIC_API_KEY, "server_env_key": env is not None},
            )
            await session.commit()
        try:
            await self.check(principal, trigger="key_saved")
        except ServiceUnavailableError:
            log.warning("the connection test after saving a key could not be queued")
        return await self.status()

    async def remove_key(self, principal: Principal) -> AiStatusOut:
        async with self.session_factory() as session:
            deleted = await delete_secret(
                session, municipality_id=self.municipality_id, name=ANTHROPIC_API_KEY
            )
            if deleted is None:
                raise NotFoundError(
                    "No Anthropic API key is saved in this console",
                    details={"secret": ANTHROPIC_API_KEY},
                )
            await write_audit(
                session,
                municipality_id=self.municipality_id,
                principal=principal,
                action="ai.key_removed",
                entity_type="app_secret",
                entity_id=deleted.id,
                before={"configured": True, "last4": deleted.last4},
                after={"configured": False},
                details={"name": ANTHROPIC_API_KEY},
            )
            await session.commit()
        return await self.status()

    async def check(
        self, principal: Principal, *, trigger: Literal["manual", "key_saved"] = "manual"
    ) -> tuple[JobOut, bool]:
        """Queue a connection test of the key the API sees now (or return the active one)."""
        async with self.session_factory() as session:
            secret = await load_secret(
                session, municipality_id=self.municipality_id, name=ANTHROPIC_API_KEY
            )
        _, current = key_state(self.settings, secret)

        async def on_created(session: AsyncSession, job_id: int) -> None:
            await write_audit(
                session,
                municipality_id=self.municipality_id,
                principal=principal,
                action="ai.check",
                entity_type="pipeline_job",
                entity_id=job_id,
                details={
                    "trigger": trigger,
                    "key_source": current.source,
                    "key_last4": current.last4,
                },
            )

        async def on_dispatch_failed(session: AsyncSession, job_id: int, error: str) -> None:
            await write_audit(
                session,
                municipality_id=self.municipality_id,
                principal=principal,
                action="job.enqueue_failed",
                entity_type="pipeline_job",
                entity_id=job_id,
                details={"type": "ai_check", "error": error},
            )

        outcome = await enqueue_job(
            self.session_factory,
            self.dispatcher,
            municipality_id=self.municipality_id,
            job_type="ai_check",
            payload={"trigger": trigger, "expected": current.fingerprint},
            target_type=CHECK_TARGET,
            target_id=None,
            key=check_dedupe_key(current),
            max_attempts=1,
            requested_by=principal.subject,
            requested_by_user_id=principal.user_id,
            on_created=on_created,
            on_dispatch_failed=on_dispatch_failed,
        )
        async with self.session_factory() as session:
            job = (
                await session.execute(JOB_SQL, {"m": self.municipality_id, "id": outcome.job_id})
            ).scalar_one()
        return job_out(job), outcome.created
