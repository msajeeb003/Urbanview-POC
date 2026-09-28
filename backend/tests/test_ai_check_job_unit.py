"""The ``ai_check`` job (jobs.tasks.ai) through eager Celery on the in-memory job store: the
lifecycle succeeds whenever a check ran, the result names the key without revealing it."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from core.extraction.credentials import NO_KEY_MESSAGE, UNREADABLE_MESSAGE, ResolvedKey
from jobs.base import MemoryJobStore, configure_job_store
from jobs.tasks.ai import NO_SDK_DETAIL, ai_check, configure_ai_check
from tests.helpers import make_settings

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")

FAKE_KEY = "sk-ant-api03-" + "x" * 36 + "k1y1"
SET_AT = datetime(2026, 10, 10, 9, 14, tzinfo=UTC)


class FakeMessages:
    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeFactory:
    def __init__(self, reply: Any = None, error: Exception | None = None) -> None:
        self.messages = FakeMessages(reply)
        self.error = error
        self.built: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.built.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(messages=self.messages)


def _reply() -> Any:
    return SimpleNamespace(
        model="claude-sonnet-5-20260901",
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=14, output_tokens=4),
        _request_id="req_1",
    )


def _resolver(resolved: ResolvedKey):
    seen: list[Any] = []

    async def resolve(job, settings):
        seen.append(job.id)
        return resolved

    resolve.seen = seen  # type: ignore[attr-defined]
    return resolve


@pytest.fixture
def store(monkeypatch):
    from jobs.celery_app import celery_app

    store = MemoryJobStore()
    configure_job_store(store)
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    yield store
    configure_job_store(None)
    configure_ai_check()


def _run(store: MemoryJobStore, *, trigger: str = "manual") -> dict[str, Any]:
    job_id = store.add(
        type="ai_check", payload={"trigger": trigger}, max_attempts=1, target_type="ai_settings"
    )
    ai_check.apply_async(args=[job_id, "podgorica"], queue="extraction").get()
    return store.jobs[job_id]


def test_no_key_succeeds_with_the_reason_and_no_cost(store):
    factory = FakeFactory(_reply())
    configure_ai_check(
        settings=make_settings(anthropic_api_key=None),
        resolver=_resolver(ResolvedKey("none")),
        client_factory=factory,
    )
    row = _run(store)
    assert row["status"] == "succeeded" and row["cost"] is None
    result = row["result"]
    assert result["status"] == "no_key" and result["detail_en"] == NO_KEY_MESSAGE
    assert (result["key_source"], result["key_last4"], result["key_set_at"]) == ("none", None, None)
    assert result["trigger"] == "manual" and factory.built == []

    configure_ai_check(
        resolver=_resolver(ResolvedKey("console", "k1y1", SET_AT, problem="unreadable"))
    )
    row = _run(store)
    assert row["result"]["status"] == "no_key"
    assert row["result"]["detail_en"] == UNREADABLE_MESSAGE
    assert row["result"]["key_set_at"] == SET_AT.isoformat()


def test_ok_records_the_cost_and_the_fingerprint(store):
    factory = FakeFactory(_reply())
    configure_ai_check(
        settings=make_settings(
            anthropic_api_key=None, anthropic_base_url="https://api.anthropic.com"
        ),
        resolver=_resolver(ResolvedKey("console", "k1y1", SET_AT, api_key=FAKE_KEY)),
        client_factory=factory,
    )
    row = _run(store, trigger="key_saved")
    assert row["status"] == "succeeded"
    result = row["result"]
    assert result["status"] == "ok" and result["trigger"] == "key_saved"
    assert (result["key_source"], result["key_last4"]) == ("console", "k1y1")
    assert result["key_set_at"] == SET_AT.isoformat()
    assert result["model_requested"] == "claude-sonnet-5"
    assert result["model_answered"] == "claude-sonnet-5-20260901"
    assert (result["input_tokens"], result["output_tokens"]) == (14, 4)
    assert result["base_url_host"] == "api.anthropic.com" and result["request_id"] == "req_1"
    assert result["checked_at"] and result["latency_ms"] >= 0
    assert row["cost"]["tokens_in"] == 14 and row["cost"]["tokens_out"] == 4
    assert row["cost"]["llm_model"] == "claude-sonnet-5-20260901"
    assert row["cost"]["estimated_cost_eur"] is not None
    (built,) = factory.built
    assert built["api_key"] == FAKE_KEY and built["max_retries"] == 0
    assert built["timeout_seconds"] == 30
    (call,) = factory.messages.calls
    assert set(call) == {"model", "max_tokens", "messages"}
    assert FAKE_KEY not in repr(row["result"]) and FAKE_KEY not in repr(row["payload"])


def test_every_string_of_the_result_is_scrubbed(store):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(
        403,
        request=request,
        json={"type": "error", "error": {"type": "permission_error", "message": f"no {FAKE_KEY}"}},
    )
    error = anthropic.PermissionDeniedError(
        f"Error code: 403 - {FAKE_KEY}", response=response, body=response.json()
    )
    configure_ai_check(
        settings=make_settings(anthropic_api_key=None),
        resolver=_resolver(ResolvedKey("server_env", "k1y1", api_key=FAKE_KEY)),
        client_factory=FakeFactory(error),
    )
    row = _run(store)
    assert row["status"] == "succeeded" and row["cost"] is None
    assert row["result"]["status"] == "permission_denied"
    assert FAKE_KEY not in repr(row["result"])
    assert "sk-ant-…k1y1" in row["result"]["detail_en"]


def test_a_worker_without_the_sdk_reports_error(store):
    configure_ai_check(
        settings=make_settings(anthropic_api_key=None),
        resolver=_resolver(ResolvedKey("server_env", "k1y1", api_key=FAKE_KEY)),
        client_factory=FakeFactory(error=ImportError("No module named 'anthropic'")),
    )
    row = _run(store)
    assert row["status"] == "succeeded"
    assert row["result"]["status"] == "error" and row["result"]["detail_en"] == NO_SDK_DETAIL


def test_an_internal_error_fails_the_job_once(store):
    async def broken(job, settings):
        raise RuntimeError("database gone")

    configure_ai_check(settings=make_settings(anthropic_api_key=None), resolver=broken)
    row = _run(store)
    assert row["status"] == "failed" and row["attempts"] == 1
    assert row["error"] == "RuntimeError: database gone"
