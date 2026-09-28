"""The admin console's connection test: one minimal Messages API call with the resolved key.

:func:`check_connection` sends the smallest request every model accepts (``model``,
``max_tokens``, one user message; no ``thinking``, ``output_config``, ``system`` or betas:
Haiku rejects ``effort``, Opus 5.5 and Fable reject ``thinking: disabled``, and Sonnet 5 may
think adaptively inside the 16 tokens) and classifies the answer as one ``AiCheckStatus``. Any
HTTP 200 is ``ok`` whatever the ``stop_reason``. API errors never raise: they become a status
with an English detail the console shows (and never contain the key: the job scrubs them too).

Synchronous; the SDK comes with the client the caller built (``core.extraction.llm.
anthropic_client``) and is imported lazily for the exception classes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.extraction.credentials import ResolvedKey, missing_key_message

CHECK_PROMPT = "Reply with the single word OK."
CHECK_MAX_TOKENS = 16
CHECK_TIMEOUT_SECONDS = 30
MESSAGE_LIMIT = 300

NO_CREDIT_DETAIL = (
    "The Anthropic account has no credit left: add credit in the Anthropic Console (Billing), "
    "then test again."
)


@dataclass(frozen=True, slots=True)
class ConnectionResult:
    status: str  # AiCheckStatus
    detail_en: str
    model_requested: str
    model_answered: str | None = None
    latency_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None
    http_status: int | None = None
    request_id: str | None = None


def _no_credit(exc: Any) -> bool:
    return getattr(exc, "type", None) == "billing_error"


def _message(exc: Any) -> str:
    """The API's own error message (``{"error": {"message"}}`` of the body) when it sent one,
    else the SDK's (``Error code: N - {body}``), at most ``MESSAGE_LIMIT`` characters."""
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    text = error.get("message") if isinstance(error, dict) else None
    return str(text or getattr(exc, "message", None) or exc)[:MESSAGE_LIMIT]


def _mentions_credit(exc: Any) -> bool:
    return any(
        "credit balance" in str(text).lower()
        for text in (_message(exc), getattr(exc, "message", ""))
    )


def _status_outcome(exc: Any, anthropic: Any, *, model: str) -> tuple[str, str]:
    """``(status, detail_en)`` of an ``APIStatusError``, most specific class first."""
    code = getattr(exc, "status_code", None)
    if isinstance(exc, anthropic.AuthenticationError):
        return (
            "invalid_key",
            "The API refused the key (HTTP 401): it is wrong or revoked. Create a new key in "
            "the Anthropic Console.",
        )
    if isinstance(exc, anthropic.PermissionDeniedError):
        return (
            "permission_denied",
            f"The key may not use this API or model (HTTP 403): {_message(exc)}",
        )
    if isinstance(exc, anthropic.NotFoundError):
        return (
            "model_unavailable",
            f"The API does not offer {model} to this key (HTTP 404): check EXTRACTION_MODEL.",
        )
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limited", "Rate limited (HTTP 429): try again in a minute."
    # the SDK raises InternalServerError for a 503 (ServiceUnavailableError is never built)
    overloaded = (anthropic.OverloadedError, anthropic.ServiceUnavailableError)
    if isinstance(exc, overloaded) or code in (503, 529):
        return "overloaded", f"The API is overloaded (HTTP {code}): try again shortly."
    if isinstance(exc, anthropic.BadRequestError) and (_no_credit(exc) or _mentions_credit(exc)):
        return "no_credit", NO_CREDIT_DETAIL
    if _no_credit(exc):
        return "no_credit", NO_CREDIT_DETAIL
    return "error", f"HTTP {code}: {_message(exc)}"


def check_connection(
    client: Any,
    *,
    model: str,
    base_url_host: str,
    max_tokens: int = CHECK_MAX_TOKENS,
    clock: Callable[[], float] = time.perf_counter,
) -> ConnectionResult:
    """One Messages API call; the outcome as a :class:`ConnectionResult` (never raises for API
    errors, only for programming errors outside the SDK's classes)."""
    import anthropic

    started = clock()

    def elapsed() -> int:
        return max(0, round((clock() - started) * 1000))

    try:
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": CHECK_PROMPT}],
        )
    except anthropic.APIStatusError as exc:
        status, detail = _status_outcome(exc, anthropic, model=model)
        return ConnectionResult(
            status=status,
            detail_en=detail,
            model_requested=model,
            latency_ms=elapsed(),
            http_status=getattr(exc, "status_code", None),
            request_id=getattr(exc, "request_id", None),
        )
    except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
        return ConnectionResult(
            status="network_error",
            detail_en=f"The worker could not reach {base_url_host}: {type(exc).__name__}.",
            model_requested=model,
            latency_ms=elapsed(),
        )
    except anthropic.CredentialsError:
        return _no_key(model, elapsed())
    except TypeError as exc:
        if "authentication method" not in str(exc):
            raise
        return _no_key(model, elapsed())
    latency = elapsed()
    usage = getattr(message, "usage", None)
    return ConnectionResult(
        status="ok",
        detail_en=f"{message.model} answered in {latency} ms.",
        model_requested=model,
        model_answered=message.model,
        latency_ms=latency,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        stop_reason=getattr(message, "stop_reason", None),
        request_id=getattr(message, "_request_id", None),
    )


def _no_key(model: str, latency_ms: int) -> ConnectionResult:
    return ConnectionResult(
        status="no_key",
        detail_en=missing_key_message(ResolvedKey("none")),
        model_requested=model,
        latency_ms=latency_ms,
    )
