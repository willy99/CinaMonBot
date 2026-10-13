"""
Тести botkit: мінімальні моделі в тимчасовій SQLite і власний контекст на час тесту (контекст проєкту,
якщо тести йдуть усередині проєкту, відновлюється після кожного тесту).
"""
from __future__ import annotations

import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import botkit
from botkit import context as bk_context


class Base(DeclarativeBase):
    pass


def _id() -> str:
    return str(uuid.uuid4())


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    role: Mapped[str] = mapped_column(String(16), default="admin")
    session_version: Mapped[int] = mapped_column(Integer, default=0)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AdminAction(Base):
    __tablename__ = "admin_actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    admin_id: Mapped[str | None] = mapped_column(String(36))
    admin_username: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)


class Heartbeat(Base):
    __tablename__ = "heartbeats"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime)
    info: Mapped[str | None] = mapped_column(Text)


def make_settings(db_url: str, tmp: Path) -> SimpleNamespace:
    return SimpleNamespace(
        BOT_TOKEN=SecretStr("123456:TEST"), ADMIN_TELEGRAM_ID=1, DATABASE_URL=db_url, is_sqlite=True,
        ADMIN_SESSION_HOURS=12, ADMIN_2FA_CODE_TTL_SECONDS=300, ADMIN_LOGIN_MAX_ATTEMPTS=5,
        ADMIN_LOGIN_LOCK_MINUTES=15, ADMIN_2FA_ENABLED=False, BACKUP_DIR=str(tmp / "backups"), BACKUP_KEEP_DAYS=14,
        BACKUP_REMOTE="", HEALTHCHECK_PING_URL="", API_HEALTH_URL="", USER_REQUESTS_PER_MINUTE=20,
    )


@pytest.fixture
async def kit():
    """Контекст botkit на тимчасовій базі. Повертає (context, Base) і відновлює попередній контекст."""
    tmp = Path(tempfile.mkdtemp(prefix="botkit-test-"))
    url = f"sqlite+aiosqlite:///{tmp}/t.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def get_session():
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    previous = bk_context._ctx
    ctx = botkit.Context(
        app_name="TestBot", settings=make_settings(url, tmp), get_session=get_session,
        models=botkit.Models(Admin, AdminAction, User, Heartbeat), root=tmp,
        t=lambda lang, key, **kw: f"{key}:{kw}", resolve_lang=lambda code: "uk",
        heartbeat_tick_seconds=60, backup_prefix="testbot-",
    )
    botkit.setup(ctx)
    from botkit import ratelimit
    ratelimit.reset()
    try:
        yield ctx
    finally:
        bk_context._ctx = previous
        await engine.dispose()
