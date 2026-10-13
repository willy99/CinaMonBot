"""
Перемикачі мови (UA/EN) і теми (темна/світла) в адмінці через симульований браузер `user`.

Типово — українська і темна тема (існуючі тести сторінок це вже перевіряють). Тут: після кліку по EN
у сайдбарі / на логіні дашборд, /login, /notifications і /checks показують англійські підписи;
тема зберігається в app.storage.user і ui.dark_mode її відображає на всіх сторінках.
"""
from __future__ import annotations

import pytest
from nicegui import app, ui

from tests.admin_helpers import login, make_admin, make_tracker, make_user, reset_auth_state, table_with, wait_until
from tests.test_log_pages import _seed


@pytest.fixture(autouse=True)
def _clean_auth_state():
    reset_auth_state()
    yield
    reset_auth_state()


def _session() -> dict:
    """Сховище єдиного симульованого користувача (app.storage.user поза контекстом сторінки недоступний)."""
    stores = list(app.storage._users.values())
    assert len(stores) == 1
    return stores[0]


def _dark_mode(user) -> ui.dark_mode:
    return user.find(kind=ui.dark_mode).elements.pop()


def _theme_icon(user) -> str:
    return user.find(marker="theme-toggle").elements.pop().props["icon"]


async def _toggle_theme(user, expect_icon: str) -> None:
    """Клік по сонцю/місяцю + очікування reload (старий DOM ще видимий, тому чекаємо на нову іконку)."""
    user.find(marker="theme-toggle").click()
    await wait_until(lambda: any(b.props.get("icon") == expect_icon for b in user.find(kind=ui.button).elements),
                     what=f"іконка теми {expect_icon}")


# ─── Мова ────────────────────────────────────────────────────────

async def test_defaults_are_ukrainian_and_dark(db, user):
    await make_admin()
    await login(user)
    await user.open("/")
    await user.should_see("Дашборд")
    await user.should_see("адміністратор")
    assert _dark_mode(user).value is True
    assert _theme_icon(user) == "light_mode"      # кнопка пропонує перейти на світлу
    assert _session().get("admin_lang") is None   # нічого не записано, поки не перемкнули


async def test_switch_to_english_from_drawer_persists_across_pages(db, user):
    await make_admin()
    alice = await make_user(1001, "Alice", "alice")
    await make_tracker(alice.id, source="allo.ua", title="Dyson", status="error", errors=3)
    await login(user)
    await user.open("/")
    user.find(marker="lang-en").click()
    await user.should_see("Dashboard", retries=40)
    assert _session()["admin_lang"] == "en"
    await user.should_see("administrator")
    await user.should_see("Users")
    await user.should_see("Premium active")
    await user.should_see("New users →")
    await user.should_see("Price checks →")
    await user.should_see("Admin 2FA")
    await user.should_see("disabled")
    await user.should_see("Nobody — all subscriptions")
    await user.should_see("No data yet")
    await user.should_not_see("Дашборд")
    await user.should_not_see("Даних ще немає")
    problems = table_with(user, "errors")
    assert [c["label"] for c in problems.columns] == ["Product", "Site", "Errors", "Status", "Checked"]
    assert "Error" in problems.slots["body-cell-status"].template and "Помилка" not in problems.slots["body-cell-status"].template

    # інша сторінка бере мову з сесії
    await user.open("/checks")
    await user.should_see("Price checks")
    await user.should_see("Showing 0–0 of 0")
    empty = user.find(kind=ui.table).elements.pop()
    assert "No checks yet" in empty.slots["no-data"].template      # слот не індексується should_see

    # і назад на UA
    user.find(marker="lang-uk").click()
    await user.should_see("Перевірки цін", retries=40)
    assert _session()["admin_lang"] == "uk"


