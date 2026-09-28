"""The model seam of AI extraction: one structured call per prompt.

:class:`StructuredModel` takes the system blocks, the user message and the response schema and
returns the parsed JSON with the model name and token usage. :class:`ClaudeModel` implements it
with the Anthropic SDK (the ``ai`` extra, imported lazily): structured outputs
(``output_config.format``, the prompt set's JSON Schema), the system blocks cached, adaptive
thinking, streaming (long tables produce long outputs) and, when switched on, server-side
refusal fallback (documented for Opus 5 / Fable 5.1). The default model is Claude Sonnet 5:
it reads the tables well, supports adaptive thinking and effort, and caches the system prompt
(Haiku 4.5 has neither thinking mode nor effort, and caches only prompts of 4096+ tokens).
:class:`ScriptedModel` replays canned responses (tests, dry runs of the evaluation).

Errors are classified for the job layer: :class:`ModelUnavailable` (timeouts, connection
errors, 5xx / 529 overloaded) and :class:`ModelRateLimited` (429) are worth retrying with
backoff; :class:`ModelRefused`, :class:`ModelOutputInvalid` (truncated or not the schema) and
:class:`ModelError` (bad request, authentication) are not.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.extraction.prompts import SystemBlock

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ModelError(Exception):
    """The call cannot succeed as sent (bad request, authentication, permission)."""


class ModelRefused(ModelError):
    pass


class ModelOutputInvalid(ModelError):
    """The response is truncated or does not parse against the response model. ``usage`` and
    ``model`` say what the rejected answer cost, when known."""

    def __init__(
        self, message: str, *, usage: ModelUsage | None = None, model: str | None = None
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.model = model


class ModelUnavailable(Exception):
    """Transient: timeout, connection error, server error or overload. Retry with backoff."""


class ModelRateLimited(ModelUnavailable):
    pass


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def tokens_in(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: ModelUsage) -> ModelUsage:
        return ModelUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True, slots=True)
class ModelReply:
    data: dict[str, Any]
    model: str
    usage: ModelUsage = field(default_factory=ModelUsage)
    request_id: str | None = None


class StructuredModel(Protocol):
    name: str

    def complete(
        self, *, system: Sequence[SystemBlock], user: str, schema: dict[str, Any]
    ) -> ModelReply: ...


class ClaudeModel:
    """Claude through the Anthropic SDK. ``client`` is injectable (tests use a mock transport)."""

    def __init__(
        self,
        model: str = "claude-sonnet-5",
        *,
        api_key: str | None = None,
        effort: str | None = "high",
        adaptive_thinking: bool = True,
        max_tokens: int = 32_000,
        refusal_fallback: bool = False,
        timeout_seconds: float = 600,
        base_url: str | None = None,
        client: Any = None,
    ) -> None:
        self.name = model
        self.effort = effort
        self.adaptive_thinking = adaptive_thinking
        self.max_tokens = max_tokens
        self.refusal_fallback = refusal_fallback
        if client is None:
            import anthropic

            client = anthropic.Anthropic(
                api_key=api_key, base_url=base_url, timeout=timeout_seconds, max_retries=2
            )
        self._client = client

    def request(
        self, system: Sequence[SystemBlock], user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        params: dict[str, Any] = {
            "model": self.name,
            "max_tokens": self.max_tokens,
            "system": [
                {"type": "text", "text": block.text}
                | ({"cache_control": {"type": "ephemeral"}} if block.cache else {})
                for block in system
            ],
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }
        if self.adaptive_thinking:
            params["thinking"] = {"type": "adaptive"}
        if self.refusal_fallback:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    def complete(
        self, *, system: Sequence[SystemBlock], user: str, schema: dict[str, Any]
    ) -> ModelReply:
        import anthropic

        params = self.request(system, user, schema)
        messages = self._client.beta.messages if self.refusal_fallback else self._client.messages
        try:
            with messages.stream(**params) as stream:
                message = stream.get_final_message()
        except anthropic.RateLimitError as exc:
            raise ModelRateLimited(f"rate limited: {exc.message}") from exc
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            raise ModelUnavailable(f"{type(exc).__name__}: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code in (408, 409) or exc.status_code >= 500:
                raise ModelUnavailable(f"HTTP {exc.status_code}: {exc.message}") from exc
            raise ModelError(f"HTTP {exc.status_code}: {exc.message}") from exc
        request_id = getattr(message, "_request_id", None)
        raw = message.usage
        usage = ModelUsage(
            input_tokens=raw.input_tokens or 0,
            output_tokens=raw.output_tokens or 0,
            cache_read_tokens=raw.cache_read_input_tokens or 0,
            cache_write_tokens=raw.cache_creation_input_tokens or 0,
        )
        if message.stop_reason == "refusal":
            raise ModelRefused(f"the model declined (request {request_id})")
        if message.stop_reason == "max_tokens":
            raise ModelOutputInvalid(
                f"output truncated at {self.max_tokens} tokens", usage=usage, model=message.model
            )
        text = "".join(block.text for block in message.content if block.type == "text")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelOutputInvalid(
                f"the response is not JSON: {exc}", usage=usage, model=message.model
            ) from exc
        return ModelReply(data=data, model=message.model, usage=usage, request_id=request_id)


Script = Callable[[Sequence[SystemBlock], str, dict[str, Any]], dict[str, Any]]


class ScriptedModel:
    """Replays responses in order, or computes them from the request (tests, dry runs)."""

    def __init__(self, responses: Iterable[dict[str, Any]] | Script, name: str = "scripted"):
        self.name = name
        self.calls: list[tuple[tuple[SystemBlock, ...], str, dict[str, Any]]] = []
        self._script = responses if callable(responses) else None
        self._queue = None if callable(responses) else list(responses)

    def complete(
        self, *, system: Sequence[SystemBlock], user: str, schema: dict[str, Any]
    ) -> ModelReply:
        self.calls.append((tuple(system), user, schema))
        if self._script is not None:
            data = self._script(system, user, schema)
        elif self._queue:
            data = self._queue.pop(0)
        else:
            raise ModelError("the script has no response left")
        return ModelReply(data=data, model=self.name)
