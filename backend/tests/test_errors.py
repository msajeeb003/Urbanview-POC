"""Uniform error envelope: {"error": {"code", "message", "request_id", "details"?}}."""

from __future__ import annotations

import logging

import pytest
from fastapi import APIRouter, FastAPI

from core.errors import AppError, NotFoundError
from tests.helpers import make_app, make_settings


def _install_test_routes(app: FastAPI) -> None:
    router = APIRouter(prefix="/_test")

    @router.get("/not-found")
    async def not_found():
        raise NotFoundError("Order not found", details={"order_id": 42})

    @router.get("/custom")
    async def custom():
        raise AppError(
            "Free AI interactions used up",
            code="ai_quota_exhausted",
            status_code=402,
            details={"free_interactions": 3},
        )

    @router.get("/boom")
    async def boom():
        raise RuntimeError("secret internal detail")

    app.include_router(router)


@pytest.fixture
def app(fake_redis) -> FastAPI:
    app = make_app(make_settings(rate_limit_requests=1000), fake_redis)
    _install_test_routes(app)
    return app


async def test_unknown_route_uses_envelope(client):
    r = await client.get("/does-not-exist")
    assert r.status_code == 404
    assert r.json() == {
        "error": {
            "code": "not_found",
            "message": "Not Found",
            "request_id": r.headers["X-Request-ID"],
        }
    }


async def test_method_not_allowed_uses_envelope(client):
    r = await client.post("/health")
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "method_not_allowed"


async def test_validation_error_422_with_details(client):
    r = await client.get("/v1/locate", params={"lat": "abc", "lng": 19.26})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert err["message"] == "Request validation failed"
    assert isinstance(err["details"], list)
    assert "lat" in err["details"][0]["loc"]


async def test_app_error_subclass(client):
    r = await client.get("/_test/not-found")
    assert r.status_code == 404
    err = r.json()["error"]
    assert err == {
        "code": "not_found",
        "message": "Order not found",
        "request_id": r.headers["X-Request-ID"],
        "details": {"order_id": 42},
    }


async def test_app_error_with_custom_code_and_status(client):
    r = await client.get("/_test/custom")
    assert r.status_code == 402
    err = r.json()["error"]
    assert err["code"] == "ai_quota_exhausted"
    assert err["details"] == {"free_interactions": 3}


async def test_unhandled_exception_is_logged_with_request_id_and_not_leaked(client, caplog):
    with caplog.at_level(logging.ERROR, logger="urbanview.errors"):
        r = await client.get("/_test/boom", headers={"X-Request-ID": "req-boom-1"})

    assert r.status_code == 500
    assert r.headers["X-Request-ID"] == "req-boom-1"
    assert r.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal server error",
            "request_id": "req-boom-1",
        }
    }
    assert "secret internal detail" not in r.text

    record = next(rec for rec in caplog.records if rec.name == "urbanview.errors")
    assert record.levelno == logging.ERROR
    assert record.request_id == "req-boom-1"
    assert record.error_type == "RuntimeError"
    assert record.exc_info and record.exc_info[0] is RuntimeError
