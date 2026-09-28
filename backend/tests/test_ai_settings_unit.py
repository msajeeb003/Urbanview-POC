"""The AI extraction page's API (``/v1/admin/ai``): role gate, redacted validation errors, the
pure builders on canned rows and the worker probe. No database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.fernet import Fernet

from api.services.ai_settings import (
    ENCRYPTION_NOTE,
    INACTIVE_NOTE,
    NO_KEY_DETAIL,
    build_checklist,
    check_dedupe_key,
    check_out,
    key_state,
    key_verified,
    model_settings,
    secret_from_row,
    smtp_state,
    usage_out,
)
from api.services.worker_probe import CeleryWorkerProbe, WorkerState
from core.app_secrets import StoredSecret, encrypt
from core.auth import Principal, Role, bearer_token
from core.extraction.credentials import (
    ENCRYPTION_KEY_MISSING_MESSAGE,
    NO_KEY_MESSAGE,
    UNREADABLE_MESSAGE,
    ResolvedKey,
)
from tests.helpers import make_app, make_client, make_redis, make_settings

FAKE_KEY = "sk-ant-api03-" + "x" * 36 + "k1y1"
ENV_KEY = "sk-ant-api03-" + "e" * 36 + "env9"
FERNET = Fernet.generate_key().decode()
SET_AT = datetime(2026, 10, 10, 9, 14, 3, 123456, tzinfo=UTC)
NOW = datetime(2026, 10, 10, 9, 30, tzinfo=UTC)


class FakeStaffAuthenticator:
    def __init__(self, sessions: dict[str, Principal]) -> None:
        self.sessions = sessions

    async def authenticate(self, authorization):
        return self.sessions.get(bearer_token(authorization) or "")


STAFF = FakeStaffAuthenticator(
    {
        "sess-admin-0001": Principal("ana@example.com", Role.admin, user_id=7),
        "sess-review-001": Principal("bob@example.com", Role.reviewer, user_id=8),
        "sess-expert-001": Principal("eve@example.com", Role.expert, user_id=9),
    }
)
ROUTES = [
    ("GET", "/v1/admin/ai", None),
    ("PUT", "/v1/admin/ai/key", {"api_key": FAKE_KEY}),
    ("DELETE", "/v1/admin/ai/key", None),
    ("POST", "/v1/admin/ai/check", None),
]


def _settings(**overrides: Any):
    base: dict[str, Any] = {
        "anthropic_api_key": None,
        "secrets_encryption_key": FERNET,
        "smtp_host": None,
        "mail_allowlist": [],
        "rate_limit_requests": 100,
    }
    base.update(overrides)
    return make_settings(**base)


async def test_the_role_gate_on_every_route():
    app = make_app(_settings(), make_redis(), staff_authenticator=STAFF)
    async with app.router.lifespan_context(app), make_client(app) as client:
        for method, path, body in ROUTES:
            answers = {}
            for who, token in (
                ("anonymous", None),
                ("reviewer", "sess-review-001"),
                ("expert", "sess-expert-001"),
                ("admin", "sess-admin-0001"),
            ):
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                response = await client.request(method, path, json=body, headers=headers)
                answers[who] = response
                assert FAKE_KEY not in response.text
            assert answers["anonymous"].status_code == 401, path
            assert answers["reviewer"].status_code == 403, path
            assert answers["expert"].status_code == 403, path
            # the gate passed; the service needs PostGIS (503 under nodata)
            assert answers["admin"].status_code == 503, path
            assert answers["admin"].json()["error"]["code"] == "service_unavailable"


async def test_validation_errors_on_the_key_route_never_echo_the_input():
    app = make_app(_settings(), make_redis(), staff_authenticator=STAFF)
    headers = {"Authorization": "Bearer sess-admin-0001", "Content-Type": "application/json"}
    broken = f'{{"api_key": "{FAKE_KEY}"'  # no closing brace: parsed before the dependencies
    async with app.router.lifespan_context(app), make_client(app) as client:
        malformed = await client.put("/v1/admin/ai/key", content=broken, headers=headers)
        other_route = await client.post(
            "/v1/admin/engine/proposals", content=broken, headers=headers
        )
    assert malformed.status_code == 422
    body = malformed.json()["error"]
    assert body["code"] == "validation_error"
    assert body["details"] and body["details"][0]["type"] == "json_invalid"
    assert FAKE_KEY not in malformed.text
    assert all("input" not in d and "ctx" not in d for d in body["details"])
    # other routes keep the full detail
    assert other_route.status_code == 422
    assert all("ctx" in d for d in other_route.json()["error"]["details"])


async def test_openapi_marks_the_key_route():
    app = make_app(_settings(), make_redis())
    async with app.router.lifespan_context(app), make_client(app) as client:
        spec = (await client.get("/openapi.json")).json()
    put = spec["paths"]["/v1/admin/ai/key"]["put"]
    assert put["x-redact-input"] is True
    assert set(spec["paths"]["/v1/admin/ai/key"]) == {"put", "delete"}
    assert "202" in spec["paths"]["/v1/admin/ai/check"]["post"]["responses"]
    assert {"AiStatusOut", "AiKeyIn", "AiCheckOut"} <= set(spec["components"]["schemas"])


# --- builders --------------------------------------------------------------------------------


def _secret(settings, value: str = FAKE_KEY, *, ciphertext: str | None = None) -> StoredSecret:
    return StoredSecret(
        id=1,
        name="anthropic_api_key",
        last4=value[-4:],
        set_by="ana@example.com",
        set_by_user_id=7,
        set_at=SET_AT,
        ciphertext=ciphertext if ciphertext is not None else encrypt(settings, value),
    )


def test_secret_from_row_reads_json_timestamps():
    settings = _settings()
    row = {
        "id": 3,
        "name": "anthropic_api_key",
        "ciphertext": encrypt(settings, FAKE_KEY),
        "last4": "k1y1",
        "set_by": "ana@example.com",
        "set_by_user_id": None,
        "set_at": "2026-10-10T11:14:03.123456+02:00",
    }
    secret = secret_from_row(row)
    assert secret is not None and secret.set_at == SET_AT and secret.set_at.tzinfo is UTC
    assert FAKE_KEY not in repr(secret)
    assert secret_from_row(None) is None


def test_key_state_for_every_source():
    settings = _settings()
    # nothing anywhere
    state, current = key_state(settings, None)
    assert (state.configured, state.source, state.last4) == (False, "none", None)
    assert not state.console_key_stored and state.console_key_readable is None
    assert current.api_key is None

    # console only
    state, current = key_state(settings, _secret(settings))
    assert (state.configured, state.source, state.last4) == (True, "console", "k1y1")
    assert state.console_key_active and state.console_key_readable is True
    assert state.console_note_en is None and state.set_by == "ana@example.com"
    assert state.set_at == SET_AT and current.api_key == FAKE_KEY
    assert FAKE_KEY not in state.model_dump_json()

    # env only
    env = _settings(anthropic_api_key=ENV_KEY)
    state, current = key_state(env, None)
    assert (state.configured, state.source, state.last4) == (True, "server_env", "env9")
    assert state.server_env_key and state.server_env_last4 == "env9"
    assert current.fingerprint == {"source": "server_env", "last4": "env9", "set_at": None}

    # both: the environment wins, the console key is inactive
    state, _ = key_state(env, _secret(env))
    assert state.source == "server_env" and state.console_key_stored
    assert not state.console_key_active and state.console_note_en == INACTIVE_NOTE
    assert state.console_key_last4 == "k1y1"

    # stored, but no encryption key on this server
    stored = _secret(settings)
    missing = _settings(secrets_encryption_key=None)
    state, current = key_state(missing, stored)
    assert not state.configured and state.source == "console"
    assert state.console_key_readable is None
    assert state.console_note_en == ENCRYPTION_KEY_MISSING_MESSAGE
    assert current.problem == "encryption_key_missing"

    # stored under another key
    other = _settings(secrets_encryption_key=Fernet.generate_key().decode())
    state, current = key_state(other, stored)
    assert not state.configured and state.console_key_readable is False
    assert state.console_note_en == UNREADABLE_MESSAGE and current.problem == "unreadable"


def _job(**overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": 41,
        "kind": "extract",
        "type": "ai_check",
        "queue": "extraction",
        "status": "succeeded",
        "target_type": "ai_settings",
        "payload": {"trigger": "manual", "expected": {}},
        "max_attempts": 1,
        "requested_by": "ops",
        "requested_at": "2026-10-10T09:20:00+00:00",
        "finished_at": "2026-10-10T09:20:01+00:00",
        "error": None,
        "result": {
            "status": "ok",
            "detail_en": "claude-sonnet-5 answered in 812 ms.",
            "key_source": "console",
            "key_last4": "k1y1",
            "key_set_at": SET_AT.isoformat(),
            "model_requested": "claude-sonnet-5",
            "model_answered": "claude-sonnet-5",
            "latency_ms": 812,
            "input_tokens": 14,
            "output_tokens": 4,
            "checked_at": "2026-10-10T09:20:01+00:00",
        },
        "cost": {"estimated_cost_eur": 0.0001, "llm_model": "claude-sonnet-5"},
    }
    job.update(overrides)
    return job


CONSOLE = ResolvedKey("console", "k1y1", SET_AT, api_key=FAKE_KEY)


def test_check_out_and_key_verified():
    assert check_out(None, CONSOLE) is None
    assert key_verified(None, CONSOLE) == (False, "Never tested: run Test connection.")

    ok = check_out(_job(), CONSOLE)
    assert ok is not None and ok.status == "ok" and ok.matches_current_key
    assert (ok.input_tokens, ok.output_tokens, ok.latency_ms) == (14, 4, 812)
    assert ok.estimated_cost_eur == 0.0001 and ok.key_source == "console"
    assert key_verified(ok, CONSOLE) == (
        True,
        "Verified 2026-10-10 09:20 UTC: claude-sonnet-5 answered in 812 ms.",
    )
    # the same instant written in another zone still matches
    shifted = _job()
    shifted["result"] = {**shifted["result"], "key_set_at": "2026-10-10T11:14:03.123456+02:00"}
    assert check_out(shifted, CONSOLE).matches_current_key

    running = check_out(
        _job(
            status="running",
            result=None,
            finished_at=None,
            payload={"trigger": "key_saved", "expected": CONSOLE.fingerprint},
        ),
        CONSOLE,
    )
    assert running.status is None and running.matches_current_key
    assert key_verified(running, CONSOLE) == (None, "A connection test is running.")

    failed = check_out(_job(status="failed", result=None, error="RuntimeError: boom"), CONSOLE)
    assert failed.status == "error"
    assert key_verified(failed, CONSOLE) == (False, "The test job failed: RuntimeError: boom")

    credit = _job()
    credit["result"] = {**credit["result"], "status": "no_credit", "detail_en": "No credit."}
    assert key_verified(check_out(credit, CONSOLE), CONSOLE) == (False, "No credit.")

    newer = ResolvedKey("console", "zz99", SET_AT + timedelta(minutes=5), api_key="x")
    other = check_out(_job(), newer)
    assert not other.matches_current_key
    assert key_verified(other, newer) == (
        False,
        "The last test used a different key (console, ending k1y1): test again.",
    )
    # the same last 4 saved again later is another key too
    resaved = ResolvedKey("console", "k1y1", SET_AT + timedelta(seconds=1), api_key="x")
    assert not check_out(_job(), resaved).matches_current_key

    worker_env = _job()
    worker_env["result"] = {
        **worker_env["result"],
        "key_source": "server_env",
        "key_last4": "env9",
        "key_set_at": None,
    }
    ok_flag, detail = key_verified(check_out(worker_env, CONSOLE), CONSOLE)
    assert ok_flag is False
    assert detail.startswith("The last test used a different key (server environment, ending env9)")
    assert detail.endswith(
        "The worker sees ANTHROPIC_API_KEY but the API does not: recreate both (up -d api worker)."
    )
    env_now = ResolvedKey("server_env", "env9", api_key="y")
    assert key_verified(check_out(worker_env, env_now), env_now)[0] is True


def test_smtp_state():
    assert smtp_state(_settings()) == (
        False,
        "SMTP_HOST is not set: sign-in links are not e-mailed (use python -m core.staff "
        "login-link).",
    )

    class Staging:  # staging settings refuse dev defaults: a stand-in with the fields read
        smtp_host = "smtp.example.com"
        smtp_port = 587
        mail_allowlist: list[str] = []
        app_env = "staging"

    assert smtp_state(Staging()) == (
        False,
        "APP_ENV=staging with an empty MAIL_ALLOWLIST: no e-mail goes out.",
    )
    listed = _settings(smtp_host="smtp.example.com", mail_allowlist=["a@x.me", "@y.me", " "])
    assert smtp_state(listed) == (True, "Mail goes only to MAIL_ALLOWLIST (2 entries).")
    assert smtp_state(_settings(smtp_host="smtp.example.com", smtp_port=2525)) == (
        True,
        "SMTP via smtp.example.com:2525.",
    )


def _worker(state: str) -> WorkerState:
    return WorkerState(state, 1 if state in ("ready", "eager") else 0, f"{state} detail", NOW)


def test_build_checklist_order_required_and_ready():
    settings = _settings()
    state, _ = key_state(settings, _secret(settings))
    items = build_checklist(
        key=state,
        verified=(True, "Verified."),
        worker=_worker("ready"),
        reviewers=2,
        smtp=(False, "no smtp"),
    )
    assert [i.key for i in items] == [
        "api_key",
        "key_verified",
        "worker",
        "reviewer_account",
        "smtp",
    ]
    assert [i.required for i in items] == [True, True, True, True, False]
    assert all(i.ok is True for i in items if i.required)
    assert items[0].label_en == "Anthropic API key configured"
    assert items[0].detail_en == (
        "Saved in this console by ana@example.com on 2026-10-10 09:14 UTC, ending k1y1."
    )
    assert items[3].detail_en == "2 active admin / reviewer account(s)."
    assert items[4].ok is False and items[4].label_en.startswith("E-mail configured")

    none_state, _ = key_state(settings, None)
    items = build_checklist(
        key=none_state,
        verified=(False, "Never tested: run Test connection."),
        worker=_worker("unreachable"),
        reviewers=0,
        smtp=(True, "ok"),
    )
    assert items[0].ok is False and items[0].detail_en == NO_KEY_DETAIL
    assert items[2].ok is None and items[3].ok is False
    assert items[3].detail_en.startswith("No active admin or reviewer: python -m core.staff")
    assert not all(i.ok is True for i in items if i.required)
    assert (
        build_checklist(
            key=none_state,
            verified=(None, ""),
            worker=_worker("no_worker"),
            reviewers=1,
            smtp=(True, ""),
        )[2].ok
        is False
    )
    assert (
        build_checklist(
            key=none_state,
            verified=(None, ""),
            worker=_worker("eager"),
            reviewers=1,
            smtp=(True, ""),
        )[2].ok
        is True
    )

    env_state, _ = key_state(_settings(anthropic_api_key=ENV_KEY), None)
    item = build_checklist(
        key=env_state, verified=(None, ""), worker=_worker("ready"), reviewers=1, smtp=(True, "")
    )[0]
    assert item.detail_en == "From the server environment (ANTHROPIC_API_KEY), ending env9."
    broken, _ = key_state(_settings(secrets_encryption_key=None), _secret(settings))
    item = build_checklist(
        key=broken, verified=(None, ""), worker=_worker("ready"), reviewers=1, smtp=(True, "")
    )[0]
    assert item.ok is False and item.detail_en == ENCRYPTION_KEY_MISSING_MESSAGE


def test_usage_out_fills_zeros_and_totals():
    rows = [
        {
            "type": "extract_document",
            "jobs": 3,
            "succeeded": 2,
            "failed": 1,
            "tokens_in": 120_000,
            "tokens_out": 9_000,
            "estimated_cost_eur": 0.495,
            "first_requested_at": "2026-10-01T08:00:00+00:00",
            "last_finished_at": "2026-10-09T10:00:00+00:00",
        },
        {
            "type": "ai_check",
            "jobs": 1,
            "succeeded": 1,
            "failed": 0,
            "tokens_in": 14,
            "tokens_out": 4,
            "estimated_cost_eur": 0.0001,
            "first_requested_at": "2026-10-10T09:20:00+00:00",
            "last_finished_at": "2026-10-10T09:20:01+00:00",
        },
    ]
    last = {
        "job_id": 9,
        "status": "succeeded",
        "document_id": 4,
        "requested_at": "2026-10-09T09:00:00+00:00",
        "finished_at": "2026-10-09T10:00:00+00:00",
    }
    usage = usage_out(rows, last)
    assert [r.type for r in usage.rows] == ["extract_document", "import_market_data", "ai_check"]
    market = usage.rows[1]
    assert (market.jobs, market.tokens_in, market.estimated_cost_eur) == (0, 0, 0.0)
    assert market.last_finished_at is None
    assert (usage.total.jobs, usage.total.succeeded, usage.total.failed) == (4, 3, 1)
    assert (usage.total.tokens_in, usage.total.tokens_out) == (120_014, 9_004)
    assert usage.total.estimated_cost_eur == 0.4951 and usage.total.type == "total"
    assert usage.total.last_finished_at == datetime(2026, 10, 10, 9, 20, 1, tzinfo=UTC)
    assert usage.since == datetime(2026, 10, 1, 8, tzinfo=UTC)
    assert usage.last_extraction.job_id == 9 and usage.last_extraction.document_id == 4
    assert usage.currency == "EUR" and usage.note_en.startswith("Estimated from LLM_PRICE_*")
    empty = usage_out([], None)
    assert empty.total.jobs == 0 and empty.since is None and empty.last_extraction is None


def test_check_dedupe_key_names_the_key():
    assert check_dedupe_key(ResolvedKey("none")) == "ai_check:ai_settings:-:none:-:-"
    assert (
        check_dedupe_key(ResolvedKey("server_env", "env9", api_key="k"))
        == "ai_check:ai_settings:-:server_env:env9:-"
    )
    assert check_dedupe_key(CONSOLE) == (
        "ai_check:ai_settings:-:console:k1y1:2026-10-10T09:14:03.123456+00:00"
    )
    assert FAKE_KEY not in check_dedupe_key(CONSOLE)


def test_model_settings_prices_and_host():
    settings = _settings(
        extraction_model="claude-opus-5",
        extraction_effort=None,
        llm_price_table="claude-opus-5=5:25,claude-haiku-4-5=1:5",
        anthropic_base_url="https://proxy.example.com/anthropic",
        market_model=None,
    )
    model = model_settings(settings)
    assert (model.price_input_eur_per_mtok, model.price_output_eur_per_mtok) == (5.0, 25.0)
    assert model.base_url_host == "proxy.example.com" and not model.base_url_is_default
    assert model.market_model == "claude-opus-5" and model.effort is None
    default = model_settings(_settings())
    assert default.base_url_host == "api.anthropic.com" and default.base_url_is_default
    assert (default.price_input_eur_per_mtok, default.price_output_eur_per_mtok) == (3.0, 15.0)
    assert default.model == "claude-sonnet-5" and default.timeout_seconds == 600


def test_notes_are_the_contract_wording():
    assert ENCRYPTION_NOTE.startswith("SECRETS_ENCRYPTION_KEY is not set on the server")
    assert NO_KEY_MESSAGE.startswith("No Anthropic API key: set ANTHROPIC_API_KEY")


# --- worker probe -----------------------------------------------------------------------------


class FakeInspect:
    def __init__(self, replies: Any = None, error: Exception | None = None) -> None:
        self.replies = replies
        self.error = error
        self.calls = 0

    def __call__(self, timeout: float) -> FakeInspect:
        return self

    def active_queues(self) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.replies


@pytest.fixture
def celery(monkeypatch):
    from jobs.celery_app import celery_app

    monkeypatch.setattr(celery_app.conf, "task_always_eager", False)
    return celery_app


def _probe(monkeypatch, celery, inspect: FakeInspect, clock: list[float] | None = None):
    monkeypatch.setattr(celery.control, "inspect", inspect)
    ticks = clock if clock is not None else [0.0]
    return CeleryWorkerProbe(monotonic=lambda: ticks[0], now=lambda: NOW)


async def test_worker_probe_states(monkeypatch, celery):
    replies = {
        "celery@w1": [{"name": "default"}, {"name": "extraction"}],
        "celery@w2": [{"name": "extraction"}],
        "celery@w3": [{"name": "email"}],
    }
    state = await _probe(monkeypatch, celery, FakeInspect(replies)).probe()
    assert (state.state, state.workers) == ("ready", 2)
    assert state.detail_en == "2 worker(s) on the extraction queue" and state.checked_at == NOW

    for nothing in (None, {}):
        state = await _probe(monkeypatch, celery, FakeInspect(nothing)).probe()
        assert (state.state, state.workers) == ("no_worker", 0)
        assert state.detail_en.startswith("No worker answered")

    other = {"celery@w3": [{"name": "email"}]}
    state = await _probe(monkeypatch, celery, FakeInspect(other)).probe()
    assert state.state == "no_worker"
    assert state.detail_en == "1 worker(s) answered but none consumes the extraction queue"

    state = await _probe(monkeypatch, celery, FakeInspect(error=ConnectionError("down"))).probe()
    assert state.state == "unreachable"
    assert state.detail_en == "Could not reach the job queue (Redis): ConnectionError"


async def test_worker_probe_caches_and_reports_eager(monkeypatch, celery):
    ticks = [100.0]
    inspect = FakeInspect({"celery@w1": [{"name": "extraction"}]})
    probe = _probe(monkeypatch, celery, inspect, ticks)
    await probe.probe()
    ticks[0] = 110.0
    await probe.probe()
    assert inspect.calls == 1  # cached for 15 s
    ticks[0] = 116.0
    await probe.probe()
    assert inspect.calls == 2

    monkeypatch.setattr(celery.conf, "task_always_eager", True)
    eager = await CeleryWorkerProbe(now=lambda: NOW).probe()
    assert (eager.state, eager.workers) == ("eager", 1)
