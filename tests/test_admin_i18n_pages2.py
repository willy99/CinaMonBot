"""
Локалізація і тема решти сторінок адмінки (phase 2): /users, /users/{id}, /trackers, /platforms, /feedback, /admins.

Для кожної сторінки: англійські підписи після перемикання мови (заголовок, колонки таблиць, порожні стани,
слоти-бейджі без українських літералів), smoke-рендер у світлій темі і по одному діалогу (картка юзера,
валідація створення адміна) англійською у світлій темі. Українські тексти не чіпаємо — їх пінять інші тести.
"""
from __future__ import annotations

import re

import pytest
from nicegui import app, ui

from app.admin import queries as q
from app.database.models import ScrapeLog
from app.database.session import get_session
from app.services.scrapers import registry
from tests.admin_helpers import (
    input_named, login, make_admin, make_tracker, make_user, reset_auth_state, table_with, tables, utc, wait_until,
)
from tests.helpers import fsm_storage
from tests.test_feedback import _send_feedback


@pytest.fixture(autouse=True)
def _clean_auth_state():
    reset_auth_state()
    yield
    reset_auth_state()


def _session() -> dict:
    stores = list(app.storage._users.values())
    assert len(stores) == 1
    return stores[0]


def _dark_mode(user) -> ui.dark_mode:
    return user.find(kind=ui.dark_mode).elements.pop()


async def _login_en(user, light: bool = False) -> None:
    """Вхід + англійська (і за потреби світла тема) прямо в сесії — як робить перемикач у сайдбарі."""
    await login(user)
    _session()["admin_lang"] = "en"
    if light:
        _session()["admin_theme"] = "light"


def _no_data(table: ui.table) -> str:
    return table.slots["no-data"].template


def _assert_slots_compile(user) -> None:
    """Симульований браузер не компілює Vue, тож ловимо типову поломку окремо: подвійні лапки (json.dumps)
    всередині :class="..." обривають атрибут, і Quasar не рендерить таблицю зовсім."""
    for table in tables(user):
        for name, slot in table.slots.items():
            for m in re.finditer(r':(?:class|style|title)="([^"]*)"', slot.template or ""):
                assert not m.group(1).rstrip().endswith("{"), f"{name}: обірваний атрибут — {m.group(0)!r}"


# ─── /users ──────────────────────────────────────────────────────

async def test_users_page_in_english_via_drawer_switch(db, user):
    await make_admin()
    alice = await make_user(1001, "Alice", "alice")
    await make_user(1002, "Bob", "bobby", tier="premium", premium_until=utc(days=-30))
    await make_user(1003, "Carol", None, blocked=True)
    await make_tracker(alice.id, title=None)
    await login(user)
    await user.open("/users")
    await user.should_see("3 користувачів", retries=20)
    user.find(marker="lang-en").click()
    await user.should_see("3 users", retries=40)
    await user.should_see("Users")
    await user.should_not_see("користувачів")
    _assert_slots_compile(user)
    table = table_with(user, "trackers_active")
    assert [c["label"] for c in table.columns] == \
        ["Name", "Username", "Telegram ID", "Language", "Plan", "Items", "Registered"]
    tier = table.slots["body-cell-tier"].template
    assert '"Blocked"' in tier and "Premium · until " in tier and "Заблокований" not in tier
    assert "/ {{ props.row.trackers_active }} active" in table.slots["body-cell-trackers"].template
    assert "Nobody found" in _no_data(table)
    assert table._props.get("rows-per-page-label") == "Rows:"
    toggles = user.find(kind=ui.toggle).elements
    status = next(t for t in toggles if "blocked" in t.options)
    assert status.options == {"all": "Any status", "active": "Active", "blocked": "Blocked"}
    user.find(kind=ui.input).type("zzz")
    await user.should_see("0 users", retries=20)


