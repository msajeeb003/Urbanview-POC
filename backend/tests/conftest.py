from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient

from core.config import Settings
from tests.helpers import make_app, make_client, make_redis, make_settings


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def fake_redis():
    return make_redis()


@pytest.fixture
def app(settings: Settings, fake_redis) -> FastAPI:
    return make_app(settings, fake_redis)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            yield c
