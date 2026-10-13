"""
Спільний каркас тестів.

- DATABASE_URL виставляється ДО імпорту app.* (settings читає env при імпорті).
  Усі тести працюють на тимчасовій SQLite-базі в tmp-каталозі сесії.
- Фікстура `db`: чиста база (drop_all + create_all) на кожен тест, engine
  диспозиться після тесту, щоб з'єднання не прилипали до чужого event loop.
- Фікстура `user` (NiceGUI testing plugin): симульований браузер для сторінок
  адмінки; головний файл — admin.py (див. pytest.ini).
- Фабрики фейкових Telegram-об'єктів — у tests/helpers.py.
"""
from __future__ import annotations

import os
import tempfile

_TMP_DIR = tempfile.mkdtemp(prefix="priceguard-tests-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DIR}/test.db"
os.environ["BOT_TOKEN"] = "123456:test-token"
os.environ["ADMIN_2FA_ENABLED"] = "false"
os.environ["ADMIN_STORAGE_SECRET"] = "test-secret"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["ADMIN_TELEGRAM_ID"] = "431742835"   # збігається з tests.helpers.ADMIN_ID
os.environ["CARD_NUMBER"] = "0000 0000 0000 0000"
os.environ["CARD_IBAN"] = "UA00 000000 0000 0000 0000 0000000"
os.environ["ENVIRONMENT"] = "production"   # вимикає SQL echo у логах

import logging  # noqa: E402

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

pytest_plugins = ["nicegui.testing.user_plugin"]



def _stamp_head(conn) -> None:
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from app.database.migrate import alembic_config
    MigrationContext.configure(conn).stamp(ScriptDirectory.from_config(alembic_config()), "head")

@pytest_asyncio.fixture
async def db():
    """Чиста база на тест."""
    from app.database.models import Base
    from app.database.session import engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_stamp_head)     # схема = моделі = head (процеси перевіряють ensure_schema)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _offline_dns(monkeypatch):
    """Тести не ходять у DNS: будь-який домен — публічна адреса; літеральні IP netguard перевіряє як є."""
    from app.services.scrapers import netguard

    async def fake_resolve(host: str) -> list[str]:
        return ["93.184.216.34"]
    monkeypatch.setattr(netguard, "_resolve", fake_resolve)
    netguard.clear_cache()
    from app.scheduler import breaker
    breaker.reset()
    from app.services import ratelimit
    ratelimit.reset()
