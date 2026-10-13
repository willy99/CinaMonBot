"""
Спільне тіло env.py для Alembic: міграції на async-engine (SQLite — batch-режим для ALTER).
У проєкті app/database/migrations/env.py лише викликає run(Base.metadata, settings.DATABASE_URL).
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

config = None
target_metadata = None
_default_url = ""


def _url() -> str:
    return config.attributes.get("url") or _default_url


def _configure(**kw) -> None:
    context.configure(target_metadata=target_metadata, render_as_batch=_url().startswith("sqlite"),
                      compare_type=True, compare_server_default=True, **kw)


def run_migrations_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url(), poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:          # виклик із уже відкритого з'єднання (тести, migrate.py)
        _run(connection)
    else:
        asyncio.run(_run_async())


def run(metadata, default_url: str) -> None:
    global config, target_metadata, _default_url
    config, target_metadata, _default_url = context.config, metadata, default_url
    if config.config_file_name and config.attributes.get("configure_logger", True):
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
