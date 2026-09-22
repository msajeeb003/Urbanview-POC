"""Async SQLAlchemy engine/session for PostgreSQL + PostGIS.

Domain models live in ``core.models``. Every domain table carries a ``municipality_id`` column so
one deployment can serve several municipalities and data stays separable (BRD §8).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

if TYPE_CHECKING:
    from core.config import Settings


class Base(DeclarativeBase):
    pass


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        echo=settings.db_echo,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


async def check_database(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