async def test_user_card_in_english_and_light_mode(db, user):
    await make_admin()
    alice = await make_user(1001, "Alice", "alice")
    t = await make_tracker(alice.id, title="iPhone 15", errors=2, target=35000, available=False)
    await _login_en(user, light=True)
    await user.open(f"/users/{alice.id}")
    assert _dark_mode(user).value is False
    await user.should_see("User")
    await user.should_see("← All users")
    await user.should_see("Telegram ID: 1001")
    await user.should_see("Language: uk")
    await user.should_see("📬 digest: enabled")
    await user.should_see("Items: 1")
    await user.should_see("User's items")
    await user.should_see("1 total")
    await user.should_see("Recent notifications")
    await user.should_see("No notifications yet")
    await user.should_not_see("Товарів")
    _assert_slots_compile(user)
    trackers = table_with(user, "errors")
    assert [c["label"] for c in trackers.columns][:6] == ["Product", "Site", "Price", "Target", "Status", "Checked"]
    status = trackers.slots["body-cell-status"].template
    assert '"Active"' in status and "{{ props.row.errors }} err." in status and "Активний" not in status
    assert "out of stock" in trackers.slots["body-cell-title"].template
    assert "#ec835a" not in trackers.slots["body-cell-title"].template
    assert "Price history" in trackers.slots["body-cell-actions"].template
    row = trackers.rows[0]
    assert row["notif_title"] == "Notifications: price drops on, availability on"
    assert row["price_str"] == "₴40,000"

    user.find("Activate Premium").click()
    await user.should_see("Premium activated until", retries=20)
    await user.should_see("Deactivate", retries=20)
    await user.should_see("Premium · until")
    await user.should_not_see("Premium · до")
    user.find("Deactivate").click()
    await user.should_see("Premium deactivated", retries=20)
    user.find("Block").click()
    await user.should_see("Unblock", retries=20)

    row = table_with(user, "errors").rows[0]
    user.find(kind=ui.table).trigger("pause", args=row)
    await user.should_see("Tracker paused", retries=20)
    await wait_until(lambda: table_with(user, "errors").rows[0]["status"] == "paused", what="статус paused")
    row = table_with(user, "errors").rows[0]
    user.find(kind=ui.table).trigger("history", args=row)
    await user.should_see("0 records", retries=20)
    await user.should_see("No history yet")
    await user.should_see("Close")
    assert (await q.user_detail(alice.id))["trackers"][0]["id"] == t.id


async def test_user_not_found_in_english(db, user):
    await make_admin()
    await _login_en(user)
    await user.open("/users/no-such-user")
    await user.should_see("User not found")
    await user.should_see("← Back to list")


# ─── /trackers ───────────────────────────────────────────────────

async def test_trackers_page_in_english(db, user):
    await make_admin()
    alice = await make_user(1001, "Alice", "alice")
    await make_tracker(alice.id, title="iPhone 15")
    await make_tracker(alice.id, source="comfy.ua", title=None, status="paused", price=1500)
    await _login_en(user)
    await user.open("/trackers")
    await user.should_see("Trackers")
    await user.should_see("2 trackers")
    await user.should_see("Search by product name")
    await user.should_not_see("трекерів")
    _assert_slots_compile(user)
    table = table_with(user, "price_str")
    assert [c["label"] for c in table.columns] == ["Product", "Site", "Price", "Status", "User", "Checked", "Added"]
    status = table.slots["body-cell-status"].template
    assert '"Paused"' in status and '"Paused: limit"' in status and "На паузі" not in status
    by = {r["source"]: r for r in table.rows}
    assert by["comfy.ua"]["title"] == "Untitled" and by["comfy.ua"]["price_str"] == "₴1,500"
    assert by["rozetka.com.ua"]["price_str"] == "₴40,000"
    toggle = user.find(kind=ui.toggle).elements.pop()
    assert toggle.options == {"all": "All", "active": "Active", "paused": "Paused", "error": "Errors"}
    assert user.find(kind=ui.select).elements.pop().options["all"] == "All sites"
    with user:
        toggle.value = "paused"
    await user.should_see("1 trackers", retries=20)


async def test_trackers_page_light_smoke(db, user):
    await make_admin()
    await _login_en(user, light=True)
    await user.open("/trackers")
    assert _dark_mode(user).value is False
    await user.should_see("0 trackers")


# ─── /platforms ──────────────────────────────────────────────────

