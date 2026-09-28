"""The AI extraction page on PostGIS (``/v1/admin/ai``): the key saved encrypted and write-only,
its audit rows (last 4 only), the connection test job (queue, attempts, dedupe by key), the
readiness checklist and spend, a key the server cannot decrypt, removal, the role gate; the
extraction and market workers using the resolved key; and the connection test end to end with
eager Celery and the real SDK on a mock transport. No request leaves the machine."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from api.services.worker_probe import WorkerState
from core.app_secrets import decrypt, encrypt, store_secret
from core.extraction.credentials import UNREADABLE_MESSAGE
from core.staff import create_user, issue_session
from jobs.base import SqlJobStore, configure_job_store
from jobs.tasks.ai import configure_ai_check
from tests.helpers import make_app, make_client, make_settings
from tests.integration.test_admin_pipeline_postgis import CLEANUP as PIPELINE_CLEANUP
from tests.integration.test_admin_pipeline_postgis import FakeDispatcher

pytestmark = pytest.mark.integration

TOKEN = "admin-token-1234"
M = "podgorica"
KEY_A = "sk-ant-api03-" + "a" * 36 + "AAA1"
KEY_B = "sk-ant-api03-" + "b" * 36 + "BBB2"
ENV_KEY = "sk-ant-" + "x" * 40
FERNET = Fernet.generate_key().decode()
CLEANUP = (
    "DELETE FROM app_secrets",
    "DELETE FROM market_data",
    "DELETE FROM market_imports",
    *PIPELINE_CLEANUP,
)


class FakeWorkerProbe:
    def __init__(self, state: str = "ready") -> None:
        self.state = state

    async def probe(self) -> WorkerState:
        workers = 1 if self.state in ("ready", "eager") else 0
        return WorkerState(self.state, workers, f"{self.state} (test)", datetime.now(UTC))


@pytest.fixture(autouse=True)
async def _clean(postgis_url):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    async def clean() -> None:
        engine = create_async_engine(postgis_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine)() as session:
                for statement in CLEANUP:
                    await session.execute(text(statement))
                await session.commit()
        finally:
            await engine.dispose()

    await clean()
    yield
    await clean()


def settings_for(postgis_url, **overrides: Any):
    values: dict[str, Any] = dict(
        location_resolver="postgis",
        database_url=postgis_url,
        rate_limit_requests=100_000,
        admin_api_tokens=f"{TOKEN}:admin:ops",
        anthropic_api_key=None,
        secrets_encryption_key=FERNET,
        smtp_host=None,
        mail_allowlist=[],
    )
    values.update(overrides)
    return make_settings(**values)


def build(postgis_url, dispatcher=None, **overrides: Any):
    dispatcher = dispatcher if dispatcher is not None else FakeDispatcher()
    app = make_app(
        settings_for(postgis_url, **overrides),
        admin_dispatcher=dispatcher,
        worker_probe=FakeWorkerProbe("ready"),
    )
    return app, dispatcher


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def query(app, sql: str, **params) -> list[dict]:
    async with app.state.session_factory() as session:
        return [dict(r) for r in (await session.execute(text(sql), params)).mappings()]


def item(status: dict, key: str) -> dict:
    return next(i for i in status["checklist"] if i["key"] == key)


async def test_initial_status(postgis_url):
    app, _ = build(postgis_url)
    async with app.router.lifespan_context(app), make_client(app) as client:
        response = await client.get("/v1/admin/ai", headers=auth())
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    status = response.json()
    assert status["key"]["source"] == "none" and not status["key"]["configured"]
    assert status["encryption_ready"] and status["encryption_note_en"] is None
    assert [i["key"] for i in status["checklist"]] == [
        "api_key",
        "key_verified",
        "worker",
        "reviewer_account",
        "smtp",
    ]
    assert item(status, "api_key")["ok"] is False
    assert item(status, "worker")["ok"] is True
    assert item(status, "smtp")["ok"] is False and not item(status, "smtp")["required"]
    assert item(status, "smtp")["detail_en"].startswith("SMTP_HOST is not set")
    assert status["ready"] is False and status["check"] is None
    assert status["links"] == {
        "api_keys_url": "https://console.anthropic.com/settings/keys",
        "billing_url": "https://console.anthropic.com/settings/billing",
    }
    assert [r["type"] for r in status["usage"]["rows"]] == [
        "extract_document",
        "import_market_data",
        "ai_check",
    ]
    assert all(r["jobs"] == 0 for r in status["usage"]["rows"])
    assert status["usage"]["total"]["estimated_cost_eur"] == 0
    assert status["model"]["model"] == "claude-sonnet-5"
    assert status["model"]["base_url_is_default"]


async def test_saving_a_key_encrypts_it_audits_last4_and_queues_a_test(postgis_url):
    app, dispatcher = build(postgis_url)
    async with app.router.lifespan_context(app), make_client(app) as client:
        response = await client.put(
            "/v1/admin/ai/key", json={"api_key": f"  {KEY_A}\n"}, headers=auth()
        )
        assert response.status_code == 200, response.text
        assert KEY_A not in response.text
        status = response.json()
        assert status["key"]["console_key_last4"] == "AAA1"
        assert status["key"]["source"] == "console" and status["key"]["configured"]
        assert status["key"]["console_key_active"] and status["key"]["set_by"] == "ops"
        assert item(status, "api_key")["ok"] is True
        assert item(status, "api_key")["detail_en"].startswith("Saved in this console by ops")
        assert status["check"]["job"]["status"] == "queued" and status["check"]["status"] is None
        assert item(status, "key_verified")["ok"] is None
        again = (await client.get("/v1/admin/ai", headers=auth())).text
        assert KEY_A not in again

    (row,) = await query(app, "SELECT * FROM app_secrets WHERE municipality_id = :m", m=M)
    assert row["ciphertext"] != KEY_A and KEY_A not in row["ciphertext"]
    assert decrypt(app.state.settings, row["ciphertext"]) == KEY_A
    assert row["last4"] == "AAA1" and row["set_by"] == "ops" and row["set_by_user_id"] is None

    (audit,) = await query(
        app, "SELECT * FROM audit_log WHERE action = 'ai.key_set' AND entity_id = :id", id=row["id"]
    )
    assert audit["after"]["last4"] == "AAA1" and audit["after"]["active"] is True
    assert audit["before"] == {"configured": False, "last4": None}
    assert KEY_A not in json.dumps([audit["before"], audit["after"], audit["details"]])

    (call,) = dispatcher.calls
    assert call[0] == "ai_check" and call[2] == M
    (job,) = await query(app, "SELECT * FROM pipeline_jobs WHERE id = :id", id=call[1])
    assert (job["max_attempts"], job["queue"], job["kind"]) == (1, "extraction", "extract")
    assert job["target_type"] == "ai_settings" and job["target_id"] is None
    assert job["payload"]["trigger"] == "key_saved"
    assert job["payload"]["expected"]["last4"] == "AAA1"
    assert job["dedupe_key"].startswith("ai_check:ai_settings:-:console:AAA1:")
    assert KEY_A not in json.dumps(job, default=str)
    (check,) = await query(
        app, "SELECT * FROM audit_log WHERE action = 'ai.check' AND entity_id = :id", id=call[1]
    )
    assert check["details"] == {
        "trigger": "key_saved",
        "key_source": "console",
        "key_last4": "AAA1",
    }


async def test_without_an_encryption_key_nothing_is_stored(postgis_url):
    app, dispatcher = build(postgis_url, secrets_encryption_key=None)
    async with app.router.lifespan_context(app), make_client(app) as client:
        response = await client.put("/v1/admin/ai/key", json={"api_key": KEY_A}, headers=auth())
        status = (await client.get("/v1/admin/ai", headers=auth())).json()
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "encryption_key_missing"
    assert error["details"] == {"env_var": "SECRETS_ENCRYPTION_KEY"}
    assert KEY_A not in response.text
    assert await query(app, "SELECT id FROM app_secrets") == []
    assert dispatcher.calls == []
    assert not status["encryption_ready"]
    assert status["encryption_note_en"].startswith("SECRETS_ENCRYPTION_KEY is not set")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("sk-proj-" + "p" * 40, "An Anthropic API key starts with sk-ant-."),
        ("sk-ant-admin01-" + "q" * 40, "This is an Admin API key: paste a regular API key."),
        ("sk-ant-api03-" + "r" * 10, "The key is too short to be an Anthropic API key."),
        (
            "sk-ant-api03-" + "s" * 20 + " " + "s" * 20,
            "The key must not contain spaces or line breaks.",
        ),
        (
            "sk-ant-api03-" + "t" * 30 + "$$",
            "The key contains characters an Anthropic API key does not use.",
        ),
        ("   ", "Paste the API key."),
    ],
)
async def test_invalid_keys_are_refused_without_echo(postgis_url, value, message):
    app, dispatcher = build(postgis_url)
    async with app.router.lifespan_context(app), make_client(app) as client:
        response = await client.put("/v1/admin/ai/key", json={"api_key": value}, headers=auth())
        misnamed = await client.put("/v1/admin/ai/key", json={"apikey": KEY_A}, headers=auth())
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"] == [{"loc": ["body", "api_key"], "msg": message, "type": "value_error"}]
    if value.strip():
        assert value not in response.text
    assert misnamed.status_code == 422 and KEY_A not in misnamed.text
    assert all("input" not in d for d in misnamed.json()["error"]["details"])
    assert await query(app, "SELECT id FROM app_secrets") == [] and dispatcher.calls == []


async def test_a_new_key_gets_a_new_test(postgis_url):
    app, dispatcher = build(postgis_url)
    async with app.router.lifespan_context(app), make_client(app) as client:
        first = (
            await client.put("/v1/admin/ai/key", json={"api_key": KEY_A}, headers=auth())
        ).json()
        second = (
            await client.put("/v1/admin/ai/key", json={"api_key": KEY_B}, headers=auth())
        ).json()
    assert second["key"]["console_key_last4"] == "BBB2"
    assert second["key"]["set_at"] > first["key"]["set_at"]
    assert len(dispatcher.calls) == 2 and dispatcher.calls[0][1] != dispatcher.calls[1][1]
    assert second["check"]["job"]["id"] == dispatcher.calls[1][1]
    audits = await query(app, "SELECT before, after FROM audit_log WHERE action = 'ai.key_set'")
    assert audits[-1]["before"] == {"configured": True, "last4": "AAA1"}


async def test_the_check_route_is_idempotent_per_key(postgis_url):
    app, dispatcher = build(postgis_url)
    async with app.router.lifespan_context(app), make_client(app) as client:
        first = await client.post("/v1/admin/ai/check", headers=auth())
        second = await client.post("/v1/admin/ai/check", headers=auth())
    assert first.status_code == 202, first.text
    assert second.status_code == 200 and second.json()["id"] == first.json()["id"]
    job = first.json()
    assert job["type"] == "ai_check" and job["target_type"] == "ai_settings"
    assert job["status_url"] == f"/v1/admin/jobs/{job['id']}"
    assert job["dedupe_key"] == "ai_check:ai_settings:-:none:-:-"
    assert len(dispatcher.calls) == 1


async def test_a_dead_broker_records_the_test_as_failed(postgis_url):
    app, _ = build(postgis_url, FakeDispatcher(fail=True))
    async with app.router.lifespan_context(app), make_client(app) as client:
        response = await client.post("/v1/admin/ai/check", headers=auth())
        status = (await client.get("/v1/admin/ai", headers=auth())).json()
        saved = await client.put("/v1/admin/ai/key", json={"api_key": KEY_A}, headers=auth())
    assert response.status_code == 503
    details = response.json()["error"]["details"]
    assert details["job_id"] and details["status_url"] == f"/v1/admin/jobs/{details['job_id']}"
    assert status["check"]["status"] == "error" and status["check"]["job"]["status"] == "failed"
    assert item(status, "key_verified")["ok"] is False
    assert item(status, "key_verified")["detail_en"].startswith("The test job failed: queue")
    # the key stays saved when the test cannot be queued
    assert saved.status_code == 200 and saved.json()["key"]["console_key_last4"] == "AAA1"
    failed = await query(app, "SELECT details FROM audit_log WHERE action = 'job.enqueue_failed'")
    assert any(row["details"].get("type") == "ai_check" for row in failed)


async def _secret_row(app) -> dict:
    (row,) = await query(app, "SELECT id, last4, set_at FROM app_secrets")
    return row


async def _insert_check(app, result: dict[str, Any]) -> int:
    async with app.state.session_factory() as session:
        job_id = (
            await session.execute(
                text(
                    "INSERT INTO pipeline_jobs (municipality_id, kind, type, queue, status, "
                    "target_type, payload, max_attempts, attempts, requested_by, requested_at, "
                    "started_at, finished_at, result, llm_model, llm_tokens_in, llm_tokens_out, "
                    "estimated_cost_eur) VALUES (:m, 'extract', 'ai_check', 'extraction', "
                    "'succeeded', 'ai_settings', '{}'::jsonb, 1, 1, 'test', "
                    "clock_timestamp(), clock_timestamp(), clock_timestamp(), "
                    "CAST(:result AS jsonb), 'claude-sonnet-5', 14, 4, 0.0001) RETURNING id"
                ),
                {"m": M, "result": json.dumps(result)},
            )
        ).scalar_one()
        await session.commit()
    return int(job_id)


def ok_result(source: str, last4: str, set_at: str | None) -> dict[str, Any]:
    return {
        "status": "ok",
        "detail_en": "claude-sonnet-5 answered in 640 ms.",
        "key_source": source,
        "key_last4": last4,
        "key_set_at": set_at,
        "model_requested": "claude-sonnet-5",
        "model_answered": "claude-sonnet-5",
        "latency_ms": 640,
        "input_tokens": 14,
        "output_tokens": 4,
        "checked_at": datetime.now(UTC).isoformat(),
    }


async def test_key_verified_follows_the_fingerprint(postgis_url):
    app, _ = build(postgis_url)
    async with app.router.lifespan_context(app), make_client(app) as client:
        await client.put("/v1/admin/ai/key", json={"api_key": KEY_A}, headers=auth())
        secret = await _secret_row(app)
        await _insert_check(app, ok_result("console", "AAA1", secret["set_at"].isoformat()))
        verified = (await client.get("/v1/admin/ai", headers=auth())).json()
        await client.put("/v1/admin/ai/key", json={"api_key": KEY_B}, headers=auth())
        secret = await _secret_row(app)
        await _insert_check(app, ok_result("console", "AAA1", secret["set_at"].isoformat()))
        stale = (await client.get("/v1/admin/ai", headers=auth())).json()
    assert verified["check"]["status"] == "ok" and verified["check"]["matches_current_key"]
    assert item(verified, "key_verified")["ok"] is True
    assert item(verified, "key_verified")["detail_en"].endswith(
        "claude-sonnet-5 answered in 640 ms."
    )
    assert verified["check"]["estimated_cost_eur"] == 0.0001
    assert item(stale, "key_verified")["ok"] is False
    assert item(stale, "key_verified")["detail_en"] == (
        "The last test used a different key (console, ending AAA1): test again."
    )


async def test_the_server_environment_key_wins(postgis_url):
    app, _ = build(postgis_url, anthropic_api_key=ENV_KEY)
    async with app.router.lifespan_context(app), make_client(app) as client:
        before = (await client.get("/v1/admin/ai", headers=auth())).json()
        saved = (
            await client.put("/v1/admin/ai/key", json={"api_key": KEY_A}, headers=auth())
        ).json()
    assert before["key"]["source"] == "server_env" and before["key"]["last4"] == "xxxx"
    assert item(before, "api_key")["detail_en"] == (
        "From the server environment (ANTHROPIC_API_KEY), ending xxxx."
    )
    key = saved["key"]
    assert key["source"] == "server_env" and key["server_env_key"]
    assert key["console_key_stored"] and not key["console_key_active"]
    assert key["console_note_en"] == (
        "Inactive while the server environment sets a key (ANTHROPIC_API_KEY)."
    )
    (audit,) = await query(
        app,
        "SELECT after, details FROM audit_log WHERE action = 'ai.key_set' "
        "ORDER BY id DESC LIMIT 1",
    )
    assert audit["after"]["active"] is False and audit["details"]["server_env_key"] is True


async def test_a_key_saved_under_another_encryption_key_is_unreadable(postgis_url):
    app, _ = build(postgis_url)
    async with app.router.lifespan_context(app), make_client(app) as client:
        await client.put("/v1/admin/ai/key", json={"api_key": KEY_A}, headers=auth())
    other, _ = build(postgis_url, secrets_encryption_key=Fernet.generate_key().decode())
    async with other.router.lifespan_context(other), make_client(other) as client:
        status = (await client.get("/v1/admin/ai", headers=auth())).json()
    assert status["key"]["console_key_stored"] and status["key"]["console_key_readable"] is False
    assert (
        not status["key"]["configured"] and status["key"]["console_note_en"] == UNREADABLE_MESSAGE
    )
    assert item(status, "api_key")["ok"] is False
    assert item(status, "api_key")["detail_en"].endswith("paste it again")


async def test_removing_the_key(postgis_url):
    app, _ = build(postgis_url)
    async with app.router.lifespan_context(app), make_client(app) as client:
        await client.put("/v1/admin/ai/key", json={"api_key": KEY_A}, headers=auth())
        removed = await client.delete("/v1/admin/ai/key", headers=auth())
        again = await client.delete("/v1/admin/ai/key", headers=auth())
    assert removed.status_code == 200 and not removed.json()["key"]["console_key_stored"]
    assert await query(app, "SELECT id FROM app_secrets") == []
    (audit,) = await query(
        app, "SELECT before, after FROM audit_log WHERE action = 'ai.key_removed'"
    )
    assert audit["before"] == {"configured": True, "last4": "AAA1"}
    assert audit["after"] == {"configured": False}
    assert again.status_code == 404
    assert again.json()["error"]["details"] == {"secret": "anthropic_api_key"}


async def test_reviewer_accounts_and_the_staff_session_gate(postgis_url):
    app, _ = build(postgis_url)
    async with app.router.lifespan_context(app), make_client(app) as client:
        factory = app.state.session_factory
        before = (await client.get("/v1/admin/ai", headers=auth())).json()
        await create_user(factory, municipality_id=M, email="rev@example.com", role="reviewer")
        await create_user(
            factory, municipality_id=M, email="gone@example.com", role="admin", is_active=False
        )
        token = await issue_session(factory, municipality_id=M, email="rev@example.com")
        after = (await client.get("/v1/admin/ai", headers=auth())).json()
        denied = await client.get("/v1/admin/ai", headers=auth(token))
        denied_put = await client.put(
            "/v1/admin/ai/key", json={"api_key": KEY_A}, headers=auth(token)
        )
    assert item(before, "reviewer_account")["ok"] is False
    assert item(after, "reviewer_account")["ok"] is True
    assert item(after, "reviewer_account")["detail_en"] == "1 active admin / reviewer account(s)."
    assert denied.status_code == 403 and denied_put.status_code == 403
    assert await query(app, "SELECT id FROM app_secrets") == []


async def test_usage_sums_the_ai_jobs(postgis_url):
    app, _ = build(postgis_url)
    async with app.router.lifespan_context(app), make_client(app) as client:
        async with app.state.session_factory() as session:
            for status, tokens_in, tokens_out, cost in (
                ("succeeded", 100_000, 8_000, 0.42),
                ("succeeded", 20_000, 1_000, 0.075),
                ("failed", None, None, None),
            ):
                await session.execute(
                    text(
                        "INSERT INTO pipeline_jobs (municipality_id, kind, type, queue, status, "
                        "target_type, max_attempts, requested_by, requested_at, finished_at, "
                        "llm_tokens_in, llm_tokens_out, estimated_cost_eur) VALUES (:m, "
                        "'extract', 'extract_document', 'extraction', :status, 'document', 3, "
                        "'test', clock_timestamp(), clock_timestamp(), :tin, :tout, :cost)"
                    ),
                    {"m": M, "status": status, "tin": tokens_in, "tout": tokens_out, "cost": cost},
                )
            await session.commit()
        status = (await client.get("/v1/admin/ai", headers=auth())).json()
    usage = status["usage"]
    extract = usage["rows"][0]
    assert (extract["type"], extract["jobs"], extract["succeeded"], extract["failed"]) == (
        "extract_document",
        3,
        2,
        1,
    )
    assert (extract["tokens_in"], extract["tokens_out"]) == (120_000, 9_000)
    assert extract["estimated_cost_eur"] == 0.495
    assert usage["total"]["jobs"] == 3 and usage["total"]["estimated_cost_eur"] == 0.495
    assert usage["rows"][1]["jobs"] == 0 and usage["rows"][2]["jobs"] == 0
    assert usage["since"] is not None
    assert usage["last_extraction"]["status"] == "failed"


# --- the workers use the resolved key ---------------------------------------------------------


async def _store_console_key(postgis_url, value: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    settings = settings_for(postgis_url)
    engine = create_async_engine(postgis_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as session:
            await store_secret(
                session,
                municipality_id=M,
                name="anthropic_api_key",
                ciphertext=encrypt(settings, value),
                last4=value[-4:],
                set_by="test",
                set_by_user_id=None,
            )
            await session.commit()
    finally:
        await engine.dispose()


@pytest.fixture
def eager(postgis_url, monkeypatch):
    from jobs.celery_app import celery_app

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    configure_job_store(SqlJobStore(database_url=postgis_url))
    yield
    configure_job_store(None)
    configure_ai_check()


@pytest.mark.parametrize("with_key", [True, False])
async def test_the_extraction_job_uses_the_console_key(postgis_url, monkeypatch, eager, with_key):
    pytest.importorskip("pymupdf")
    import core.extraction.evaluate as evaluate
    from jobs.enqueue import CeleryDispatcher
    from jobs.tasks.extraction import configure_extraction, configure_preprocess
    from tests.extraction_script import Transcriber
    from tests.integration.test_extraction_job_postgis import Storage, _no_sleep, register

    captured: list[str | None] = []

    def fake_model(settings=None, *, api_key=None):
        captured.append(api_key)
        return Transcriber()

    monkeypatch.setattr(evaluate, "model_from_settings", fake_model)
    if with_key:
        await _store_console_key(postgis_url, KEY_A)
    settings = settings_for(
        postgis_url, preprocess_page_image_dpi=40, extraction_model=Transcriber().name
    )
    storage = Storage()
    configure_preprocess(database_url=postgis_url, storage=storage, settings=settings)
    configure_extraction(
        database_url=postgis_url, storage=storage, settings=settings, sleep=_no_sleep
    )
    try:
        from tests.pdf_synthetic import planning_pdf

        app = make_app(settings, storage=storage, admin_dispatcher=CeleryDispatcher())
        async with app.router.lifespan_context(app), make_client(app) as client:
            document_id = await register(client, planning_pdf())
            response = await client.post(
                f"/v1/admin/documents/{document_id}/jobs/extract", headers=auth()
            )
            assert response.status_code == 202, response.text
            job = (await client.get(response.json()["status_url"], headers=auth())).json()
    finally:
        configure_preprocess(database_url=None, storage=None, settings=None)
        configure_extraction(database_url=None, storage=None, settings=None, sleep=None)
    if with_key:
        assert job["status"] == "succeeded", job
        assert captured == [KEY_A]
    else:
        assert job["status"] == "failed" and job["attempts"] == 1
        assert job["error"].startswith("ModelNotConfigured: No Anthropic API key")
        assert captured == []


@pytest.mark.parametrize("with_key", [True, False])
async def test_the_market_job_uses_the_console_key(postgis_url, monkeypatch, eager, with_key):
    import core.market.pipeline as pipeline
    from jobs.enqueue import CeleryDispatcher
    from jobs.tasks.market import configure_market

    captured: list[str | None] = []

    def fake_model(settings, *, api_key=None):
        captured.append(api_key)
        return None  # the rules alone

    monkeypatch.setattr(pipeline, "model_from_settings", fake_model)
    if with_key:
        await _store_console_key(postgis_url, KEY_B)
    settings = settings_for(postgis_url, market_normalise_llm="auto", market_min_listings=3)
    configure_market(database_url=postgis_url, settings=settings, model_factory=None)
    try:
        app = make_app(settings, admin_dispatcher=CeleryDispatcher())
        listings = "\n".join(
            [
                "Centar; 2.400; 02.09.2026",
                "Centar grada; 2.100; 05.09.2026",
                "Centar, Njegoševa; 2.650; 06.09.2026",
            ]
        )
        async with app.router.lifespan_context(app), make_client(app) as client:
            response = await client.post(
                "/v1/admin/market/listings",
                json={"source": "Realitica", "retrieved_on": "2026-09-24", "listings": listings},
                headers=auth(),
            )
            assert response.status_code == 202, response.text
            job = (await client.get(response.json()["job"]["status_url"], headers=auth())).json()
    finally:
        configure_market(database_url=None, settings=None)
    assert job["status"] == "succeeded", job
    assert captured == ([KEY_B] if with_key else [])


# --- the connection test end to end ---------------------------------------------------------------


async def test_the_connection_test_end_to_end(postgis_url, eager):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    from jobs.enqueue import CeleryDispatcher

    seen: list[dict[str, Any]] = []

    def handler(request: Any) -> Any:
        seen.append({"key": request.headers.get("x-api-key"), "body": json.loads(request.content)})
        return httpx2.Response(
            200,
            headers={"request-id": "req_e2e"},
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "OK"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 14, "output_tokens": 4},
            },
        )

    def client_factory(*, api_key, base_url, timeout_seconds, max_retries):
        return anthropic.Anthropic(
            api_key=api_key,
            base_url="https://api.example.invalid",
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
            max_retries=max_retries,
            timeout=timeout_seconds,
        )

    settings = settings_for(postgis_url)
    configure_ai_check(database_url=postgis_url, settings=settings, client_factory=client_factory)
    app = make_app(
        settings, admin_dispatcher=CeleryDispatcher(), worker_probe=FakeWorkerProbe("eager")
    )
    async with app.router.lifespan_context(app), make_client(app) as client:
        saved = await client.put("/v1/admin/ai/key", json={"api_key": KEY_A}, headers=auth())
        assert saved.status_code == 200, saved.text
        status = (await client.get("/v1/admin/ai", headers=auth())).json()
    assert [s["key"] for s in seen] == [KEY_A]
    assert set(seen[0]["body"]) == {"model", "max_tokens", "messages"}
    check = status["check"]
    assert check["job"]["status"] == "succeeded" and check["status"] == "ok"
    assert check["key_source"] == "console" and check["key_last4"] == "AAA1"
    assert check["matches_current_key"] and check["estimated_cost_eur"] is not None
    assert (check["input_tokens"], check["output_tokens"]) == (14, 4)
    assert item(status, "key_verified")["ok"] is True
    assert item(status, "worker")["ok"] is True
    assert status["usage"]["rows"][2]["jobs"] == 1
    assert KEY_A not in json.dumps(status)
    (job,) = await query(app, "SELECT result, payload, error FROM pipeline_jobs")
    assert KEY_A not in json.dumps(job, default=str)
