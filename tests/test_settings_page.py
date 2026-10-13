"""/settings у PriceGuard (botkit): зміна ліміту Free діє в боті без рестарту; меню має «Сповіщення», «Перевірки», «Налаштування»."""
from __future__ import annotations

import pytest
from nicegui import ui

from app.admin.theme import NAV
from app.services import runtime_settings as rt
from tests.admin_helpers import login, make_admin, reset_auth_state


@pytest.fixture(autouse=True)
def _clean(db):
    reset_auth_state()
    rt.reset_cache()
    yield
    rt.reset_cache()


def test_menu_has_notifications_checks_settings():
    paths = [p for p, _, _ in NAV]
    assert {"/notifications", "/checks", "/settings"} <= set(paths)


async def test_free_limit_override_applies_without_restart():
    from app.database.models import User
    u = User(telegram_id=1)
    assert u.active_tracker_limit == rt.default("FREE_TIER_MAX_ITEMS")
    await rt.set_value("FREE_TIER_MAX_ITEMS", 12, by="pavel")
    assert u.active_tracker_limit == 12 and rt.cached("PREMIUM_PRICE_UAH") == rt.default("PREMIUM_PRICE_UAH")


async def test_settings_page_save_and_reset(user):
    await make_admin()
    await login(user)
    await user.open("/settings")
    await user.should_see("Перевірка цін", retries=40)
    field = [e for e in user.find(kind=ui.number).elements if "setting-FREE_TIER_MAX_ITEMS" in (e._markers or [])][0]
    field.value = 9
    user.find(marker="save-FREE_TIER_MAX_ITEMS").click()
    await user.should_see("Збережено: FREE_TIER_MAX_ITEMS = 9", retries=40)
    assert await rt.get("FREE_TIER_MAX_ITEMS") == 9
    user.find(marker="reset-FREE_TIER_MAX_ITEMS").click()
    await user.should_see("Скинуто до .env: FREE_TIER_MAX_ITEMS", retries=40)
    assert await rt.get("FREE_TIER_MAX_ITEMS") == rt.default("FREE_TIER_MAX_ITEMS")
