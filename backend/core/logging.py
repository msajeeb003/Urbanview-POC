"""Logging: one stdout handler, request id on every record, JSON outside dev.

Pass structured fields with ``extra={...}``; they are emitted as top-level JSON keys (or appended
as ``key=value`` in dev). ``request_id`` is filled from the request context unless given explicitly.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import TYPE_CHECKING, Any

from core.context import get_request_id

if TYPE_CHECKING:
    from core.config import Settings

_HANDLER_NAME = "urbanview"
_STANDARD_ATTRS = set(logging.LogRecord("x", logging.INFO, "x", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "request_id",
}


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id() or "-"
        return True


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        k: v
        for k, v in record.__dict__.items()
        if k not in _STANDARD_ATTRS and not k.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class DevFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = _extras(record)
        if extras:
            line += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        return line


def configure_logging(settings: Settings) -> None:
    """Idempotent: safe to call once per app factory invocation."""
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    for handler in list(root.handlers):
        if getattr(handler, "name", None) == _HANDLER_NAME:
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.name = _HANDLER_NAME
    handler.addFilter(RequestIdFilter())
    if settings.is_dev:
        handler.setFormatter(
            DevFormatter("%(asctime)s %(levelname)-5s [%(request_id)s] %(name)s: %(message)s")
        )
    else:
        handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    # RequestContextMiddleware writes the access line (route + latency); silence uvicorn's own.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
