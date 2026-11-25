"""
Тести сторінок адмінки через симульований браузер NiceGUI (`user`): логін і 2FA, дашборд,
користувачі + картка з діями, трекери, адміни, платформи.

Кожен тест незалежний: чиста база (db), свій адмін, вхід через helpers.login().
Рядки ui.table не індексуються should_see — дивимось у .rows (tests/admin_helpers.py).

    venv/bin/python -m pytest tests/test_admin_pages.py -q
"""
from __future__ import annotations

import time
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from nicegui import ui
from nicegui.testing.user_interaction import UserInteraction
from sqlalchemy import select

from botkit.admin import auth
from app.config import settings
from app.database.models import Admin, AdminAction, PriceHistory, PriceTracker, ScrapeLog, User
from app.database.session import get_session
from app.services.scrapers import registry
from tests.admin_helpers import (
    PASSWORD, all_rows, input_named, login, logout_button, make_admin, make_tracker, make_user,
    reset_auth_state, table_with, utc, wait_until,
)


@pytest.fixture(autouse=True)
def _clean_auth_state():
    reset_auth_state()
    yield
    reset_auth_state()


async def _audit_actions() -> list[tuple[str, str, str]]:
    """Відсортовано: created_at має секундну точність, тож порядок у межах секунди невизначений."""
    async with get_session() as s:
        rows = (await s.execute(select(AdminAction))).scalars().all()
    return sorted((a.admin_username, a.action, a.target or "") for a in rows)


async def _get(model, pk):
    async with get_session() as s:
        return await s.get(model, pk)


# ─── Доступ ──────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "/users", "/trackers", "/platforms", "/admins", "/users/some-id"])
async def test_unauthenticated_lands_on_login(db, user, path):
    await user.open(path)
    await user.should_see("Увійти")
    await user.should_see("Увійдіть, щоб продовжити")
    await user.should_not_see("адміністратор")


async def test_login_page_renders(db, user):
    await user.open("/login")
    await user.should_see("ЦінаБот Admin")
    await user.should_see("Логін")
    await user.should_see("Пароль")


# ─── Логін без 2FA ───────────────────────────────────────────────

async def test_login_empty_fields(db, user):
    await user.open("/login")
    user.find("Увійти").click()
    await user.should_see("Введіть логін і пароль")


async def test_login_wrong_password(db, user):
    await make_admin()
    await user.open("/login")
    user.find("Логін").type("tester")
    user.find("Пароль").type("wrong-password-1")
    user.find("Увійти").click()
    await user.should_see("Невірний логін або пароль", retries=20)
    await user.should_not_see("адміністратор")