async def test_platforms_page_in_english_with_detail_dialog_in_light_mode(db, user):
    await make_admin()
    platform = next(p for p in registry.PLATFORMS if p.status == "verified" and p.notes)
    alice = await make_user(1001, "Alice", "alice")
    await make_tracker(alice.id, source=platform.domain, title="Товар")
    async with get_session() as s:
        s.add_all([
            ScrapeLog(domain=platform.domain, ok=True, strategy="json-ld", duration_ms=100, created_at=utc(hours=1)),
            ScrapeLog(domain=platform.domain, ok=False, reason="parse_failed", duration_ms=50, created_at=utc(hours=3)),
            ScrapeLog(domain=platform.domain, ok=False, reason="network", duration_ms=10, created_at=utc(hours=4)),
        ])
    await _login_en(user, light=True)
    await user.open("/platforms")
    assert _dark_mode(user).value is False
    await user.should_see("Platforms")
    await user.should_see("Verified")
    await user.should_see("Experimental")
    await user.should_see("Other sites")
    await user.should_see("domains outside the registry")
    await user.should_see("3 parse attempts")
    await user.should_see("Success rate 24 h")
    await user.should_see("Healed 30 d")
    await user.should_see("Problems in 24 h")
    await user.should_see(f"{len(registry.PLATFORMS)} platforms", retries=20)
    await user.should_not_see("платформ")

    _assert_slots_compile(user)
    problems = table_with(user, "rate")
    assert [c["label"] for c in problems.columns] == \
        ["Platform", "Domain", "Status", "Attempts", "Failures", "Rate", "Auto-heal", "Last reason"]
    assert "yes" in problems.slots["body-cell-healed"].template and "так" not in problems.slots["body-cell-healed"].template
    assert '"Verified"' in problems.slots["body-cell-status"].template
    assert "#ffb3b3" not in problems.slots["body-cell-rate"].template and "var(--critical-ink)" in problems.slots["body-cell-rate"].template

    main = table_with(user, "checks_30d")
    assert [c["label"] for c in main.columns][:6] == ["Platform", "Domain", "Country", "Status", "Strategy", "Trackers"]
    assert "/ {{ props.row.active }} active" in main.slots["body-cell-trackers"].template
    assert "Nothing found" in _no_data(main)
    select = user.find(kind=ui.select).elements.pop()
    assert select.options["all"] == "All countries"
    assert select.options["UA"] == "🇺🇦 Ukraine"
    toggle = user.find(kind=ui.toggle).elements.pop()
    assert toggle.options["other"] == "Other sites" and toggle.options["all"] == "All"

    row = next(r for r in main.rows if r["domain"] == platform.domain)
    user.find(kind=ui.table).trigger("rowClick", args=[None, row])
    await user.should_see("Recent parse attempts", retries=20)
    await user.should_see("last 3")
    await user.should_see("Trackers: 1 · active 1 · with errors 0")
    await user.should_see("Users: 1")
    await user.should_see("Successful attempts")
    await user.should_see("Failed attempts")
    await user.should_see("(uk)")                      # нотатки реєстру лишаються українськими, з підказкою
    await user.should_see(platform.notes)
    recent = table_with(user, "how")
    assert [c["label"] for c in recent.columns] == ["When", "Domain", "Result", "Strategy / reason", "Heal", "ms"]
    assert '"Error"' in recent.slots["body-cell-ok"].template and "Помилка" not in recent.slots["body-cell-ok"].template


async def test_platforms_notes_hint_hidden_in_ukrainian(db, user):
    await make_admin()
    platform = next(p for p in registry.PLATFORMS if p.notes)
    await login(user)
    await user.open("/platforms")
    await user.should_see(f"{len(registry.PLATFORMS)} платформ", retries=20)
    row = next(r for r in table_with(user, "checks_30d").rows if r["domain"] == platform.domain)
    user.find(kind=ui.table).trigger("rowClick", args=[None, row])
    await user.should_see("Останні спроби парсингу", retries=20)
    await user.should_see(platform.notes)
    await user.should_not_see("(укр.)")


# ─── /feedback ───────────────────────────────────────────────────

