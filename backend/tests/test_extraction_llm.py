"""The model seam of AI extraction (core.extraction.llm): the Claude request (structured output,
cached system blocks, adaptive thinking, refusal fallback), reading the reply, and classifying
failures into retryable and final ones. No network: fake clients and a mock HTTP transport."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.extraction.llm import (
    FALLBACK_BETA,
    ClaudeModel,
    ModelError,
    ModelOutputInvalid,
    ModelRateLimited,
    ModelRefused,
    ModelUnavailable,
    ScriptedModel,
)
from core.extraction.prompts import SystemBlock

SYSTEM = (SystemBlock("rules and glossary", cache=True), SystemBlock("task", cache=True))
SCHEMA = {
    "type": "object",
    "properties": {"entries": {"type": "array", "items": {"type": "string"}}},
    "required": ["entries"],
    "additionalProperties": False,
}


class _Stream:
    def __init__(self, message: Any = None, error: Exception | None = None):
        self.message, self.error = message, error

    def __enter__(self) -> _Stream:
        if self.error is not None:
            raise self.error
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get_final_message(self) -> Any:
        return self.message


class _Messages:
    def __init__(self, stream: _Stream):
        self._stream, self.params = stream, None

    def stream(self, **params: Any) -> _Stream:
        self.params = params
        return self._stream


def _fake(message: Any = None, error: Exception | None = None) -> tuple[Any, _Messages, _Messages]:
    beta, plain = _Messages(_Stream(message, error)), _Messages(_Stream(message, error))
    return SimpleNamespace(beta=SimpleNamespace(messages=beta), messages=plain), beta, plain


def _message(text: str = '{"entries": []}', stop: str = "end_turn") -> Any:
    return SimpleNamespace(
        stop_reason=stop,
        model="claude-sonnet-5",
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(
            input_tokens=120,
            output_tokens=40,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=0,
        ),
    )


def test_the_request_asks_for_the_schema_with_cached_rules():
    params = ClaudeModel(client=object()).request(SYSTEM, "the pages", SCHEMA)
    assert params["model"] == "claude-sonnet-5"  # the default: see core.extraction.llm
    assert params["output_config"] == {
        "format": {"type": "json_schema", "schema": SCHEMA},
        "effort": "high",
    }
    assert params["thinking"] == {"type": "adaptive"}
    assert [block["cache_control"] for block in params["system"]] == [{"type": "ephemeral"}] * 2
    assert params["messages"] == [{"role": "user", "content": "the pages"}]
    assert "betas" not in params and "fallbacks" not in params  # off unless switched on
    opus = ClaudeModel("claude-opus-5", client=object(), refusal_fallback=True)
    with_fallback = opus.request(SYSTEM, "the pages", SCHEMA)
    assert with_fallback["betas"] == [FALLBACK_BETA] and with_fallback["fallbacks"] == "default"
    plain = ClaudeModel(
        "claude-haiku-4-5",
        client=object(),
        effort=None,
        adaptive_thinking=False,
        refusal_fallback=False,
    ).request((SystemBlock("rules"),), "p", SCHEMA)
    assert plain["model"] == "claude-haiku-4-5" and "cache_control" not in plain["system"][0]
    assert "thinking" not in plain and "betas" not in plain and "fallbacks" not in plain
    assert plain["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}


def test_a_reply_is_read_from_its_text_block_with_usage():
    client, beta, plain = _fake(_message('{"entries": ["SS"]}'))
    reply = ClaudeModel(client=client).complete(system=SYSTEM, user="p", schema=SCHEMA)
    assert reply.data == {"entries": ["SS"]} and reply.model == "claude-sonnet-5"
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (120, 40)
    assert reply.usage.cache_read_tokens == 900 and reply.usage.tokens_in == 1020
    assert plain.params is not None and beta.params is None
    client, beta, plain = _fake(_message())
    ClaudeModel(client=client, refusal_fallback=True).complete(
        system=SYSTEM, user="p", schema=SCHEMA
    )
    assert beta.params is not None and plain.params is None  # the fallback needs the beta API


@pytest.mark.parametrize(
    ("stop", "text", "error"),
    [
        ("refusal", "", ModelRefused),
        ("max_tokens", '{"entries": [', ModelOutputInvalid),
        ("end_turn", "not json", ModelOutputInvalid),
    ],
)
def test_refusals_and_broken_output_are_final(stop, text, error):
    client, _, _ = _fake(_message(text, stop))
    with pytest.raises(error):
        ClaudeModel(client=client).complete(system=SYSTEM, user="p", schema=SCHEMA)
    assert not issubclass(error, ModelUnavailable)  # not retried


def test_api_errors_are_classified_for_retries():
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    request = httpx2.Request("POST", "https://api.example.invalid/v1/messages")

    def status(cls: Any, code: int) -> Exception:
        return cls("boom", response=httpx2.Response(code, request=request), body=None)

    expected = [
        (status(anthropic.RateLimitError, 429), ModelRateLimited),
        (status(anthropic.InternalServerError, 500), ModelUnavailable),
        (status(anthropic.OverloadedError, 529), ModelUnavailable),
        (anthropic.APITimeoutError(request=request), ModelUnavailable),
        (anthropic.APIConnectionError(request=request), ModelUnavailable),
        (status(anthropic.BadRequestError, 400), ModelError),
        (status(anthropic.AuthenticationError, 401), ModelError),
    ]
    for raised, mapped in expected:
        client, _, _ = _fake(error=raised)
        with pytest.raises(mapped) as caught:
            ClaudeModel(client=client).complete(system=SYSTEM, user="p", schema=SCHEMA)
        retryable = isinstance(caught.value, ModelUnavailable)
        assert retryable == (mapped in (ModelRateLimited, ModelUnavailable)), raised


def _sse(text: str) -> bytes:
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 50,
                    },
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 12},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def test_the_wire_request_through_the_real_sdk():
    """The Anthropic SDK sends what the contract asks for (checked at the HTTP layer)."""
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    seen: dict[str, Any] = {}

    def handler(request: Any) -> Any:
        seen["url"] = str(request.url)
        seen["beta"] = request.headers.get("anthropic-beta")
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=_sse('{"entries": ["a"]}')
        )

    client = anthropic.Anthropic(
        api_key="test-key",
        base_url="https://api.example.invalid",
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    reply = ClaudeModel(client=client).complete(system=SYSTEM, user="the pages", schema=SCHEMA)
    assert reply.data == {"entries": ["a"]} and reply.model == "claude-sonnet-5"
    assert (reply.usage.input_tokens, reply.usage.cache_read_tokens) == (100, 50)
    assert seen["url"] == "https://api.example.invalid/v1/messages"
    assert seen["beta"] is None and "fallbacks" not in seen["body"]
    assert seen["body"]["model"] == "claude-sonnet-5"
    ClaudeModel("claude-opus-5", client=client, refusal_fallback=True).complete(
        system=SYSTEM, user="the pages", schema=SCHEMA
    )
    assert seen["url"].startswith("https://api.example.invalid/v1/messages")
    assert seen["beta"] == FALLBACK_BETA
    body = seen["body"]
    assert body["stream"] is True and body["fallbacks"] == "default"
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert body["thinking"] == {"type": "adaptive"}
    assert body["system"][0] == {
        "type": "text",
        "text": "rules and glossary",
        "cache_control": {"type": "ephemeral"},
    }
    assert body["messages"] == [{"role": "user", "content": "the pages"}]


def test_the_scripted_model_replays_and_records():
    model = ScriptedModel([{"entries": []}])
    assert model.complete(system=SYSTEM, user="p", schema=SCHEMA).data == {"entries": []}
    assert model.calls == [(SYSTEM, "p", SCHEMA)]
    with pytest.raises(ModelError):
        model.complete(system=SYSTEM, user="p", schema=SCHEMA)