async def test_login_lockout_message(db, user, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_LOGIN_MAX_ATTEMPTS", 2)
    await make_admin()
    await user.open("/login")
    user.find("Логін").type("tester")
    for i in range(2):
        # The error text from the previous attempt stays on the page: clear it so should_see waits for the
        # handler of THIS attempt to finish (on a slow CPU it could otherwise overwrite the lockout message).
        for label in user.find(kind=ui.label).elements:
            if label.text.startswith("Невірний логін"):
                label.text = ""
        user.find("Пароль").clear()
        user.find("Пароль").type("wrong-password-1")
        user.find("Увійти").click()
        await user.should_see("Невірний логін або пароль", retries=20)
        await wait_until(lambda: any(a.locked_until or a.failures == i + 1 for a in auth._attempts.values()),
                         what=f"невдала спроба {i + 1} зареєстрована")
    # після блокування навіть правильний пароль не пускає
    user.find("Пароль").clear()
    user.find("Пароль").type(PASSWORD)
    user.find("Увійти").click()
    await user.should_see("Забагато спроб", retries=20)
    await user.should_not_see("адміністратор")


async def test_login_success_shows_dashboard(db, user):
    admin = await make_admin()
    await user.open("/login")
    user.find("Логін").type("Tester")   # регістр не важливий
    user.find("Пароль").type(PASSWORD)
    user.find("Увійти").click()
    await user.should_see("Дашборд", retries=40)
    await user.should_see("Користувачі")
    await user.should_see("Premium активних")
    await user.should_see("tester")   # ім'я в сайдбарі
    assert (await _get(Admin, admin.id)).last_login_at is not None


async def test_login_redirects_to_requested_page(db, user):
    await make_admin()
    await user.open("/trackers")          # middleware запам'ятовує referrer_path
    await user.should_see("Увійти")
    user.find("Логін").type("tester")
    user.find("Пароль").type(PASSWORD)
    user.find("Увійти").click()
    await user.should_see("трекерів", retries=40)
    await user.should_see("Пошук за назвою товару")


# ─── Логін з 2FA ─────────────────────────────────────────────────

async def _password_step(user, username: str = "tester") -> None:
    await user.open("/login")
    user.find("Логін").type(username)
    user.find("Пароль").type(PASSWORD)
    user.find("Увійти").click()


async def test_login_2fa_flow(db, user, monkeypatch):
    admin = await make_admin(telegram_id=4242)
    monkeypatch.setattr(settings, "ADMIN_2FA_ENABLED", True)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(auth, "send_telegram_message", send)

    await _password_step(user)
    await user.should_see("Крок 2 з 2", retries=20)
    send.assert_awaited_once()
    assert send.await_args.args[0] == 4242
    code = auth._pending[admin.id].code
    assert code in send.await_args.args[1]

    wrong = "000000" if code != "000000" else "111111"
    user.find("Код з Telegram").type(wrong)
    user.find("Підтвердити").click()
    await user.should_see("Невірний код", retries=20)
    await user.should_not_see("адміністратор")

    # перезавантаження сторінки лишає на кроці 2 (pending_2fa у сесії)
    await user.open("/login")
    await user.should_see("Крок 2 з 2")
    user.find("Код з Telegram").type(code)
    user.find("Підтвердити").click()
    await user.should_see("Дашборд", retries=40)
    assert admin.id not in auth._pending
    assert (await _get(Admin, admin.id)).last_login_at is not None


async def test_login_2fa_back_to_password_step(db, user, monkeypatch):
    await make_admin(telegram_id=4242)
    monkeypatch.setattr(settings, "ADMIN_2FA_ENABLED", True)
    monkeypatch.setattr(auth, "send_telegram_message", AsyncMock(return_value=True))
    await _password_step(user)
    await user.should_see("Крок 2 з 2", retries=20)
    user.find("Назад до входу").click()
    await user.should_see("Увійдіть, щоб продовжити")
    await user.should_not_see("Крок 2 з 2")
    await user.open("/login")              # і після перезавантаження — крок 1
    await user.should_see("Увійдіть, щоб продовжити")


async def _expire_pending_code(user, monkeypatch) -> None:
    """Крок 2 з простроченим кодом: зсуваємо monotonic у модулі auth за межі TTL і підтверджуємо."""
    await _password_step(user)
    await user.should_see("Крок 2 з 2", retries=20)
    real = time.monotonic()
    monkeypatch.setattr(auth, "time", types.SimpleNamespace(
        time=time.time, monotonic=lambda: real + settings.ADMIN_2FA_CODE_TTL_SECONDS + 1))
    user.find("Код з Telegram").type("123456")
    user.find("Підтвердити").click()


async def test_login_2fa_expired_code_returns_to_step_one(db, user, monkeypatch):
    admin = await make_admin(telegram_id=4242)
    monkeypatch.setattr(settings, "ADMIN_2FA_ENABLED", True)
    monkeypatch.setattr(auth, "send_telegram_message", AsyncMock(return_value=True))
    await _expire_pending_code(user, monkeypatch)
    await user.should_see("Увійдіть, щоб продовжити", retries=20)
    await user.should_not_see("Крок 2 з 2")
    assert admin.id not in auth._pending
    await user.should_not_see("адміністратор")


async def test_login_2fa_expired_code_message_is_shown(db, user, monkeypatch):
    await make_admin(telegram_id=4242)
    monkeypatch.setattr(settings, "ADMIN_2FA_ENABLED", True)
    monkeypatch.setattr(auth, "send_telegram_message", AsyncMock(return_value=True))
    await _expire_pending_code(user, monkeypatch)
    await user.should_see("Увійдіть, щоб продовжити", retries=20)
    await user.should_see("Код прострочений")


async def test_login_2fa_without_telegram_id(db, user, monkeypatch):
    await make_admin(telegram_id=None)
    monkeypatch.setattr(settings, "ADMIN_2FA_ENABLED", True)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(auth, "send_telegram_message", send)
    await _password_step(user)
    await user.should_see("не вказано Telegram ID", retries=20)
    send.assert_not_awaited()


async def test_login_2fa_send_failure(db, user, monkeypatch):
    await make_admin(telegram_id=4242)
    monkeypatch.setattr(settings, "ADMIN_2FA_ENABLED", True)
    monkeypatch.setattr(auth, "send_telegram_message", AsyncMock(return_value=False))
    await _password_step(user)
    await user.should_see("Не вдалося надіслати код", retries=20)
    await user.should_not_see("Крок 2 з 2")


# ─── Дашборд ─────────────────────────────────────────────────────

async def test_dashboard_empty_states(db, user):
    await make_admin()
    await login(user)
    await user.open("/")
    await user.should_see("Нікого — усі підписки")
    await user.should_see("Усе гаразд")
    await user.should_see("Даних ще немає")
    await user.should_see("Поки нема трекерів")
    await user.should_see("Поки нема активних трекерів")
    await user.should_see("2FA адмінки")
    await user.should_see("вимкнено")


async def test_dashboard_renders_seeded_numbers(db, user):
    await make_admin()
    alice = await make_user(1001, "Alice", "alice", created=utc(days=2))
    bob = await make_user(1002, "Bob", "bobby", tier="premium", premium_until=utc(days=-30))
    carol = await make_user(1003, "Carol", None, tier="premium", premium_until=utc(days=-3, hours=-1))
    await make_tracker(alice.id, title="iPhone 15")
    await make_tracker(alice.id, source="comfy.ua", title="Samsung TV", status="paused")
    await make_tracker(bob.id, source="allo.ua", title="Dyson", status="error", errors=3)
    await make_tracker(carol.id, title="Kindle", source="amazon.com")

    await login(user)
    await user.open("/")
    await user.should_see(f"≈ {2 * settings.PREMIUM_PRICE_UAH} грн/міс")
    await user.should_see("1 на паузі · 1 з помилкою")
    await user.should_see("3 за 30 днів")
    await user.should_see("всього 1")                      # Premium закінчується
    await user.should_not_see("Нікого — усі підписки")
    await user.should_not_see("Усе гаразд")
    await user.should_not_see("Поки нема трекерів")
    expiring = table_with(user, "days_left")
    assert [r["name"] for r in expiring.rows] == ["Carol"] and expiring.rows[0]["days_left"] == 3
    problems = table_with(user, "errors")
    assert [r["title"] for r in problems.rows] == ["Dyson"]
    platforms = table_with(user, "error_rate_24h")
    assert {r["domain"] for r in platforms.rows} >= {"rozetka.com.ua", "amazon.com"}


# ─── Користувачі ─────────────────────────────────────────────────

async def test_users_page_list_search_and_filters(db, user):
    await make_admin()
    alice = await make_user(1001, "Alice", "alice")
    await make_user(1002, "Bob", "bobby", tier="premium", premium_until=utc(days=-30))
    await make_user(1003, "Carol", None, blocked=True)
    await make_tracker(alice.id, title="iPhone 15")
    await make_tracker(alice.id, source="comfy.ua", title="Samsung TV", status="paused")

    await login(user)
    await user.open("/users")
    await user.should_see("3 користувачів", retries=20)
    by = {r["name"]: r for r in all_rows(user)}
    assert set(by) == {"Alice", "Bob", "Carol"}
    assert by["Alice"]["trackers"] == 2 and by["Alice"]["trackers_active"] == 1
    assert by["Bob"]["tier"] == "premium" and by["Carol"]["blocked"] is True

    user.find(kind=ui.input).type("bob")
    await user.should_see("1 користувачів", retries=20)
    assert [r["name"] for r in all_rows(user)] == ["Bob"]
    user.find(kind=ui.input).clear()
    await user.should_see("3 користувачів", retries=20)

    toggles = user.find(kind=ui.toggle).elements
    tier = next(t for t in toggles if "premium" in t.options)
    status = next(t for t in toggles if "blocked" in t.options)
    with user:
        tier.value = "free"
    await user.should_see("2 користувачів", retries=20)
    assert {r["name"] for r in all_rows(user)} == {"Alice", "Carol"}
    with user:
        status.value = "blocked"
    await user.should_see("1 користувачів", retries=20)
    assert [r["name"] for r in all_rows(user)] == ["Carol"]
    with user:
        tier.value = "all"
        status.value = "all"
    await user.should_see("3 користувачів", retries=20)


async def test_user_page_not_found(db, user):
    await make_admin()
    await login(user)
    await user.open("/users/no-such-user")
    await user.should_see("Користувача не знайдено")


async def test_user_page_premium_and_block_actions(db, user):
    await make_admin()
    alice = await make_user(1001, "Alice", "alice")
    await login(user)
    await user.open(f"/users/{alice.id}")
    await user.should_see("Alice")
    await user.should_see("@alice")
    await user.should_see("Telegram ID: 1001")
    await user.should_see("Товарів: 0")
    await user.should_see("Нічого не додано")
    await user.should_see("Сповіщень ще не було")
    await user.should_not_see("Деактивувати")

    user.find("Активувати Premium").click()
    await user.should_see("Premium активовано", retries=20)
    u = await _get(User, alice.id)
    assert u.tier == "premium"
    assert abs((u.premium_until - (utc() + timedelta(days=30))).total_seconds()) < 60

    await user.should_see("Деактивувати", retries=20)
    await user.should_see("Premium · до")
    user.find("Деактивувати").click()
    await user.should_see("Premium деактивовано", retries=20)
    await wait_until(lambda: not user._sees("Деактивувати"), what="кнопка Деактивувати зникне")
    assert (await _get(User, alice.id)).tier == "free"

    user.find("Заблокувати").click()
    await user.should_see("Заблоковано", retries=20)
    await user.should_see("Розблокувати", retries=20)
    assert (await _get(User, alice.id)).is_blocked is True
    user.find("Розблокувати").click()
    await user.should_see("Розблоковано", retries=20)
    await user.should_see("Заблокувати", retries=20)
    assert (await _get(User, alice.id)).is_blocked is False

    assert await _audit_actions() == [
        ("tester", "premium_activate", "1001"), ("tester", "premium_revoke", "1001"),
        ("tester", "user_block", "1001"), ("tester", "user_unblock", "1001"),
    ]


async def test_user_page_tracker_actions_and_history(db, user):
    await make_admin()
    alice = await make_user(1001, "Alice", "alice")
    t = await make_tracker(alice.id, title="iPhone 15", errors=2, target=35000)
    async with get_session() as s:
        s.add_all([PriceHistory(tracker_id=t.id, price=Decimal(41000), recorded_at=utc(days=2)),
                   PriceHistory(tracker_id=t.id, price=Decimal(40000), recorded_at=utc(hours=1))])
    await login(user)
    await user.open(f"/users/{alice.id}")
    await user.should_see("Товарів: 1")
    await user.should_see("1 всього")
    row = table_with(user, "errors").rows[0]
    assert row["title"] == "iPhone 15" and row["status"] == "active" and row["errors"] == 2

    user.find(kind=ui.table).trigger("pause", args=row)
    await user.should_see("Трекер на паузі", retries=20)
    await wait_until(lambda: table_with(user, "errors").rows[0]["status"] == "paused", what="статус paused")
    assert (await _get(PriceTracker, t.id)).status == "paused"

    row = table_with(user, "errors").rows[0]
    user.find(kind=ui.table).trigger("activate", args=row)
    await user.should_see("Трекер активовано", retries=20)
    await wait_until(lambda: table_with(user, "errors").rows[0]["errors"] == 0, what="лічильник помилок скинуто")
    fresh = await _get(PriceTracker, t.id)
    assert fresh.status == "active" and fresh.consecutive_errors == 0

    row = table_with(user, "errors").rows[0]
    user.find(kind=ui.table).trigger("history", args=row)
    await user.should_see("2 записів", retries=20)
    await user.should_see("Мінімум:")
    await user.should_see("Максимум:")

    assert await _audit_actions() == sorted([("tester", "tracker_paused", t.id), ("tester", "tracker_active", t.id)])


# ─── Трекери ─────────────────────────────────────────────────────

async def test_trackers_page_filters(db, user):
    await make_admin()
    alice = await make_user(1001, "Alice", "alice")
    bob = await make_user(1002, "Bob", None)
    await make_tracker(alice.id, title="iPhone 15")
    await make_tracker(alice.id, source="comfy.ua", title="Samsung TV", status="paused")
    await make_tracker(bob.id, source="allo.ua", title="Dyson", status="error", errors=4, price=None)

    await login(user)
    await user.open("/trackers")
    await user.should_see("3 трекерів")
    by = {r["title"]: r for r in all_rows(user)}
    assert set(by) == {"iPhone 15", "Samsung TV", "Dyson"}
    assert by["iPhone 15"]["user"] == "Alice @alice" and by["Dyson"]["user"] == "Bob"
    assert by["Dyson"]["price"] is None and by["iPhone 15"]["price"] == 40000.0

    toggle = user.find(kind=ui.toggle).elements.pop()
    with user:
        toggle.value = "paused"
    await user.should_see("1 трекерів", retries=20)
    assert [r["title"] for r in all_rows(user)] == ["Samsung TV"]
    with user:
        toggle.value = "all"
    await user.should_see("3 трекерів", retries=20)

    source = user.find(kind=ui.select).elements.pop()
    assert set(source.options) == {"all", "rozetka.com.ua", "comfy.ua", "allo.ua"}
    with user:
        source.value = "allo.ua"
    await user.should_see("1 трекерів", retries=20)
    assert [r["title"] for r in all_rows(user)] == ["Dyson"]
    with user:
        source.value = "all"
    await user.should_see("3 трекерів", retries=20)

    user.find(kind=ui.input).type("IPHONE")
    await user.should_see("1 трекерів", retries=20)
    assert [r["title"] for r in all_rows(user)] == ["iPhone 15"]


# ─── Адміни ──────────────────────────────────────────────────────

async def test_admins_page_list_and_create(db, user, monkeypatch):
    await make_admin()
    await make_admin("sleepy", active=False, telegram_id=9)
    await login(user)
    await user.open("/admins")
    await user.should_see("1 активних з 2")
    await user.should_see("Поки порожньо")          # журнал
    rows = table_with(user, "username").rows
    assert {(r["username"], r["active"]) for r in rows} == {("tester", True), ("sleepy", False)}
    assert next(r for r in rows if r["username"] == "sleepy")["telegram_id"] == 9

    user.find("Створити").click()
    await user.should_see("Логін: латиниця")
    input_named(user, "Логін").type("ab")
    user.find("Створити").click()
    await user.should_see("Логін: латиниця")

    input_named(user, "Логін").clear()
    input_named(user, "Логін").type("Newbie")
    input_named(user, "Пароль").type("short1")
    user.find("Створити").click()
    await user.should_see("не коротший за 10")

    input_named(user, "Пароль").clear()
    input_named(user, "Пароль").type("good-password-1")
    input_named(user, "Telegram ID для 2FA").type("abc")
    user.find("Створити").click()
    await user.should_see("Telegram ID має бути числом")

    input_named(user, "Telegram ID для 2FA").clear()
    monkeypatch.setattr(settings, "ADMIN_2FA_ENABLED", True)
    user.find("Створити").click()
    await user.should_see("2FA увімкнено: Telegram ID обов'язковий")
    monkeypatch.setattr(settings, "ADMIN_2FA_ENABLED", False)

    input_named(user, "Логін").clear()
    input_named(user, "Логін").type("TESTER")
    user.find("Створити").click()
    await user.should_see("Такий логін уже є", retries=20)

    input_named(user, "Логін").clear()
    input_named(user, "Логін").type("Newbie")
    input_named(user, "Telegram ID для 2FA").type("4242")
    user.find("Створити").click()
    await user.should_see("Адміна newbie створено", retries=20)
    await user.should_see("2 активних з 3", retries=20)
    async with get_session() as s:
        created = (await s.execute(select(Admin).where(Admin.username == "newbie"))).scalar_one()
    assert created.telegram_id == 4242 and created.is_active
    assert auth.verify_password("good-password-1", created.password_hash)
    assert await _audit_actions() == [("tester", "admin_create", "newbie")]
    assert {r["username"] for r in table_with(user, "username").rows} == {"tester", "sleepy", "newbie"}
    log = table_with(user, "action").rows
    assert log[0]["action"] == "admin_create" and log[0]["admin"] == "tester" and log[0]["details"] == "telegram_id=4242"


async def test_admins_page_activate_deactivate_rules(db, user):
    me = await make_admin()
    other = await make_admin("other", active=False)
    await login(user)
    await user.open("/admins")
    await user.should_see("1 активних з 2")
    rows = {r["username"]: r for r in table_with(user, "username").rows}

    user.find(kind=ui.table).trigger("deactivate", args=rows["tester"])
    await user.should_see("Не можна деактивувати самого себе", retries=20)
    assert (await _get(Admin, me.id)).is_active is True

    user.find(kind=ui.table).trigger("deactivate", args=rows["other"])
    await user.should_see("Має залишитись хоча б один активний адмін", retries=20)

    user.find(kind=ui.table).trigger("activate", args=rows["other"])
    await user.should_see("other активовано", retries=20)
    await user.should_see("2 активних з 2", retries=20)
    assert (await _get(Admin, other.id)).is_active is True

    rows = {r["username"]: r for r in table_with(user, "username").rows}
    user.find(kind=ui.table).trigger("deactivate", args=rows["other"])
    await user.should_see("other деактивовано", retries=20)
    await user.should_see("1 активних з 2", retries=20)
    assert (await _get(Admin, other.id)).is_active is False
    assert await _audit_actions() == [("tester", "admin_activate", "other"), ("tester", "admin_deactivate", "other")]


async def test_admins_page_telegram_and_password_dialogs(db, user):
    await make_admin()
    other = await make_admin("other", telegram_id=1)
    await login(user)
    await user.open("/admins")
    await user.should_see("2 активних з 2")

    # Telegram ID: діалог → невалідне → валідне → refresh (діалог зникає разом із content)
    row = next(r for r in table_with(user, "username").rows if r["username"] == "other")
    user.find(kind=ui.table).trigger("telegram", args=row)
    await user.should_see("Telegram ID для 2FA · other")
    with user.scope(kind=ui.dialog):
        tg = user.find(marker="admin-telegram")
        assert tg.elements.pop().value == "1"
        tg.clear()
        user.find(marker="admin-telegram").type("abc")
        user.find("Зберегти").click()
        await user.should_see("Має бути число")
        user.find(marker="admin-telegram").clear()
        user.find(marker="admin-telegram").type("777")
        user.find(marker="admin-current-password").type("wrong-pass-1")
        user.find("Зберегти").click()
        await user.should_see("Невірний поточний пароль")              # повторне підтвердження (S6)
        user.find(marker="admin-current-password").clear()
        user.find(marker="admin-current-password").type(PASSWORD)
        user.find("Зберегти").click()
    await user.should_see("Telegram ID оновлено", retries=20)
    await wait_until(lambda: not user._sees(kind=ui.dialog), what="діалог зник після refresh")
    assert (await _get(Admin, other.id)).telegram_id == 777

    # Пароль: слабкий → помилка в діалозі; сильний → збережено
    row = next(r for r in table_with(user, "username").rows if r["username"] == "other")
    user.find(kind=ui.table).trigger("password", args=row)
    await user.should_see("Новий пароль для other")
    with user.scope(kind=ui.dialog):
        user.find(marker="admin-new-password").type("weak")
        user.find("Зберегти").click()
        await user.should_see("не коротший за 10")
        user.find(marker="admin-new-password").clear()
        user.find(marker="admin-new-password").type("brand-new-pass-9")
        user.find(marker="admin-current-password").type(PASSWORD)
        user.find("Зберегти").click()
    await user.should_see("Пароль змінено", retries=20)
    fresh = await _get(Admin, other.id)
    assert auth.verify_password("brand-new-pass-9", fresh.password_hash)
    assert not auth.verify_password(PASSWORD, fresh.password_hash)
    assert await _audit_actions() == sorted([("tester", "admin_telegram_change", "other"), ("tester", "admin_password_reset", "other")])


async def test_logout_button_ends_session(db, user):
    await make_admin()
    await login(user)
    await user.open("/admins")
    await user.should_see("адміністратор")
    UserInteraction(user, {logout_button(user)}, None).click()   # кнопка без тексту — лише icon=logout
    await user.should_see("Увійдіть, щоб продовжити", retries=20)
    await user.open("/")
    await user.should_see("Увійти")
    await user.should_not_see("адміністратор")


# ─── Платформи ───────────────────────────────────────────────────

async def test_platforms_page_with_registry_and_logs(db, user):
    await make_admin()
    platform = next(p for p in registry.PLATFORMS if p.status == "verified")
    alice = await make_user(1001, "Alice", "alice")
    await make_tracker(alice.id, source=platform.domain, title="Товар")
    await make_tracker(alice.id, source="weird.example", title="Невідомий")
    async with get_session() as s:
        s.add_all([
            ScrapeLog(domain=platform.domain, ok=True, strategy="json-ld", duration_ms=100, created_at=utc(hours=1)),
            ScrapeLog(domain=platform.domain, ok=True, strategy="json-ld", healed=True, duration_ms=300, created_at=utc(hours=2)),
            ScrapeLog(domain=platform.domain, ok=False, reason="parse_failed", duration_ms=50, created_at=utc(hours=3)),
            ScrapeLog(domain=platform.domain, ok=False, reason="network", duration_ms=10, created_at=utc(hours=4)),
        ])

    await login(user)
    await user.open("/platforms")
    await user.should_see("Перевірено")
    await user.should_see("Інші сайти")
    await user.should_see("домени поза реєстром")
    await user.should_see("4 спроб парсингу")
    await user.should_see("50%")
    await user.should_see(f"{len(registry.PLATFORMS) + 1} платформ", retries=20)
    await user.should_not_see("Жодної платформи з високою часткою невдач")

    problems = table_with(user, "rate")
    assert [r["domain"] for r in problems.rows] == [platform.domain]
    assert problems.rows[0]["rate"] == 50.0 and problems.rows[0]["healed"] == 1

    main = table_with(user, "checks_30d")
    by = {r["domain"]: r for r in main.rows}
    row = by[platform.domain]
    assert row["name"] == platform.name and row["status"] == "verified"
    assert (row["trackers"], row["active"], row["users"]) == (1, 1, 1)
    assert (row["checks_30d"], row["fails_30d"], row["error_rate_30d"], row["healed_30d"]) == (4, 2, 50.0, 1)
    assert row["last_fail_reason"] == "parse_failed" and row["last_ok"] != "—"
    other = by["weird.example"]
    assert other["status"] == "other" and other["name"] == "weird.example" and other["trackers"] == 1

    toggle = user.find(kind=ui.toggle).elements.pop()
    with user:
        toggle.value = "other"
    await user.should_see("1 платформ", retries=20)
    assert [r["domain"] for r in table_with(user, "checks_30d").rows] == ["weird.example"]
    with user:
        toggle.value = "all"
    await user.should_see(f"{len(registry.PLATFORMS) + 1} платформ", retries=20)

    user.find(kind=ui.table).trigger("rowClick", args=[None, row])
    await user.should_see("Останні спроби парсингу", retries=20)
    await user.should_see("4 останніх")
    await user.should_see("Трекерів: 1 · активних 1 · з помилкою 0")