async def test_notifications_and_checks_in_english(db, user):
    await _seed()
    await make_admin()
    await login(user)
    await user.open("/notifications")
    await user.should_see("Показано 1–25 з 60")
    user.find(marker="lang-en").click()
    await user.should_see("Notifications", retries=40)
    await user.should_see("Showing 1–25 of 60")
    await user.should_see("page 1 of 3")
    await user.should_see("25 per page, newest first")
    await user.should_not_see("Показано")
    table = table_with(user, "event_label")
    assert [c["label"] for c in table.columns] == ["When", "Event", "Product", "Price", "User", "Status"]
    assert {r["event_label"] for r in table.rows} <= {"Price drop", "Target reached"}
    assert "Sent" in table.slots["body-cell-status"].template and "Надіслано" not in table.slots["body-cell-status"].template
    assert "₴" in table.rows[0]["change"]                       # гроші теж англійським форматом
    select = user.find(kind=ui.select).elements.pop()
    assert select.options["all"] == "All events" and select.options["decrease"] == "Price drop"

    await user.open("/checks?page=2")
    await user.should_see("Price checks")
    await user.should_see("Showing 26–30 of 30")
    await user.should_see("page 2 of 2")
    checks = table_with(user, "price_str")
    assert "In stock" in checks.slots["body-cell-available"].template
    assert "В наявності" not in checks.slots["body-cell-available"].template


async def test_login_page_switches_language(db, user):
    await user.open("/login")
    await user.should_see("Увійдіть, щоб продовжити")
    user.find(marker="lang-en").click()
    await user.should_see("Sign in to continue", retries=40)
    await user.should_see("Username")
    await user.should_see("Password")
    await user.should_not_see("Увійдіть, щоб продовжити")
    user.find(kind=ui.button, content="Sign in").click()      # "Sign in to continue" — label, не кнопка
    await user.should_see("Enter your username and password")
    # після входу мова лишається англійською
    await make_admin()
    user.find("Username").type("tester")
    user.find("Password").type("correct-horse-7")
    user.find(kind=ui.button, content="Sign in").click()
    await user.should_see("administrator", retries=40)
    await user.should_see("Dashboard")


async def test_unknown_language_in_session_falls_back_to_uk(db, user):
    await make_admin()
    await login(user)
    _session()["admin_lang"] = "de"
    _session()["admin_theme"] = "sepia"
    await user.open("/")
    await user.should_see("Дашборд")
    assert _dark_mode(user).value is True


# ─── Тема ────────────────────────────────────────────────────────

async def test_theme_toggle_persists_and_dark_mode_reflects_it(db, user):
    await make_admin()
    await login(user)
    await user.open("/")
    assert _dark_mode(user).value is True
    await _toggle_theme(user, "dark_mode")
    await user.should_see("Дашборд")
    assert _session()["admin_theme"] == "light"
    assert _dark_mode(user).value is False
    assert _theme_icon(user) == "dark_mode"

    # інша сторінка і логін теж світлі
    await user.open("/notifications")
    assert _dark_mode(user).value is False
    await user.open("/checks")
    assert _dark_mode(user).value is False

    await _toggle_theme(user, "light_mode")
    await user.should_see("Перевірки цін")
    assert _session()["admin_theme"] == "dark"
    assert _dark_mode(user).value is True


async def test_login_page_theme_toggle(db, user):
    await user.open("/login")
    assert _dark_mode(user).value is True
    await _toggle_theme(user, "dark_mode")
    await user.should_see("Увійдіть, щоб продовжити")
    assert _dark_mode(user).value is False
    assert _session()["admin_theme"] == "light"


async def test_light_palette_is_used_for_charts_in_light_mode(db, user):
    """Графіки беруть кольори з palette(): у світлій темі — світлі токени, а не темні константи."""
    from app.admin import theme

    await make_admin()
    alice = await make_user(1001, "Alice", "alice")
    await make_tracker(alice.id, title="iPhone 15")
    await login(user)
    _session()["admin_theme"] = "light"
    await user.open("/")
    charts = user.find(kind=ui.echart).elements
    assert charts
    for chart in charts:
        opt = chart.options
        axis_color = opt["xAxis"]["axisLabel"]["color"] if opt["xAxis"].get("axisLabel") else None
        if axis_color:
            assert axis_color == theme._LIGHT["muted"]
        tooltip = opt["tooltip"]
        assert tooltip["backgroundColor"] == theme._LIGHT["surface2"]
        assert tooltip["textStyle"]["color"] == theme._LIGHT["ink"]
        colors = {s["itemStyle"]["color"] for s in opt["series"]}
        assert colors <= set(theme._LIGHT["series"]), colors
