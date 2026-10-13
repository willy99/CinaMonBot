"""A1: схему веде Alembic — свіжа база, база до Alembic, перевірка в процесах, паралельний запуск, моделі = head."""
from __future__ import annotations

import asyncio

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from app.database import migrate as mg
from app.database.models import Base
from app.database.session import engine

pytestmark = pytest.mark.usefixtures("db")


async def drop_everything() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    await engine.dispose()          # міграція йде окремим engine в іншому потоці


async def columns(table: str) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns(table)})


async def test_fresh_database_upgrades_to_head_matching_models():
    await drop_everything()
    assert await asyncio.to_thread(mg.migrate) == mg.head_revision()
    assert (await mg.current_revision())[0] == mg.head_revision()
    async with engine.connect() as conn:
        diff = await conn.run_sync(lambda c: compare_metadata(
            MigrationContext.configure(c, opts={"compare_type": True}), Base.metadata))
    # Змінили модель і забули ревізію → тут з'явиться різниця: alembic revision --autogenerate -m "..."
    assert diff == []


async def test_pre_alembic_database_is_caught_up_and_stamped():
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE alembic_version"))
        await conn.execute(text("ALTER TABLE users DROP COLUMN digest_enabled"))     # «стара» схема
    await engine.dispose()
    assert "digest_enabled" not in await columns("users")
    await asyncio.to_thread(mg.migrate)
    assert "digest_enabled" in await columns("users")
    assert (await mg.current_revision())[0] == mg.head_revision()


async def test_processes_refuse_outdated_schema():
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE alembic_version"))
    with pytest.raises(mg.SchemaOutdated, match="run.sh migrate"):
        await mg.ensure_schema()


async def test_parallel_migrations_do_not_collide():
    await drop_everything()
    results = await asyncio.gather(*(asyncio.to_thread(mg.migrate) for _ in range(3)))
    assert set(results) == {mg.head_revision()}
    await mg.ensure_schema()


async def test_split_bot_blocked_data_migration():
    """Заблоковані адміном (є «user_block» у журналі) лишаються is_blocked; решта — це юзер заблокував бота."""
    from alembic import command
    await drop_everything()
    cfg = mg.alembic_config()
    await asyncio.to_thread(command.upgrade, cfg, "f1aa0eecc704")              # схема до розділення
    async with engine.begin() as conn:
        for uid, tg in (("u-admin", 1), ("u-self", 2), ("u-ok", 3)):
            await conn.execute(text(
                "INSERT INTO users (id, telegram_id, language_code, tier, is_blocked, digest_enabled, created_at, updated_at) "
                f"VALUES ('{uid}', {tg}, 'uk', 'free', {1 if tg < 3 else 0}, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        await conn.execute(text(
            "INSERT INTO admin_actions (id, admin_username, action, target, created_at) "
            "VALUES ('a1', 'pavel', 'user_block', '1', CURRENT_TIMESTAMP)"))
    await engine.dispose()
    await asyncio.to_thread(command.upgrade, cfg, "head")
    async with engine.connect() as conn:
        rows = dict((r[0], (r[1], r[2])) for r in await conn.execute(text("SELECT id, is_blocked, bot_blocked FROM users")))
    assert rows == {"u-admin": (1, 0), "u-self": (0, 1), "u-ok": (0, 0)}


async def test_pre_alembic_database_runs_data_migrations():
    """База до Alembic: схему доводить legacy_upgrade, але міграції даних ревізій після baseline теж виконуються."""
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE alembic_version"))
        await conn.execute(text("DROP TABLE heartbeats"))
        await conn.execute(text("ALTER TABLE users DROP COLUMN bot_blocked"))
        await conn.execute(text(
            "INSERT INTO users (id, telegram_id, language_code, tier, is_blocked, digest_enabled, created_at, updated_at) "
            "VALUES ('u-self', 7, 'uk', 'free', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    await engine.dispose()
    await asyncio.to_thread(mg.migrate)
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT is_blocked, bot_blocked FROM users WHERE id = 'u-self'"))).one()
    assert tuple(row) == (0, 1)
    assert (await mg.current_revision())[0] == mg.head_revision()
