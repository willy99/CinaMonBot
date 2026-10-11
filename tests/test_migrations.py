"""init_db додає відсутні колонки в наявні таблиці (адитивна міграція) — на SQLite."""
from __future__ import annotations

from sqlalchemy import inspect, text

from app.database.models import Base
from app.database.session import engine, init_db


async def test_init_db_adds_missing_columns(db):
    # Імітуємо стару базу: прибираємо кілька нових колонок
    async with engine.begin() as conn:
        for tbl, col in (("price_trackers", "notify_drop"), ("price_trackers", "target_percent"), ("users", "digest_enabled")):
            await conn.execute(text(f"ALTER TABLE {tbl} DROP COLUMN {col}"))
        cols = await conn.run_sync(lambda c: {x["name"] for x in inspect(c).get_columns("price_trackers")})
        assert "notify_drop" not in cols
    await init_db()
    async with engine.begin() as conn:
        tr_cols = await conn.run_sync(lambda c: {x["name"]: x for x in inspect(c).get_columns("price_trackers")})
        u_cols = await conn.run_sync(lambda c: {x["name"] for x in inspect(c).get_columns("users")})
    assert {"notify_drop", "target_percent"} <= set(tr_cols) and "digest_enabled" in u_cols
    # NOT NULL-колонка з дефолтом: існуючі рядки отримують True
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO users (id, telegram_id, language_code, tier, is_blocked, created_at, updated_at) "
                                "VALUES ('u1', 1, 'uk', 'free', 0, '2026-01-01', '2026-01-01')"))
        val = (await conn.execute(text("SELECT digest_enabled FROM users WHERE id='u1'"))).scalar_one()
    assert val == 1


async def test_init_db_is_idempotent(db):
    await init_db()
    await init_db()
    async with engine.begin() as conn:
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    assert set(Base.metadata.tables) <= tables
