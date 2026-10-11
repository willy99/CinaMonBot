"""
Спільні помічники для тестів адмін-дашборду (test_admin_auth / pages / media_guard).

- make_admin(): адмін у БД з відомим паролем (PASSWORD)
- login(user, ...): повний вхід через сторінку /login (2FA вимкнено у conftest)
- make_user() / make_tracker(): фабрики сутностей бота для сідів
- reset_auth_state(): чистить глобальні лічильники спроб і pending-коди 2FA,
  які живуть у модулі app.admin.auth і переживають тести
- wait_until(): чекає на асинхронне перемальовування (reload таблиць тощо)
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from nicegui import ui
from nicegui.testing.user_interaction import UserInteraction

from app.admin import auth
from app.database.models import Admin, PriceTracker, User
from app.database.session import get_session

PASSWORD = "correct-horse-7"


def utc(**delta) -> datetime:
    """Naive UTC «зараз мінус delta» — як у колонках DateTime без tz. utc(days=-3) → через 3 дні."""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(**delta)


def reset_auth_state() -> None:
    auth._attempts.clear()
    auth._pending.clear()


async def make_admin(username: str = "tester", password: str = PASSWORD,
                     telegram_id: int | None = None, active: bool = True) -> Admin:
    async with get_session() as s:
        a = Admin(username=username.lower(), password_hash=auth.hash_password(password),
                  telegram_id=telegram_id, is_active=active)
        s.add(a)
        await s.flush()
        return a


async def make_user(telegram_id: int, first_name: str | None = "Alice", username: str | None = None, *,
                    lang: str = "uk", tier: str = "free", premium_until: datetime | None = None,
                    blocked: bool = False, created: datetime | None = None) -> User:
    async with get_session() as s:
        u = User(telegram_id=telegram_id, first_name=first_name, username=username, language_code=lang,
                 tier=tier, premium_until=premium_until, is_blocked=blocked)
        if created is not None:
            u.created_at = created
        s.add(u)
        await s.flush()
        return u


async def make_tracker(user_id: str, *, source: str = "rozetka.com.ua", title: str | None = "iPhone 15",
                       status: str = "active", price: float | None = 40000, currency: str = "UAH",
                       errors: int = 0, url: str | None = None, target: float | None = None,
                       last_checked: datetime | None = None, created: datetime | None = None,
                       available: bool = True) -> PriceTracker:
    url = url or f"https://{source}/p/{(title or 'x').lower().replace(' ', '-')}"
    async with get_session() as s:
        t = PriceTracker(
            user_id=user_id, url=url, canonical_url=url, source=source, title=title, status=status,
            current_price=Decimal(str(price)) if price is not None else None, currency=currency,
            consecutive_errors=errors, target_price=Decimal(str(target)) if target is not None else None,
            last_checked_at=last_checked, is_available=available,
        )
        if created is not None:
            t.created_at = created
        s.add(t)
        await s.flush()
        return t


async def login(user, username: str = "tester", password: str = PASSWORD) -> None:
    """Вхід через форму. Після успіху чекає на каркас сторінки (підпис «адміністратор» у сайдбарі),
    бо куди саме перекине — залежить від referrer_path у сесії."""
    await user.open("/login")
    user.find("Логін").type(username)
    user.find("Пароль").type(password)
    user.find("Увійти").click()
    await user.should_see("адміністратор", retries=40)


async def wait_until(predicate: Callable[[], bool], timeout: float = 3.0, what: str = "умова") -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"не дочекались: {what}")
        await asyncio.sleep(0.05)


def tables(user) -> list[ui.table]:
    return sorted(user.find(kind=ui.table).elements, key=lambda t: t.id)


def all_rows(user) -> list[dict]:
    """Рядки всіх таблиць на сторінці (ui.table не індексується should_see, тому дивимось у .rows)."""
    return [row for t in tables(user) for row in t.rows]


def table_with(user, key: str) -> ui.table:
    """Таблиця, рядки якої мають поле key (на сторінці може бути кілька таблиць)."""
    for t in tables(user):
        if t.rows and key in t.rows[0]:
            return t
    raise AssertionError(f"немає таблиці з полем {key!r}")


def input_named(user, label: str) -> UserInteraction:
    """ui.input за підписом (label). На відміну від user.find(label) не чіпляє ui.label з тим самим текстом
    (наприклад, повідомлення про помилку «Логін: ...»)."""
    found = {e for e in user.find(kind=ui.input).elements if e.props.get("label") == label}
    if not found:
        raise AssertionError(f"немає поля вводу з підписом {label!r}")
    return UserInteraction(user, found, label)


def logout_button(user) -> ui.button:
    for b in user.find(kind=ui.button).elements:
        if b.props.get("icon") == "logout":
            return b
    raise AssertionError("кнопки виходу немає на сторінці")
