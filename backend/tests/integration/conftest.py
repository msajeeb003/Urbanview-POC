"""PostGIS integration fixtures.

Session setup (once): probe ``TEST_DATABASE_URL`` (skip everything if unreachable), reset the
``public`` schema, run the migrations up, down to base and up again (they must round-trip), then
load ``database/seeds/podgorica_sample`` plus synthetic volume. Tests then get an app wired to the
real resolver, an HTTP client and a raw connection for EXPLAIN.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import Settings
from core.seeds import load_sample, load_synthetic_bulk
from tests.helpers import make_app, make_client, make_settings

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "database" / "alembic.ini"
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://urbanview:urbanview@localhost:5432/urbanview_test"
)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


async def _probe(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def _reset_schema(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


async def _seed(url: str) -> tuple[dict[str, int], dict[str, int]]:
    """The hand-made sample plus synthetic volume (300 documents, 10k cadastral + 10k planned
    parcels east of the sample) so plans and latencies are measured on realistic table sizes."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            sample = await load_sample(session, "podgorica_sample")
            bulk = await load_synthetic_bulk(session)
            return sample, bulk
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def postgis_url() -> str:
    try:
        asyncio.run(asyncio.wait_for(_probe(TEST_DATABASE_URL), 5))
    except Exception as exc:  # noqa: BLE001 - any failure means "no database here"
        pytest.skip(
            f"PostGIS integration tests need TEST_DATABASE_URL={TEST_DATABASE_URL}: "
            f"{type(exc).__name__}: {exc}"
        )
    asyncio.run(_reset_schema(TEST_DATABASE_URL))
    cfg = alembic_config(TEST_DATABASE_URL)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")  # migrations must round-trip
    command.upgrade(cfg, "head")
    sample, bulk = asyncio.run(_seed(TEST_DATABASE_URL))
    assert sample["cadastral_parcels"] == 7 and sample["planning_documents"] == 5
    assert sample["urban_parcels"] == 6
    assert bulk["cadastral_parcels"] == 10_000 and bulk["planning_documents"] == 300
    assert bulk["urban_parcels"] >= 10_000
    return TEST_DATABASE_URL


@pytest.fixture
def pg_settings(postgis_url: str) -> Settings:
    return make_settings(
        location_resolver="postgis", database_url=postgis_url, rate_limit_requests=100_000
    )


@pytest.fixture
def pg_app(pg_settings: Settings) -> FastAPI:
    return make_app(pg_settings)


@pytest_asyncio.fixture
async def pg_client(pg_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with pg_app.router.lifespan_context(pg_app):
        async with make_client(pg_app) as client:
            yield client


@pytest_asyncio.fixture
async def pg_conn(postgis_url: str) -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(postgis_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()
