"""The connection test's Messages API call and its classification (core.extraction.connection),
through the real SDK on a mock transport: no network, no key leaves the test."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.extraction.connection import CHECK_MAX_TOKENS, CHECK_PROMPT, check_connection
from core.extraction.credentials import NO_KEY_MESSAGE
from core.extraction.llm import anthropic_client

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")

FAKE_KEY = "sk-ant-api03-" + "x" * 40
MODEL = "claude-sonnet-5"
HOST = "api.example.invalid"


def _message(model: str = MODEL, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "OK"}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 14, "output_tokens": 4},
    }


def _error(status: int, kind: str, message: str) -> Any:
    return httpx2.Response(
        status,
        headers={"request-id": "req_err_1"},
        json={"type": "error", "error": {"type": kind, "message": message}},
    )


class Transport:
    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.calls.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _client(transport: Transport, *, api_key: str | None = FAKE_KEY) -> Any:
    return anthropic.Anthropic(
        api_key=api_key,
        base_url=f"https://{HOST}",
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(transport)),
        max_retries=0,
        timeout=5,
    )


def _check(reply: Any) -> tuple[Any, Transport]:
    transport = Transport(reply)
    result = check_connection(_client(transport), model=MODEL, base_url_host=HOST)
    return result, transport


def test_a_200_is_ok_with_the_model_tokens_and_latency():
    ticks = iter([10.0, 10.8125])
    transport = Transport(
        httpx2.Response(200, headers={"request-id": "req_ok_1"}, json=_message("claude-sonnet-5"))
    )
    result = check_connection(
        _client(transport), model=MODEL, base_url_host=HOST, clock=lambda: next(ticks)
    )
    assert result.status == "ok" and result.model_answered == "claude-sonnet-5"
    assert (result.input_tokens, result.output_tokens) == (14, 4)
    assert result.latency_ms == 812 and result.request_id == "req_ok_1"
    assert result.stop_reason == "end_turn" and result.model_requested == MODEL
    assert len(transport.calls) == 1


def test_the_request_is_minimal_and_carries_the_key():
    result, transport = _check(httpx2.Response(200, json=_message(stop_reason="max_tokens")))
    assert result.status == "ok"  # a truncated answer is still a working connection
    (request,) = transport.calls
    body = json.loads(request.content)
    assert set(body) == {"model", "max_tokens", "messages"}
    assert body["model"] == MODEL and body["max_tokens"] == CHECK_MAX_TOKENS == 16
    assert body["messages"] == [{"role": "user", "content": CHECK_PROMPT}]
    assert request.headers.get("x-api-key") == FAKE_KEY
    assert request.headers.get("anthropic-beta") is None
    assert str(request.url) == f"https://{HOST}/v1/messages"


def test_the_factory_builds_a_client_without_retries():
    client = anthropic_client(
        api_key=FAKE_KEY, base_url=f"https://{HOST}", timeout_seconds=30, max_retries=0
    )
    assert client.max_retries == 0 and client.api_key == FAKE_KEY


@pytest.mark.parametrize(
    ("status", "kind", "message", "expected"),
    [
        (401, "authentication_error", "invalid x-api-key", "invalid_key"),
        (403, "permission_error", "Your key may not use this model", "permission_denied"),
        (404, "not_found_error", "model: claude-sonnet-5", "model_unavailable"),
        (429, "rate_limit_error", "Number of requests has exceeded", "rate_limited"),
        (529, "overloaded_error", "Overloaded", "overloaded"),
        (503, "api_error", "Service unavailable", "overloaded"),
        (500, "api_error", "Internal server error", "error"),
        (400, "billing_error", "Your account is out of credit", "no_credit"),
        (
            400,
            "invalid_request_error",
            "Your credit balance is too low to access the Anthropic API.",
            "no_credit",
        ),
        (400, "invalid_request_error", "max_tokens: must be positive", "error"),
    ],
)
def test_api_errors_map_to_a_status(status, kind, message, expected):
    result, transport = _check(_error(status, kind, message))
    assert result.status == expected
    assert result.http_status == status and result.request_id == "req_err_1"
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert len(transport.calls) == 1  # max_retries=0: one call per check
    assert FAKE_KEY not in result.detail_en


def test_details_are_precise():
    result, _ = _check(_error(401, "authentication_error", "invalid x-api-key"))
    assert result.detail_en == (
        "The API refused the key (HTTP 401): it is wrong or revoked. Create a new key in the "
        "Anthropic Console."
    )
    result, _ = _check(_error(403, "permission_error", "Your key may not use this model"))
    assert result.detail_en == (
        "The key may not use this API or model (HTTP 403): Your key may not use this model"
    )
    result, _ = _check(_error(404, "not_found_error", "model"))
    assert result.detail_en == (
        f"The API does not offer {MODEL} to this key (HTTP 404): check EXTRACTION_MODEL."
    )
    result, _ = _check(_error(529, "overloaded_error", "Overloaded"))
    assert result.detail_en == "The API is overloaded (HTTP 529): try again shortly."
    result, _ = _check(_error(400, "billing_error", "no credit"))
    assert result.detail_en.startswith("The Anthropic account has no credit left")
    result, _ = _check(_error(500, "api_error", "boom " * 200))
    assert result.detail_en.startswith("HTTP 500: boom") and len(result.detail_en) <= 310


def test_network_failures():
    result, _ = _check(httpx2.ConnectError("refused"))
    assert result.status == "network_error"
    assert result.detail_en == f"The worker could not reach {HOST}: APIConnectionError."
    result, _ = _check(httpx2.ReadTimeout("slow"))
    assert result.status == "network_error"
    assert result.detail_en == f"The worker could not reach {HOST}: APITimeoutError."


def test_no_key_at_all(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    transport = Transport(httpx2.Response(200, json=_message()))
    try:
        client = _client(transport, api_key=None)
    except Exception as exc:  # noqa: BLE001 - an SDK that refuses at construction
        pytest.skip(f"the SDK refuses a keyless client: {exc}")
    result = check_connection(client, model=MODEL, base_url_host=HOST)
    if result.status == "ok":
        pytest.skip("the SDK found credentials elsewhere on this machine")
    assert result.status == "no_key" and result.detail_en == NO_KEY_MESSAGE
    assert transport.calls == []


def test_the_key_never_shows_in_the_result():
    for reply in (
        httpx2.Response(200, json=_message()),
        _error(401, "authentication_error", f"invalid x-api-key {FAKE_KEY[:12]}"),
        httpx2.ConnectError("refused"),
    ):
        result, _ = _check(reply)
        assert FAKE_KEY not in repr(result)
