"""Alembic environment (async engine).

The models live in ``backend/`` (``core.models``); this file puts that folder on ``sys.path`` so
the migrations can be run from anywhere with ``alembic -c database/alembic.ini …``. The database
URL comes from ``sqlalchemy.url`` when set programmatically (tests), otherwise from the backend
settings (``DATABASE_URL`` / ``backend/.env``).
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from geoalchemy2 import alembic_helpers  # noqa: E402

from core.models import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    # Keep the application's loggers alive when migrations run in-process (API startup, tests).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

if not config.get_main_option("sqlalchemy.url"):
    from core.config import get_settings

    # '%' must be escaped for configparser interpolation.
    config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))

target_metadata = Base.metadata

# PostGIS-managed objects must never be touched by autogenerate.
_POSTGIS_TABLES = {"spatial_ref_sys"}


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    if type_ == "table" and name in _POSTGIS_TABLES:
        return False
    return alembic_helpers.include_object(obj, name, type_, reflected, compare_to)


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_object=include_object,
        process_revision_directives=alembic_helpers.writer,
        render_item=alembic_helpers.render_item,
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
