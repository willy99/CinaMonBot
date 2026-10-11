"""Перевірка каркасу тестів: база, фабрики Telegram, тестовий клієнт NiceGUI."""
from __future__ import annotations

from sqlalchemy import select

from app.database.models import User
from app.database.session import get_session
from tests.helpers import capture_bot, make_message


async def test_db_fixture_is_clean(db):
    async with get_session() as s:
        assert (await s.execute(select(User))).scalars().all() == []


async def test_handlers_with_fake_telegram(db):
    from app.bot.handlers import main as h
    with capture_bot() as sent:
        await h.cmd_start(make_message(1, "/start", lang="en"))
    assert "Hi, Tester" in sent[-1].text
    assert "➕ Add item" in sent[-1].buttons


async def test_admin_login_page_renders(db, user):
    await user.open("/login")
    await user.should_see("ЦінаБот Admin")
    await user.should_see("Увійти")


async def test_admin_redirects_unauthenticated(db, user):
    await user.open("/")
    await user.should_see("Увійти")
