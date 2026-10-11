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
os.environ["ENVIRONMENT"] = "production"   # вимикає SQL echo у логах

import logging  # noqa: E402

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

import pytest_asyncio  # noqa: E402

pytest_plugins = ["nicegui.testing.user_plugin"]


@pytest_asyncio.fixture
async def db():
    """Чиста база на тест."""
    from app.database.models import Base
    from app.database.session import engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()
