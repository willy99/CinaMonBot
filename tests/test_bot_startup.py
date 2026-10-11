"""bot.py: on_startup реєструє меню команд для кожної мови + дефолт; on_shutdown зупиняє scheduler і engine."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import bot as bot_module
from app.i18n import BOT_COMMANDS, DEFAULT_LANG


def _fake_bot() -> MagicMock:
    fake = MagicMock(name="bot")
    fake.delete_webhook = AsyncMock()
    fake.set_my_commands = AsyncMock()
    fake.get_me = AsyncMock(return_value=MagicMock(username="pricebot"))
    return fake


async def test_on_startup_sets_commands_per_language_and_default(monkeypatch):
    init_db = AsyncMock()
    setup_scheduler = MagicMock()
    monkeypatch.setattr(bot_module, "init_db", init_db)
    monkeypatch.setattr(bot_module, "setup_scheduler", setup_scheduler)
    fake = _fake_bot()

    await bot_module.on_startup(fake)

    init_db.assert_awaited_once()
    setup_scheduler.assert_called_once_with(fake)
    fake.delete_webhook.assert_awaited_once_with(drop_pending_updates=True)
    fake.get_me.assert_awaited_once()

    calls = fake.set_my_commands.await_args_list
    assert len(calls) == len(BOT_COMMANDS) + 1

    by_lang: dict[str | None, list[tuple[str, str]]] = {}
    for call in calls:
        cmds = call.args[0]
        by_lang[call.kwargs.get("language_code")] = [(c.command, c.description) for c in cmds]

    for lang, commands in BOT_COMMANDS.items():
        assert by_lang[lang] == list(commands), lang
    # дефолтне меню (без language_code) — це меню мови за замовчуванням
    assert by_lang[None] == list(BOT_COMMANDS[DEFAULT_LANG])


async def test_on_startup_order_db_before_scheduler_before_commands(monkeypatch):
    order: list[str] = []

    async def init_db():
        order.append("init_db")

    monkeypatch.setattr(bot_module, "init_db", init_db)
    monkeypatch.setattr(bot_module, "setup_scheduler", lambda b: order.append("scheduler"))
    fake = _fake_bot()
    fake.set_my_commands = AsyncMock(side_effect=lambda *a, **k: order.append("commands"))

    await bot_module.on_startup(fake)
    assert order[0] == "init_db" and order[1] == "scheduler" and order[2] == "commands"


async def test_on_shutdown_stops_scheduler_and_engine(monkeypatch):
    scheduler = MagicMock()
    close_engine = AsyncMock()
    monkeypatch.setattr(bot_module, "scheduler", scheduler)
    monkeypatch.setattr(bot_module, "close_engine", close_engine)

    await bot_module.on_shutdown(_fake_bot())

    scheduler.shutdown.assert_called_once_with(wait=False)
    close_engine.assert_awaited_once()
