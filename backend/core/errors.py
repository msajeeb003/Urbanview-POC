"""Uniform error handling.

Every error response uses one JSON envelope::

    {"error": {"code": "not_found", "message": "...", "request_id": "...", "details": {...}}}

``details`` is omitted when empty. Unhandled exceptions are logged with the request id and
returned as ``internal_error`` without leaking internals.

Product rule (BRD S6, UX rule "No error state for uncovered areas"): a location with no adopted
planning data is NOT an error. Endpoints that resolve a location must return 200 with an explicit
uncovered result (see ``api.schemas.locations.LocationResolution``), never 404/500. Raise
``NotFoundError`` only for missing *entities* (an order id that does not exist), never for
"no data here".
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.context import get_request_id, request_id_var

log = logging.getLogger("urbanview.errors")


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: Any | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class AppError(Exception):
    """Base for expected, client-facing errors. Subclass, or pass ``code``/``status_code``."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "Internal server error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details
        self.headers = headers
        super().__init__(self.message)


class BadRequestError(AppError):
    status_code = 400
    code = "bad_request"
    message = "Bad request"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"
    message = "Authentication required"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"
    message = "Not allowed"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    message = "Resource not found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    message = "Conflict"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"
    message = "Too many requests"

    def __init__(self, retry_after: int, message: str | None = None, **kwargs: Any) -> None:
        headers = {"Retry-After": str(max(1, int(retry_after)))}
        super().__init__(message, headers=headers, **kwargs)


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"
    message = "Service temporarily unavailable"


HTTP_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    503: "service_unavailable",
}


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    details: Any | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id or get_request_id(),
    }
    if details is not None:
        body["details"] = jsonable_encoder(details)
    return JSONResponse(status_code=status_code, content={"error": body}, headers=headers)


def _request_id_for(request: Request) -> str | None:
    state = request.scope.get("state") or {}
    return state.get("request_id") or get_request_id()


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return error_response(
            exc.status_code,
            exc.code,
            exc.message,
            request_id=_request_id_for(request),
            details=exc.details,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            422,
            "validation_error",
            "Request validation failed",
            request_id=_request_id_for(request),
            details=exc.errors(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = HTTP_STATUS_CODES.get(exc.status_code, "http_error")
        if isinstance(exc.detail, str):
            message, details = exc.detail, None
        else:
            message, details = "HTTP error", exc.detail
        return error_response(
            exc.status_code,
            code,
            message,
            request_id=_request_id_for(request),
            details=details,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Runs in Starlette's ServerErrorMiddleware, outside RequestContextMiddleware, so the
        # request id is taken from scope state and re-bound for the log call.
        request_id = _request_id_for(request)
        token = request_id_var.set(request_id)
        try:
            log.exception(
                "unhandled exception",
                extra={
                    "request_id": request_id or "-",
                    "method": request.method,
                    "path": request.url.path,
                    "error_type": type(exc).__name__,
                },
            )
        finally:
            request_id_var.reset(token)
        headers = {"X-Request-ID": request_id} if request_id else None
        return error_response(
            500, "internal_error", "Internal server error", request_id=request_id, headers=headers
        )