async def test_feedback_page_in_english_light_with_dialog(db, user):
    fsm_storage()
    await _send_feedback(12, "Хочу підтримку Amazon", lang="uk")
    await make_admin()
    await _login_en(user, light=True)
    await user.open("/feedback")
    assert _dark_mode(user).value is False
    await user.should_see("Feedback")
    await user.should_see("Total")
    await user.should_see("1 messages")
    await user.should_not_see("повідомлень")
    _assert_slots_compile(user)
    table = table_with(user, "content_type")
    assert [c["label"] for c in table.columns] == ["When", "User", "Language", "Type", "Text", "Status"]
    assert '"📝 Text"' in table.slots["body-cell-content_type"].template and "Текст" not in table.slots["body-cell-content_type"].template
    assert '"New"' in table.slots["body-cell-status"].template and "Новий" not in table.slots["body-cell-status"].template
    assert "— no text —" in table.slots["body-cell-text"].template and "#898781" not in table.slots["body-cell-text"].template
    assert "No feedback yet" in _no_data(table)
    toggle = user.find(kind=ui.toggle).elements.pop()
    assert toggle.options == {"new": "New", "all": "All", "replied": "Replied"}

    user.find(kind=ui.table).trigger("rowClick", args=[None, table.rows[0]])
    await user.should_see("Mark as unread", retries=20)
    await user.should_see("Reply")
    await user.should_see("User card →")
    await user.should_see("Previous messages")
    await user.should_see("This is the first message")
    await user.should_see("Reply text for the user")
    await user.should_not_see("Позначити непрочитаним")
    user.find(kind=ui.button, content="Reply").click()      # "Reply text..." — плейсхолдер, не кнопка
    await user.should_see("Enter the reply text", retries=20)


# ─── /admins ─────────────────────────────────────────────────────

async def test_admins_page_in_english_light_with_validation(db, user):
    await make_admin()
    await make_admin("sleepy", active=False, telegram_id=9)
    await _login_en(user, light=True)
    await user.open("/admins")
    assert _dark_mode(user).value is False
    await user.should_see("Administrators")
    await user.should_see("1 active of 2")
    await user.should_see("New admin")
    await user.should_see("Action log")
    await user.should_see("last 50")
    await user.should_see("Empty so far")
    await user.should_see("At least 10 characters")
    await user.should_not_see("активних")
    _assert_slots_compile(user)
    table = table_with(user, "username")
    assert [c["label"] for c in table.columns][:5] == ["Username", "Telegram ID (2FA)", "Role", "Status", "Last sign-in"]
    assert next(r for r in table.rows if r["username"] == "sleepy")["last_login"] == "never"
    active = table.slots["body-cell-active"].template
    assert '"Active"' in active and '"Disabled"' in active and "Вимкнений" not in active
    assert "Change password" in table.slots["body-cell-actions"].template

    user.find("Create").click()
    await user.should_see("Username: Latin letters")
    input_named(user, "Username").type("newbie")
    input_named(user, "Password").type("short")
    user.find("Create").click()
    await user.should_see("Password must be at least 10 characters")
    await user.should_not_see("не коротший")
    input_named(user, "Password").clear()
    input_named(user, "Password").type("1234567890")
    user.find("Create").click()
    await user.should_see("Password must contain both letters and digits")
    input_named(user, "Telegram ID for 2FA").type("abc")
    input_named(user, "Password").clear()
    input_named(user, "Password").type("correct-horse-7")
    user.find("Create").click()
    await user.should_see("Telegram ID must be a number")
    input_named(user, "Telegram ID for 2FA").clear()
    user.find("Create").click()
    await user.should_see("Admin newbie created", retries=20)
    await user.should_see("2 active of 3", retries=20)

    row = next(r for r in table_with(user, "username").rows if r["username"] == "sleepy")
    user.find(kind=ui.table).trigger("activate", args=row)
    await user.should_see("sleepy activated", retries=20)
    row = next(r for r in table_with(user, "username").rows if r["username"] == "sleepy")
    user.find(kind=ui.table).trigger("telegram", args=row)
    await user.should_see("Telegram ID for 2FA · sleepy", retries=20)
    await user.should_see("Cancel")
    await user.should_see("Save")
    user.find(kind=ui.table).trigger("password", args=row)
    await user.should_see("New password for sleepy", retries=20)


async def test_all_pages_light_smoke_without_errors(db, user):
    """Світла тема на всіх п'яти сторінках без помилок рендеру (ui.dark_mode вимкнено, заголовок на місці)."""
    await make_admin()
    await login(user)
    _session()["admin_theme"] = "light"
    for path, title in (("/users", "Користувачі"), ("/trackers", "Трекери"), ("/platforms", "Платформи"),
                        ("/feedback", "Фідбек"), ("/admins", "Адміністратори")):
        await user.open(path)
        await user.should_see(title)
        assert _dark_mode(user).value is False, path
        assert tables(user), path
        _assert_slots_compile(user)
