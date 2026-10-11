"""
Регресійні тести хендлерів бота (app.bot.handlers.main) — усі команди, крім /feedback.
Кожен тест викликає хендлер напряму з фейковими Telegram-об'єктами (tests/helpers.py).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.bot.handlers import main as h
from app.config import settings
from app.database.models import (
    Notification, NotificationStatus, PriceEventType, PriceHistory, PriceTracker, SubscriptionTier,
    TrackerStatus, User,
)
from app.database.session import get_session
from app.i18n import LANGUAGES, t
from app.services.scrapers import registry
from app.services.scrapers import scraper as scraper_mod
from app.services.scrapers.registry import Platform
from app.services.scrapers.scraper import ProductData, ScrapeError
from tests.helpers import capture_bot, fsm_for, fsm_storage, make_bot, make_callback, make_message

ADMIN = int(settings.ADMIN_TELEGRAM_ID)
LIMIT = settings.FREE_TIER_MAX_ITEMS
PRICE = settings.PREMIUM_PRICE_UAH

SMALL_PLATFORMS = [
    Platform("goodshop.ua", "GoodShop", "UA", "verified", "json-ld"),
    Platform("second.ua", "Second", "UA", "verified", "microdata"),
    Platform("polshop.pl", "PolShop", "PL", "verified", "json-ld"),
    Platform("globalshop.com", "GlobalShop", "GLOBAL", "verified", "json-ld"),
    Platform("blockedshop.com", "BlockedShop", "UA", "blocked"),
    Platform("jsshop.pl", "JsShop", "PL", "needs_js"),
    Platform("expshop.de", "ExpShop", "DE", "experimental"),
]


# ─── Сіди та помічники ───────────────────────────────────────────

async def seed_user(telegram_id: int, lang: str = "uk", tier: str = "free", premium_until: datetime | None = None,
                    blocked: bool = False, first_name: str = "Tester") -> User:
    if tier == "premium" and premium_until is None:
        premium_until = datetime.utcnow() + timedelta(days=30)
    async with get_session() as s:
        user = User(telegram_id=telegram_id, username="tester", first_name=first_name, language_code=lang,
                    tier=tier, premium_until=premium_until, is_blocked=blocked)
        s.add(user)
    return user


async def seed_tracker(user: User, url: str = "https://shop.example/p/1", *, title: str | None = "Товар",
                       price: str | None = "1000", currency: str = "UAH", target: str | None = None,
                       status: str = TrackerStatus.ACTIVE, created_at: datetime | None = None) -> PriceTracker:
    host = urlparse(url).netloc.lower()
    async with get_session() as s:
        tr = PriceTracker(
            user_id=user.id, url=url, canonical_url=url, source=host[4:] if host.startswith("www.") else host,
            title=title, current_price=Decimal(price) if price else None, currency=currency,
            target_price=Decimal(target) if target else None, status=status,
        )
        if created_at is not None:
            tr.created_at = created_at
        s.add(tr)
    return tr


async def seed_many(user: User, n: int, prefix: str = "a") -> list[PriceTracker]:
    base = datetime.utcnow() - timedelta(days=1)
    return [await seed_tracker(user, f"https://{prefix}.example/p/{i}", title=f"Товар {i}",
                               created_at=base + timedelta(minutes=i)) for i in range(n)]


async def load_user(telegram_id: int) -> User | None:
    async with get_session() as s:
        return (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()


async def trackers_of(user: User) -> list[PriceTracker]:
    async with get_session() as s:
        return (await s.execute(
            select(PriceTracker).where(PriceTracker.user_id == user.id).order_by(PriceTracker.created_at)
        )).scalars().all()


async def tracker_count() -> int:
    async with get_session() as s:
        return (await s.execute(select(func.count()).select_from(PriceTracker))).scalar_one()


def product(title: str = "Widget", price: str | None = "299.99", currency: str = "USD",
            url: str = "https://shop.example/p/1", available: bool = True, strategy: str = "json-ld") -> ProductData:
    return ProductData(title=title, price=Decimal(price) if price else None, old_price=None, currency=currency,
                       is_available=available, image_url=None, canonical_url=url, strategy=strategy)


def patch_scrape(monkeypatch, result) -> AsyncMock:
    mock = AsyncMock(return_value=result)
    monkeypatch.setattr(scraper_mod.AbstractScraper, "scrape", mock)
    return mock


def admin_calls(bot) -> list:
    return [c for c in bot.send_message.await_args_list if c.kwargs.get("chat_id") == ADMIN]


@pytest.fixture(autouse=True)
def _isolate():
    fsm_storage()
    h._NEW_SITE_NOTIFIED.clear()
    yield
    h._NEW_SITE_NOTIFIED.clear()


@pytest.fixture
def small_registry(monkeypatch):
    monkeypatch.setattr(registry, "PLATFORMS", SMALL_PLATFORMS)
    monkeypatch.setattr(h, "PLATFORMS", SMALL_PLATFORMS)
    return SMALL_PLATFORMS


async def run_add_url(uid: int, url: str, lang: str = "uk", bot=None) -> list:
    """cmd_add + process_url; повертає список відправлених повідомлень."""
    with capture_bot() as sent:
        await h.cmd_add(make_message(uid, "/add", lang, bot=bot), fsm_for(uid))
        await h.process_url(make_message(uid, url, lang, bot=bot), fsm_for(uid))
    return sent


# ─── get_or_create_user ──────────────────────────────────────────

@pytest.mark.parametrize("tg_lang, stored, resolved", [
    ("uk", "uk", "uk"), ("en", "en", "en"), ("de", "de", "en"), ("ru", "ru", "uk"), ("en-GB", "en-GB", "en"),
    (None, "uk", "uk"),
])
async def test_start_creates_user_with_language(db, tg_lang, stored, resolved):
    with capture_bot():
        await h.cmd_start(make_message(1, "/start", tg_lang))
    user = await load_user(1)
    assert user is not None
    assert user.language_code == stored and user.lang == resolved
    assert user.username == "tester" and user.first_name == "Tester"
    assert user.tier == SubscriptionTier.FREE and user.is_premium is False and user.is_blocked is False


async def test_get_or_create_user_is_idempotent(db):
    msg = make_message(1, "/start", "en")
    first = await h.get_or_create_user(msg.from_user)
    second = await h.get_or_create_user(make_message(1, "/start", "de").from_user)
    assert first.id == second.id and second.language_code == "en"     # мову не перезаписує
    async with get_session() as s:
        assert (await s.execute(select(func.count()).select_from(User))).scalar_one() == 1


async def test_get_or_create_user_race_loads_existing(db, monkeypatch):
    """IntegrityError при flush (інший запит уже створив юзера) → rollback і завантаження наявного."""
    real_get_session = h.get_session
    flushes: list[int] = []

    @asynccontextmanager
    async def racing_session():
        async with real_get_session() as session:
            async def flush(*a, **k):
                flushes.append(1)
                async with real_get_session() as other:          # «паралельний» запит встиг першим
                    other.add(User(telegram_id=1, username="winner", first_name="W", language_code="en"))
                raise IntegrityError("INSERT INTO users", {}, Exception("UNIQUE constraint failed"))
            session.flush = flush
            yield session

    monkeypatch.setattr(h, "get_session", racing_session)
    user = await h.get_or_create_user(make_message(1, "/start", "uk").from_user)
    monkeypatch.undo()

    assert flushes == [1]
    assert user.username == "winner" and user.language_code == "en"
    async with get_session() as s:
        rows = (await s.execute(select(User).where(User.telegram_id == 1))).scalars().all()
    assert len(rows) == 1 and rows[0].username == "winner"


# ─── /start ──────────────────────────────────────────────────────

async def test_start_free_user_uk(db):
    with capture_bot() as sent:
        await h.cmd_start(make_message(1, "/start", "uk"))
    assert len(sent) == 1
    msg = sent[0]
    assert "Привіт, Tester" in msg.text
    assert f"до {LIMIT} товарів" in msg.text and f"Premium {PRICE} грн" in msg.text
    assert "Premium — насолоджуйся" not in msg.text
    assert msg.buttons == [t("uk", "btn_add"), t("uk", "btn_list"), t("uk", "btn_delete"),
                           t("uk", "btn_buy_premium"), t("uk", "btn_language")]
    assert msg.callbacks == ["add_tracker", "my_list", "delete_menu", "premium_info", "lang_menu"]


async def test_start_free_user_en(db):
    with capture_bot() as sent:
        await h.cmd_start(make_message(1, "/start", "en"))
    msg = sent[0]
    assert "Hi, Tester" in msg.text and f"up to {LIMIT} items" in msg.text
    assert "🌐 Language / Мова" in msg.buttons and "⭐ Get Premium" in msg.buttons


async def test_start_premium_user_has_no_buy_button(db):
    await seed_user(1, lang="en", tier="premium")
    with capture_bot() as sent:
        await h.cmd_start(make_message(1, "/start", "en"))
    msg = sent[0]
    assert "Your Premium is active" in msg.text
    assert f"up to {LIMIT} items" not in msg.text
    assert "premium_info" not in msg.callbacks and "lang_menu" in msg.callbacks


async def test_start_premium_expired_date_shows_plans(db):
    await seed_user(1, tier="premium", premium_until=datetime.utcnow() - timedelta(days=1))
    with capture_bot() as sent:
        await h.cmd_start(make_message(1, "/start"))
    assert f"до {LIMIT} товарів" in sent[0].text and "premium_info" in sent[0].callbacks


async def test_start_without_first_name_uses_default(db):
    msg = make_message(1, "/start", "uk")
    msg = msg.model_copy(update={"from_user": msg.from_user.model_copy(update={"first_name": ""})})
    with capture_bot() as sent:
        await h.cmd_start(msg)
    assert "Привіт, друже" in sent[0].text


async def test_start_blocked_user_still_gets_menu(db):
    """/start не перевіряє is_blocked — блок діє лише на /list (поточна поведінка)."""
    await seed_user(1, blocked=True)
    with capture_bot() as sent:
        await h.cmd_start(make_message(1, "/start"))
    assert "Привіт" in sent[0].text


# ─── /language ───────────────────────────────────────────────────

async def test_language_menu_message_and_callback(db):
    with capture_bot() as sent:
        await h.cmd_language(make_message(1, "/language", "en"))
    assert "Choose a language" in sent[0].text
    assert sent[0].callbacks == [f"set_lang:{code}" for code in LANGUAGES]
    assert sent[0].buttons == list(LANGUAGES.values())

    with capture_bot() as sent:
        await h.cmd_language(make_callback(1, "lang_menu", "en"))
    assert sent[0].kind == "answer" and "Choose a language" in sent[0].text
    assert sent[-1].kind == "callback"


@pytest.mark.parametrize("initial, code, confirmation", [
    ("uk", "en", "Language: English"),
    ("en", "uk", "Мова: Українська"),
    ("uk", "uk", "Мова: Українська"),
])
async def test_set_language_persists_and_confirms(db, initial, code, confirmation):
    await seed_user(1, lang=initial)
    with capture_bot() as sent:
        await h.set_language(make_callback(1, f"set_lang:{code}", initial))
    assert (await load_user(1)).language_code == code
    edit = [s for s in sent if s.kind == "edit"][0]
    assert confirmation in edit.text
    assert t(code, "btn_add") in edit.buttons and t(code, "btn_buy_premium") in edit.buttons
    assert sent[-1].kind == "callback"


async def test_set_language_premium_menu(db):
    await seed_user(1, lang="uk", tier="premium")
    with capture_bot() as sent:
        await h.set_language(make_callback(1, "set_lang:en", "uk"))
    edit = [s for s in sent if s.kind == "edit"][0]
    assert "premium_info" not in edit.callbacks


async def test_set_language_unknown_code_ignored(db):
    await seed_user(1, lang="uk")
    with capture_bot() as sent:
        await h.set_language(make_callback(1, "set_lang:xx", "uk"))
    assert (await load_user(1)).language_code == "uk"
    assert [s.kind for s in sent] == ["callback"]


async def test_set_language_unknown_user_ignored(db):
    with capture_bot() as sent:
        await h.set_language(make_callback(1, "set_lang:en", "uk"))
    assert await load_user(1) is None
    assert [s.kind for s in sent] == ["callback"]


# ─── /help, /info, /sites, /cancel ───────────────────────────────

@pytest.mark.parametrize("lang, fragment", [("uk", "<b>Команди:</b>"), ("en", "<b>Commands:</b>")])
async def test_help(db, lang, fragment):
    with capture_bot() as sent:
        await h.cmd_help(make_message(1, "/help", lang))
    assert fragment in sent[0].text and "/add" in sent[0].text and "/feedback" in sent[0].text


@pytest.mark.parametrize("lang, fragments", [
    ("uk", ["моніторинг цін", f"до {LIMIT} товарів", f"кожні {settings.FREE_TIER_CHECK_INTERVAL_MINUTES // 60} годин",
            f"Premium — {PRICE} грн", "Контакти"]),
    ("en", ["price tracking", f"up to {LIMIT} items", f"every {settings.FREE_TIER_CHECK_INTERVAL_MINUTES // 60} hours",
            f"Premium — {PRICE} UAH", "Contacts"]),
])
async def test_info(db, lang, fragments):
    with capture_bot() as sent:
        await h.cmd_info(make_message(1, "/info", lang))
    for fragment in fragments:
        assert fragment in sent[0].text, fragment
    assert "{" not in sent[0].text                           # усі плейсхолдери підставлені


async def test_sites_groups_by_country_ua_first_global_last(db, small_registry):
    with capture_bot() as sent:
        await h.cmd_sites(make_message(1, "/sites", "uk"))
    text = sent[0].text
    lines = text.split("\n")
    assert lines[0].startswith("🛒")
    assert "🇺🇦 Україна: GoodShop, Second" in lines
    assert "🇵🇱 Польща: PolShop" in lines
    assert "🌍 Міжнародні: GlobalShop" in lines
    assert lines.index("🇺🇦 Україна: GoodShop, Second") < lines.index("🇵🇱 Польща: PolShop") < lines.index("🌍 Міжнародні: GlobalShop")
    assert "ExpShop" not in text                              # experimental не показуємо як перевірений
    assert "BlockedShop, JsShop" in text                      # не працюють — окремим рядком
    assert "Будь-який інший магазин" in text


async def test_sites_en(db, small_registry):
    with capture_bot() as sent:
        await h.cmd_sites(make_message(1, "/sites", "en"))
    text = sent[0].text
    assert "Verified shops" in text and "🇺🇦 Ukraine: GoodShop, Second" in text and "🌍 International: GlobalShop" in text
    assert "Not working yet" in text and "BlockedShop, JsShop" in text


def test_sites_text_many_broken_and_empty(monkeypatch):
    many = [Platform(f"b{i}.com", f"B{i}", "UA", "blocked") for i in range(11)]
    monkeypatch.setattr(registry, "PLATFORMS", many)
    monkeypatch.setattr(h, "PLATFORMS", many)
    text = h.sites_text("uk")
    assert "—" in text.split("\n")                            # жодного перевіреного
    assert "Деякі сайти поки що не працюють" in text and "B0" not in text

    monkeypatch.setattr(registry, "PLATFORMS", [])
    monkeypatch.setattr(h, "PLATFORMS", [])
    text = h.sites_text("en")
    assert "Not working yet" not in text and "⛔" not in text


def test_sites_text_real_registry_mentions_specific_scrapers():
    text = h.sites_text("uk")
    assert "Rozetka" in text and "OLX" in text and "Prom.ua" in text and "{" not in text


async def test_cancel_inside_state(db):
    state = fsm_for(1)
    await state.set_state(h.AddTrackerStates.waiting_for_url)
    await state.update_data(url="x")
    with capture_bot() as sent:
        await h.cmd_cancel(make_message(1, "/cancel", "en"), state)
    assert await state.get_state() is None and await state.get_data() == {}
    assert [s.text for s in sent] == ["❌ Cancelled."]


async def test_cancel_outside_state_is_silent(db):
    with capture_bot() as sent:
        await h.cmd_cancel(make_message(1, "/cancel"), fsm_for(1))
    assert sent == []


async def test_cancel_callback(db):
    state = fsm_for(1)
    await state.set_state(h.AddTrackerStates.waiting_for_target_price)
    with capture_bot() as sent:
        await h.cmd_cancel(make_callback(1, "cancel_add", "uk"), state)
    assert await state.get_state() is None
    assert [s.kind for s in sent] == ["answer", "callback"] and "Скасовано" in sent[0].text


# ─── /add: cmd_add + process_url ─────────────────────────────────

@pytest.mark.parametrize("lang, fragment", [("uk", "Надішли посилання"), ("en", "Send a product link")])
async def test_cmd_add_prompts_and_sets_state(db, lang, fragment):
    state = fsm_for(1)
    with capture_bot() as sent:
        await h.cmd_add(make_message(1, "/add", lang), state)
    assert fragment in sent[0].text and sent[0].callbacks == ["cancel_add"]
    assert await state.get_state() == h.AddTrackerStates.waiting_for_url.state


async def test_cmd_add_via_callback(db):
    state = fsm_for(1)
    with capture_bot() as sent:
        await h.cmd_add(make_callback(1, "add_tracker", "en"), state)
    assert [s.kind for s in sent] == ["answer", "callback"]
    assert await state.get_state() == h.AddTrackerStates.waiting_for_url.state


@pytest.mark.parametrize("text", ["hello", "rozetka.com.ua/p1", "ftp://x.com/a", "http://nodot/a", ""])
async def test_process_url_rejects_non_urls(db, monkeypatch, text):
    scrape = patch_scrape(monkeypatch, product())
    state = fsm_for(1)
    await state.set_state(h.AddTrackerStates.waiting_for_url)
    with capture_bot() as sent:
        await h.process_url(make_message(1, text or " ", "en"), state)
    assert len(sent) == 1 and "doesn't look like a link" in sent[0].text
    assert await state.get_state() == h.AddTrackerStates.waiting_for_url.state
    scrape.assert_not_awaited()


async def test_process_url_empty_text_ignored(db, monkeypatch):
    scrape = patch_scrape(monkeypatch, product())
    with capture_bot() as sent:
        await h.process_url(make_message(1, None, "en"), fsm_for(1))
    assert sent == [] and not scrape.await_count


@pytest.mark.parametrize("url, lang, fragments", [
    ("https://blockedshop.com/p/1", "uk", ["<b>BlockedShop</b>", "блокує автоматичні запити"]),
    ("https://www.blockedshop.com/p/1", "en", ["<b>BlockedShop</b>", "blocks automated requests"]),
    ("https://jsshop.pl/p/1", "uk", ["<b>JsShop</b>", "підвантажує ціну скриптами"]),
    ("https://m.jsshop.pl/p/1", "en", ["<b>JsShop</b>", "isn't supported yet"]),
])
async def test_process_url_known_broken_platform_short_circuits(db, monkeypatch, small_registry, url, lang, fragments):
    scrape = patch_scrape(monkeypatch, product())
    sent = await run_add_url(1, url, lang)
    assert len(sent) == 2 and sent[-1].kind == "answer"
    for fragment in fragments:
        assert fragment in sent[-1].text, fragment
    scrape.assert_not_awaited()
    assert await fsm_for(1).get_state() == h.AddTrackerStates.waiting_for_url.state


@pytest.mark.parametrize("reason, lang, fragment", [
    ("blocked", "uk", "блокує автоматичні запити"),
    ("blocked", "en", "blocks automated requests"),
    ("not_found", "uk", "404"),
    ("not_found", "en", "Page not found (404)"),
    ("parse_failed", "uk", "Не знайшов ціну"),
    ("parse_failed", "en", "couldn't find a price"),
    ("network", "uk", "мережа або таймаут"),
    ("network", "en", "network error or timeout"),
    ("max_retries_exceeded", "en", "network error or timeout"),
    ("timeout", "uk", "мережа або таймаут"),
])
async def test_process_url_scrape_errors(db, monkeypatch, reason, lang, fragment):
    url = "https://shop.example/p/1"
    scrape = patch_scrape(monkeypatch, ScrapeError(url=url, reason=reason))
    sent = await run_add_url(1, url, lang)
    scrape.assert_awaited_once_with(url)
    checking = sent[1]
    assert checking.kind == "answer" and checking.text == t(lang, "checking")
    assert sent[-1].kind == "edit" and fragment in sent[-1].text
    assert await fsm_for(1).get_state() == h.AddTrackerStates.waiting_for_url.state


@pytest.mark.parametrize("lang, price_text, prompt", [
    ("uk", "$299,99", "Введи суму в USD"),
    ("en", "$299.99", "Enter an amount in USD"),
])
async def test_process_url_success(db, monkeypatch, lang, price_text, prompt):
    url = "https://shop.example/p/1?utm_source=tg"
    scrape = patch_scrape(monkeypatch, product(currency="usd"))
    sent = await run_add_url(1, url, lang)
    scrape.assert_awaited_once_with(url)
    final = sent[-1]
    assert final.kind == "edit"
    assert "<b>Widget</b>" in final.text and price_text in final.text and prompt in final.text
    assert t(lang, "in_stock") in final.text
    assert final.callbacks == ["skip_target"]

    state = fsm_for(1)
    assert await state.get_state() == h.AddTrackerStates.waiting_for_target_price.state
    data = await state.get_data()
    assert data["currency"] == "USD" and data["price"] == "299.99" and data["title"] == "Widget"
    assert data["url"] == url and data["canonical_url"] == "https://shop.example/p/1"
    assert data["source"] == "shop.example"


async def test_process_url_success_without_price_and_unavailable(db, monkeypatch):
    patch_scrape(monkeypatch, product(price=None, currency="", available=False))
    sent = await run_add_url(1, "https://shop.example/p/1", "en")
    assert "unknown" in sent[-1].text and "out of stock" in sent[-1].text and "in UAH" in sent[-1].text
    data = await fsm_for(1).get_data()
    assert data["price"] is None and data["currency"] == "UAH"


async def test_process_url_specific_scraper_domain_as_source(db, monkeypatch):
    patch_scrape(monkeypatch, product(url="https://rozetka.com.ua/ua/p1/"))
    await run_add_url(1, "https://www.rozetka.com.ua/ua/p1/?x=1", "uk")
    assert (await fsm_for(1).get_data())["source"] == "rozetka.com.ua"


# ─── Повідомлення адміну про новий сайт ──────────────────────────

async def test_new_site_notice_once_per_domain(db, monkeypatch, small_registry):
    patch_scrape(monkeypatch, product(url="https://newshop.example/p/1"))
    bot = make_bot()
    await run_add_url(1, "https://newshop.example/p/1", bot=bot)
    await run_add_url(2, "https://www.newshop.example/p/2", bot=bot)
    calls = admin_calls(bot)
    assert len(calls) == 1
    assert "🆕" in calls[0].kwargs["text"] and "newshop.example" in calls[0].kwargs["text"]
    assert "strategy=json-ld" in calls[0].kwargs["text"] and "новий" in calls[0].kwargs["text"]
    assert "newshop.example" in h._NEW_SITE_NOTIFIED


async def test_new_site_notice_for_experimental_platform(db, monkeypatch, small_registry):
    patch_scrape(monkeypatch, product(url="https://expshop.de/p/1"))
    bot = make_bot()
    await run_add_url(1, "https://expshop.de/p/1", bot=bot)
    calls = admin_calls(bot)
    assert len(calls) == 1 and "експериментальний" in calls[0].kwargs["text"]


async def test_no_new_site_notice_for_verified_platform(db, monkeypatch, small_registry):
    patch_scrape(monkeypatch, product(url="https://goodshop.ua/p/1"))
    bot = make_bot()
    await run_add_url(1, "https://goodshop.ua/p/1", bot=bot)
    assert admin_calls(bot) == [] and h._NEW_SITE_NOTIFIED == set()


async def test_no_new_site_notice_on_scrape_error(db, monkeypatch, small_registry):
    patch_scrape(monkeypatch, ScrapeError(url="https://newshop.example/p/1", reason="network"))
    bot = make_bot()
    await run_add_url(1, "https://newshop.example/p/1", bot=bot)
    assert admin_calls(bot) == []


async def test_new_site_notice_send_failure_is_swallowed(db, monkeypatch, small_registry):
    patch_scrape(monkeypatch, product(url="https://newshop.example/p/1"))
    bot = make_bot()
    bot.send_message = AsyncMock(side_effect=Exception("network"))
    sent = await run_add_url(1, "https://newshop.example/p/1", bot=bot)
    assert sent[-1].kind == "edit" and "<b>Widget</b>" in sent[-1].text


# ─── process_target / skip_target / _save_tracker ────────────────

async def prime_add(uid: int, monkeypatch, lang: str = "uk", **overrides) -> None:
    patch_scrape(monkeypatch, product(**overrides))
    await run_add_url(uid, overrides.get("url", "https://shop.example/p/1"), lang)


@pytest.mark.parametrize("text", ["abc", "-5", "0", "", "12abc", "1e", "∞"])
async def test_process_target_invalid(db, monkeypatch, text):
    await prime_add(1, monkeypatch, "en")
    state = fsm_for(1)
    with capture_bot() as sent:
        await h.process_target(make_message(1, text or " ", "en"), state)
    assert len(sent) == 1 and "Enter a number" in sent[0].text
    assert await state.get_state() == h.AddTrackerStates.waiting_for_target_price.state
    assert await tracker_count() == 0


@pytest.mark.parametrize("text, expected", [
    ("250", Decimal("250")), ("249.99", Decimal("249.99")), ("249,99", Decimal("249.99")),
    ("1 250,50", Decimal("1250.50")), ("  300  ", Decimal("300")),
])
async def test_process_target_valid_saves_tracker(db, monkeypatch, text, expected):
    await prime_add(1, monkeypatch, "en")
    state = fsm_for(1)
    with capture_bot() as sent:
        await h.process_target(make_message(1, text, "en"), state)
    assert await state.get_state() is None and await state.get_data() == {}
    user = await load_user(1)
    (tr,) = await trackers_of(user)
    assert tr.target_price == expected and tr.currency == "USD" and tr.current_price == Decimal("299.99")
    assert tr.title == "Widget" and tr.status == TrackerStatus.ACTIVE
    assert tr.url == "https://shop.example/p/1" and tr.canonical_url == "https://shop.example/p/1"
    assert tr.source == "shop.example"
    assert "<b>Added!</b>" in sent[-1].text and "Widget" in sent[-1].text and "🎯" in sent[-1].text


async def test_process_target_text_formats_target_per_language(db, monkeypatch):
    await prime_add(1, monkeypatch, "uk")
    with capture_bot() as sent:
        await h.process_target(make_message(1, "1234.5", "uk"), fsm_for(1))
    assert "🎯 Ціль: $1 234,50" in sent[-1].text and f"Слотів використано: 1/{LIMIT}" in sent[-1].text

    await prime_add(2, monkeypatch, "en")
    with capture_bot() as sent:
        await h.process_target(make_message(2, "1234.5", "en"), fsm_for(2))
    assert "🎯 Target: $1,234.50" in sent[-1].text and f"Slots used: 1/{LIMIT}" in sent[-1].text


async def test_skip_target_saves_without_target(db, monkeypatch):
    await prime_add(1, monkeypatch, "en")
    state = fsm_for(1)
    with capture_bot() as sent:
        await h.skip_target(make_callback(1, "skip_target", "en"), state)
    assert await state.get_state() is None
    (tr,) = await trackers_of(await load_user(1))
    assert tr.target_price is None and tr.current_price == Decimal("299.99")
    added = [s for s in sent if s.kind == "answer"][-1]
    assert "<b>Added!</b>" in added.text and "🎯" not in added.text
    assert sent[-1].kind == "callback"


async def test_save_tracker_duplicate_canonical_url(db, monkeypatch):
    user = await seed_user(1, lang="en")
    await seed_tracker(user, "https://shop.example/p/1")
    await prime_add(1, monkeypatch, "en")
    state = fsm_for(1)
    with capture_bot() as sent:
        await h.process_target(make_message(1, "100", "en"), state)
    assert "already tracking" in sent[-1].text
    assert await tracker_count() == 1
    assert await state.get_state() is None                       # стан усе одно очищено


async def test_save_tracker_duplicate_uk(db, monkeypatch):
    user = await seed_user(1, lang="uk")
    await seed_tracker(user, "https://shop.example/p/1")
    await prime_add(1, monkeypatch, "uk")
    with capture_bot() as sent:
        await h.skip_target(make_callback(1, "skip_target", "uk"), fsm_for(1))
    assert any("вже відстежується" in s.text for s in sent)


async def test_save_tracker_same_url_different_users_allowed(db, monkeypatch):
    other = await seed_user(2)
    await seed_tracker(other, "https://shop.example/p/1")
    await prime_add(1, monkeypatch, "uk")
    with capture_bot():
        await h.skip_target(make_callback(1, "skip_target", "uk"), fsm_for(1))
    assert await tracker_count() == 2


@pytest.mark.parametrize("existing, lang, fragment", [
    (0, "uk", "Слотів використано: 1/{n}"),
    (3, "uk", "Слотів використано: 4/{n}"),
    (4, "en", "Slots used: {n}/{n}"),
    (5, "uk", "на паузі (ліміт {n} активних)"),
    (5, "en", "paused (limit of {n} active items)"),
    (7, "en", "paused (limit of {n} active items)"),
])
async def test_save_tracker_free_tier_slot_texts(db, monkeypatch, existing, lang, fragment):
    user = await seed_user(1, lang=lang)
    await seed_many(user, existing)
    await prime_add(1, monkeypatch, lang)
    with capture_bot() as sent:
        await h.process_target(make_message(1, "100", lang), fsm_for(1))
    assert fragment.format(n=LIMIT) in sent[-1].text
    assert await tracker_count() == existing + 1
    # Понад ліміт новий (найновіший) товар одразу PAUSED_LIMIT і не перевіряється; старіші лишаються ACTIVE
    trackers = await trackers_of(user)
    expected = [TrackerStatus.ACTIVE] * min(existing + 1, LIMIT) + [TrackerStatus.PAUSED_LIMIT] * max(0, existing + 1 - LIMIT)
    assert [tr.status for tr in trackers] == expected


async def test_save_tracker_error_trackers_do_not_count(db, monkeypatch):
    user = await seed_user(1, lang="en")
    await seed_many(user, LIMIT)
    for tr in await trackers_of(user):
        async with get_session() as s:
            (await s.get(PriceTracker, tr.id)).status = TrackerStatus.ERROR
    await prime_add(1, monkeypatch, "en")
    with capture_bot() as sent:
        await h.process_target(make_message(1, "100", "en"), fsm_for(1))
    assert f"Slots used: 1/{LIMIT}" in sent[-1].text


async def test_save_tracker_premium_has_no_slot_text(db, monkeypatch):
    user = await seed_user(1, lang="en", tier="premium")
    await seed_many(user, LIMIT + 3)
    await prime_add(1, monkeypatch, "en")
    with capture_bot() as sent:
        await h.process_target(make_message(1, "100", "en"), fsm_for(1))
    assert "Slots used" not in sent[-1].text and "paused" not in sent[-1].text and "<b>Added!</b>" in sent[-1].text


async def test_save_tracker_title_truncated_to_60(db, monkeypatch):
    await prime_add(1, monkeypatch, "en", title="T" * 100)
    with capture_bot() as sent:
        await h.skip_target(make_callback(1, "skip_target", "en"), fsm_for(1))
    added = [s for s in sent if s.kind == "answer"][-1]
    assert "T" * 60 in added.text and "T" * 61 not in added.text
    (tr,) = await trackers_of(await load_user(1))
    assert tr.title == "T" * 100


# ─── /list ───────────────────────────────────────────────────────

@pytest.mark.parametrize("lang, fragment", [("uk", "ще нічого не відстежуєш"), ("en", "not tracking anything")])
async def test_list_empty(db, lang, fragment):
    with capture_bot() as sent:
        await h.cmd_list_msg(make_message(1, "/list", lang))
    assert fragment in sent[0].text and sent[0].callbacks == ["add_tracker"]


async def test_list_callback(db):
    with capture_bot() as sent:
        await h.cmd_list_cb(make_callback(1, "my_list", "en"))
    assert [s.kind for s in sent] == ["answer", "callback"]


async def test_list_blocked_user(db):
    await seed_user(1, lang="en", blocked=True)
    await seed_tracker(await load_user(1))
    with capture_bot() as sent:
        await h.cmd_list_msg(make_message(1, "/list", "en"))
    assert len(sent) == 1 and "account is blocked" in sent[0].text and sent[0].reply_markup is None


async def test_list_free_user_within_limit(db):
    user = await seed_user(1, lang="uk")
    await seed_tracker(user, "https://a.example/p/1", title="Перший", price="1234.5", target="1000",
                       created_at=datetime.utcnow() - timedelta(hours=2))
    await seed_tracker(user, "https://a.example/p/2", title=None, price=None, currency="USD",
                       created_at=datetime.utcnow() - timedelta(hours=1))
    await seed_tracker(user, "https://a.example/p/3", title="Помилковий", status=TrackerStatus.ERROR)
    with capture_bot() as sent:
        await h.cmd_list_msg(make_message(1, "/list", "uk"))
    text = sent[0].text
    assert "Твої товари (2)" in text
    assert "1. ✅ <b>Перший</b>" in text and "💰 1 234,50 грн → 🎯1 000 грн" in text
    assert "2. ✅ <b>Без назви</b>" in text and "💰 —" in text
    assert "Помилковий" not in text
    assert "⏳" not in text and "на паузі" not in text
    assert "Ваш план: Безкоштовний" in text and "Активно: 2/2 товарів" in text
    assert "Деякі товари призупинені" not in text
    assert sent[0].callbacks == ["add_tracker", "delete_menu", "settings_menu", "premium_info"]


async def test_list_free_user_beyond_limit_marks_paused(db):
    """/list сам перераховує ліміт: у Free-юзера з 7 активними два найновіші йдуть на паузу."""
    user = await seed_user(1, lang="en")
    trackers = await seed_many(user, LIMIT + 2)
    with capture_bot() as sent:
        await h.cmd_list_msg(make_message(1, "/list", "en"))
    text = sent[0].text
    statuses = [tr.status for tr in await trackers_of(user)]
    assert statuses == [TrackerStatus.ACTIVE] * LIMIT + [TrackerStatus.PAUSED_LIMIT] * 2
    assert f"Your items ({LIMIT + 2})" in text
    for i in range(LIMIT):
        assert f"{i + 1}. ✅ <b>{trackers[i].title}</b>" in text
    assert f"{LIMIT + 1}. ⏳ <b>{trackers[LIMIT].title}</b>" in text
    assert text.count("(paused: limit)") == 2
    assert f"Active: {LIMIT}/{LIMIT + 2} items" in text and "Some items are paused because of the limit" in text
    assert "premium_info" in sent[0].callbacks


async def test_list_premium_user(db):
    user = await seed_user(1, lang="en", tier="premium")
    trackers = await seed_many(user, LIMIT + 2)
    with capture_bot() as sent:
        await h.cmd_list_msg(make_message(1, "/list", "en"))
    text = sent[0].text
    assert "⏳" not in text and "paused" not in text
    assert f"{LIMIT + 2}. ✅ <b>{trackers[-1].title}</b>" in text
    assert "Your plan: Premium (unlimited)" in text
    assert sent[0].callbacks == ["add_tracker", "delete_menu", "settings_menu"]


async def test_list_manually_paused_tracker_shown_after_active(db):
    user = await seed_user(1, lang="en")
    await seed_tracker(user, "https://a.example/p/1", title="Paused one", status=TrackerStatus.PAUSED,
                       created_at=datetime.utcnow() - timedelta(hours=5))
    await seed_tracker(user, "https://a.example/p/2", title="Active one", created_at=datetime.utcnow() - timedelta(hours=1))
    with capture_bot() as sent:
        await h.cmd_list_msg(make_message(1, "/list", "en"))
    text = sent[0].text
    assert "1. ✅ <b>Active one</b>" in text and "2. ⏸ <b>Paused one</b>" in text


async def test_list_truncates_after_40(db):
    user = await seed_user(1, lang="uk", tier="premium")
    trackers = await seed_many(user, 42)
    with capture_bot() as sent:
        await h.cmd_list_msg(make_message(1, "/list", "uk"))
    text = sent[0].text
    assert "Твої товари (42)" in text
    assert f"40. ✅ <b>{trackers[39].title}</b>" in text and "41. " not in text
    assert "та ще 2 товарів не помістилося" in text


async def test_list_title_truncated_to_40(db):
    user = await seed_user(1)
    await seed_tracker(user, title="Д" * 50)
    with capture_bot() as sent:
        await h.cmd_list_msg(make_message(1, "/list"))
    assert "Д" * 40 in sent[0].text and "Д" * 41 not in sent[0].text


# ─── /delete ─────────────────────────────────────────────────────

@pytest.mark.parametrize("lang, fragment", [("uk", "Немає товарів для видалення"), ("en", "Nothing to delete")])
async def test_delete_menu_empty(db, lang, fragment):
    with capture_bot() as sent:
        await h.cmd_delete_msg(make_message(1, "/delete", lang))
    assert fragment in sent[0].text and sent[0].reply_markup is None


async def test_delete_menu_lists_active_and_paused_not_error(db):
    """У меню видалення є активні й на паузі (щоб звільняти слоти), але не помилкові."""
    user = await seed_user(1, lang="en")
    a = await seed_tracker(user, "https://a.example/p/1", title="Alpha", price="1500", currency="UAH",
                           created_at=datetime.utcnow() - timedelta(hours=2))
    b = await seed_tracker(user, "https://a.example/p/2", title=None, price=None,
                           created_at=datetime.utcnow() - timedelta(hours=1))
    c = await seed_tracker(user, "https://a.example/p/3", title="Paused", status=TrackerStatus.PAUSED_LIMIT)
    await seed_tracker(user, "https://a.example/p/4", title="Broken", status=TrackerStatus.ERROR)
    with capture_bot() as sent:
        await h.cmd_delete_msg(make_message(1, "/delete", "en"))
    menu = sent[0]
    assert "Choose an item to delete" in menu.text
    assert menu.buttons == ["🗑 Alpha · ₴1,500", "🗑 Untitled", "🗑 Paused · ₴1,000", "◀️ Back"]   # активні, потім пауза
    assert menu.callbacks == [f"del:{a.id}", f"del:{b.id}", f"del:{c.id}", "my_list"]


async def test_delete_menu_callback(db):
    with capture_bot() as sent:
        await h.cmd_delete_cb(make_callback(1, "delete_menu", "uk"))
    assert [s.kind for s in sent] == ["answer", "callback"]


async def test_confirm_delete_own_tracker(db):
    user = await seed_user(1, lang="en")
    tr = await seed_tracker(user, title="Alpha")
    with capture_bot() as sent:
        await h.confirm_delete(make_callback(1, f"del:{tr.id}", "en"))
    edit = [s for s in sent if s.kind == "edit"][0]
    assert "<b>Delete?</b>" in edit.text and "Alpha" in edit.text
    assert edit.callbacks == [f"delok:{tr.id}", "delete_menu"]
    assert sent[-1].kind == "callback" and sent[-1].text == ""


async def test_confirm_delete_foreign_tracker_denied(db):
    owner = await seed_user(2)
    tr = await seed_tracker(owner)
    with capture_bot() as sent:
        await h.confirm_delete(make_callback(1, f"del:{tr.id}", "en"))
    assert [s.kind for s in sent] == ["callback"] and "No access" in sent[0].text


async def test_confirm_delete_unknown_tracker(db):
    with capture_bot() as sent:
        await h.confirm_delete(make_callback(1, "del:nope", "uk"))
    assert [s.kind for s in sent] == ["callback"] and "не знайдено" in sent[0].text


async def test_do_delete_removes_row_and_counts(db):
    user = await seed_user(1, lang="en")
    trackers = await seed_many(user, 3)
    await seed_tracker(user, "https://z.example/p/err", status=TrackerStatus.ERROR)
    with capture_bot() as sent:
        await h.do_delete(make_callback(1, f"delok:{trackers[0].id}", "en"))
    remaining = await trackers_of(user)
    assert {tr.id for tr in remaining} == {trackers[1].id, trackers[2].id} | {
        tr.id for tr in remaining if tr.status == TrackerStatus.ERROR}
    edit = [s for s in sent if s.kind == "edit"][0]
    assert "<b>Deleted!</b>" in edit.text and trackers[0].title in edit.text and "Remaining: 2 items" in edit.text
    assert edit.callbacks == ["my_list", "add_tracker"]
    assert sent[-1].kind == "callback" and "Deleted ✅" in sent[-1].text


async def test_do_delete_uk_text(db):
    user = await seed_user(1)
    tr = await seed_tracker(user, title=None)
    with capture_bot() as sent:
        await h.do_delete(make_callback(1, f"delok:{tr.id}", "uk"))
    edit = [s for s in sent if s.kind == "edit"][0]
    assert "<b>Видалено!</b>" in edit.text and "📦 Товар" in edit.text and "Залишилось: 0 товарів" in edit.text
    assert await trackers_of(user) == []


async def test_do_delete_foreign_or_missing_is_error(db):
    owner = await seed_user(2)
    tr = await seed_tracker(owner)
    with capture_bot() as sent:
        await h.do_delete(make_callback(1, f"delok:{tr.id}", "en"))
    assert [s.kind for s in sent] == ["callback"] and sent[0].text == "Error"
    assert await tracker_count() == 1
    with capture_bot() as sent:
        await h.do_delete(make_callback(3, "delok:nope", "uk"))
    assert sent[0].text == "Помилка"


async def _seed_history_and_notification(user: User, tr: PriceTracker) -> None:
    async with get_session() as s:
        s.add(PriceHistory(tracker_id=tr.id, price=Decimal("1000")))
        s.add(Notification(user_id=user.id, tracker_id=tr.id, event_type=PriceEventType.DECREASE,
                           new_price=Decimal("900"), status=NotificationStatus.SENT))


async def test_do_delete_cascades_price_history(db):
    """PriceHistory прив'язана ORM-relationship з cascade — видаляється разом із трекером."""
    user = await seed_user(1)
    tr = await seed_tracker(user)
    await _seed_history_and_notification(user, tr)
    with capture_bot():
        await h.do_delete(make_callback(1, f"delok:{tr.id}", "uk"))
    async with get_session() as s:
        assert (await s.execute(select(PriceHistory))).scalars().all() == []


async def test_do_delete_cascades_notifications(db):
    user = await seed_user(1)
    tr = await seed_tracker(user)
    await _seed_history_and_notification(user, tr)
    with capture_bot():
        await h.do_delete(make_callback(1, f"delok:{tr.id}", "uk"))
    async with get_session() as s:
        assert (await s.execute(select(Notification))).scalars().all() == []


# ─── /premium, pay_premium ───────────────────────────────────────

@pytest.mark.parametrize("lang, fragments", [
    ("uk", [f"<b>Premium</b> — {PRICE} грн/місяць", "Необмежена кількість товарів"]),
    ("en", [f"<b>Premium</b> — {PRICE} UAH/month", "Unlimited items"]),
])
async def test_premium_offer_for_free_user(db, lang, fragments):
    with capture_bot() as sent:
        await h.cmd_premium(make_message(1, "/premium", lang))
    for fragment in fragments:
        assert fragment in sent[0].text, fragment
    assert sent[0].callbacks == ["pay_premium"]
    assert str(PRICE) in sent[0].buttons[0]


async def test_premium_active_for_premium_user(db):
    until = datetime.utcnow() + timedelta(days=12)
    await seed_user(1, lang="en", tier="premium", premium_until=until)
    with capture_bot() as sent:
        await h.cmd_premium(make_message(1, "/premium", "en"))
    assert "Your Premium is active" in sent[0].text and until.strftime("%d.%m.%Y") in sent[0].text
    assert sent[0].buttons == ["🔄 Renew Premium"] and sent[0].callbacks == ["pay_premium"]


async def test_premium_via_callback(db):
    with capture_bot() as sent:
        await h.cmd_premium(make_callback(1, "premium_info", "uk"))
    assert [s.kind for s in sent] == ["answer", "callback"] and "грн/місяць" in sent[0].text


@pytest.mark.parametrize("lang, fragments", [
    ("uk", [f"Оплата Premium — {PRICE} грн", "<code>PREM-1</code>", "/feedback"]),
    ("en", [f"Premium payment — {PRICE} UAH", "<code>PREM-1</code>", "/feedback"]),
])
async def test_pay_premium_instructions_and_admin_notice(db, lang, fragments):
    bot = make_bot()
    with capture_bot() as sent:
        await h.cmd_pay_premium(make_callback(1, "pay_premium", lang, bot=bot))
    answer = [s for s in sent if s.kind == "answer"][0]
    for fragment in fragments:
        assert fragment in answer.text, fragment
    assert settings.CARD_NUMBER in answer.text
    assert sent[-1].kind == "callback"

    calls = admin_calls(bot)
    assert len(calls) == 1
    text = calls[0].kwargs["text"]
    assert "Хтось хоче Premium" in text and "PREM-1" in text and "/activate 1" in text and f"[{lang}]" in text
    assert "@tester" in text


async def test_pay_premium_admin_notice_failure_is_swallowed(db):
    bot = make_bot()
    bot.send_message = AsyncMock(side_effect=Exception("network"))
    with capture_bot() as sent:
        await h.cmd_pay_premium(make_callback(1, "pay_premium", "uk", bot=bot))
    assert any("PREM-1" in s.text for s in sent)


async def test_pay_premium_for_premium_user_is_alert(db):
    await seed_user(1, lang="en", tier="premium")
    bot = make_bot()
    with capture_bot() as sent:
        await h.cmd_pay_premium(make_callback(1, "pay_premium", "en", bot=bot))
    assert [s.kind for s in sent] == ["callback"] and "already have Premium" in sent[0].text
    assert admin_calls(bot) == []


# ─── /activate (адмін) ───────────────────────────────────────────

async def test_activate_ignored_for_non_admin(db):
    await seed_user(5)
    with capture_bot() as sent:
        await h.cmd_activate(make_message(1, "/activate 5"))
    assert sent == [] and (await load_user(5)).tier == SubscriptionTier.FREE


async def test_activate_usage_without_args(db):
    with capture_bot() as sent:
        await h.cmd_activate(make_message(ADMIN, "/activate"))
    assert "Використання" in sent[0].text and "/activate TELEGRAM_ID" in sent[0].text


@pytest.mark.parametrize("text", ["/activate abc", "/activate 5 many"])
async def test_activate_bad_numbers(db, text):
    await seed_user(5)
    with capture_bot() as sent:
        await h.cmd_activate(make_message(ADMIN, text))
    assert "Невірний формат" in sent[0].text and (await load_user(5)).tier == SubscriptionTier.FREE


async def test_activate_unknown_user(db):
    with capture_bot() as sent:
        await h.cmd_activate(make_message(ADMIN, "/activate 999"))
    assert "999" in sent[0].text and "не знайдений" in sent[0].text


@pytest.mark.parametrize("args, days, lang, fragment", [
    ("", 30, "en", "Premium activated!"),
    (" 60", 60, "uk", "Premium активовано!"),
    (" 7", 7, "de", "Premium activated!"),
])
async def test_activate_success(db, args, days, lang, fragment):
    await seed_user(5, lang=lang, first_name="Олена")
    bot = make_bot()
    with capture_bot() as sent:
        await h.cmd_activate(make_message(ADMIN, f"/activate 5{args}", bot=bot))

    user = await load_user(5)
    assert user.tier == SubscriptionTier.PREMIUM and user.is_premium is True
    expected_until = datetime.utcnow() + timedelta(days=days)
    assert abs(user.premium_until - expected_until) < timedelta(minutes=1)
    until_str = user.premium_until.strftime("%d.%m.%Y")

    assert "Premium активовано" in sent[0].text and "Олена" in sent[0].text and "(ID: 5)" in sent[0].text
    assert f"(+{days} днів)" in sent[0].text and until_str in sent[0].text

    bot.send_message.assert_awaited_once()
    kw = bot.send_message.await_args.kwargs
    assert kw["chat_id"] == 5 and fragment in kw["text"] and until_str in kw["text"]


async def test_activate_extends_already_premium(db):
    """Повторна активація продовжує чинний Premium: +30 днів до поточної дати закінчення."""
    await seed_user(5, tier="premium", premium_until=datetime.utcnow() + timedelta(days=100))
    with capture_bot() as sent:
        await h.cmd_activate(make_message(ADMIN, "/activate 5 30"))
    user = await load_user(5)
    assert abs(user.premium_until - (datetime.utcnow() + timedelta(days=130))) < timedelta(minutes=1)
    assert "Premium продовжено" in sent[0].text


async def test_activate_unpauses_limit_trackers(db):
    """Premium знімає ліміт: товари PAUSED_LIMIT вмикаються, адмін бачить скільки."""
    user = await seed_user(5, lang="en")
    await seed_many(user, LIMIT + 3)
    with capture_bot():
        await h.cmd_list_msg(make_message(5, "/list", "en"))          # перерахунок: 3 на паузі
    assert sum(tr.status == TrackerStatus.PAUSED_LIMIT for tr in await trackers_of(user)) == 3
    with capture_bot() as sent:
        await h.cmd_activate(make_message(ADMIN, "/activate 5"))
    assert all(tr.status == TrackerStatus.ACTIVE for tr in await trackers_of(user))
    assert "Знято з паузи товарів: 3" in sent[0].text


async def test_delete_active_frees_slot_for_paused(db):
    """Видалення активного товару вмикає найстаріший з паузи і каже про це юзеру."""
    user = await seed_user(1, lang="uk")
    trackers = await seed_many(user, LIMIT + 2)
    await h.sync_free_limit(user.id)
    with capture_bot() as sent:
        await h.do_delete(make_callback(1, f"delok:{trackers[0].id}", "uk"))
    statuses = {tr.id: tr.status for tr in await trackers_of(user)}
    assert statuses[trackers[LIMIT].id] == TrackerStatus.ACTIVE          # найстаріший із паузи
    assert statuses[trackers[LIMIT + 1].id] == TrackerStatus.PAUSED_LIMIT
    edit = [x for x in sent if x.kind == "edit"][0]
    assert "знято з паузи" in edit.text and trackers[LIMIT].title in edit.text


async def test_activate_user_notify_failure_reported_to_admin(db):
    await seed_user(5)
    bot = make_bot()
    bot.send_message = AsyncMock(side_effect=Exception("Forbidden: bot was blocked by the user"))
    with capture_bot() as sent:
        await h.cmd_activate(make_message(ADMIN, "/activate 5", bot=bot))
    assert (await load_user(5)).tier == SubscriptionTier.PREMIUM
    assert "Premium активовано" in sent[0].text
    assert "Не вдалось повідомити юзера" in sent[1].text and "bot was blocked" in sent[1].text


# ─── /broadcast (адмін) ──────────────────────────────────────────

async def test_broadcast_ignored_for_non_admin(db):
    await seed_user(2)
    bot = make_bot()
    with capture_bot() as sent:
        await h.cmd_broadcast(make_message(1, "/broadcast hi", bot=bot))
    assert sent == [] and bot.send_message.await_count == 0


@pytest.mark.parametrize("text", ["/broadcast", "/broadcast   "])
async def test_broadcast_usage_without_text(db, text):
    bot = make_bot()
    with capture_bot() as sent:
        await h.cmd_broadcast(make_message(ADMIN, text, bot=bot))
    assert len(sent) == 1 and "Використання" in sent[0].text and bot.send_message.await_count == 0


async def test_broadcast_sends_to_non_blocked_users(db):
    await seed_user(1, lang="uk")
    await seed_user(2, lang="en")
    await seed_user(3, blocked=True)
    bot = make_bot()
    with capture_bot() as sent:
        await h.cmd_broadcast(make_message(ADMIN, "/broadcast <b>Привіт</b> усім", bot=bot))
    assert "Надсилаю 2 юзерам" in sent[0].text
    assert "Надіслано: 2" in sent[-1].text and "Не вдалось: 0" in sent[-1].text
    chats = sorted(c.kwargs["chat_id"] for c in bot.send_message.await_args_list)
    assert chats == [1, 2]
    for c in bot.send_message.await_args_list:
        assert c.kwargs["text"] == "<b>Привіт</b> усім" and c.kwargs["parse_mode"] == "HTML"


async def test_broadcast_marks_blocked_and_counts_failures(db):
    await seed_user(1)
    await seed_user(2)
    await seed_user(3)
    bot = make_bot()

    async def send(chat_id, text, **kw):
        if chat_id == 2:
            raise Exception("Telegram server says - Forbidden: bot was blocked by the user")
        if chat_id == 3:
            raise Exception("Bad Request: chat not found")
        return None

    bot.send_message = AsyncMock(side_effect=send)
    with capture_bot() as sent:
        await h.cmd_broadcast(make_message(ADMIN, "/broadcast hi", bot=bot))
    assert "Надіслано: 1" in sent[-1].text and "Не вдалось: 2" in sent[-1].text
    assert (await load_user(2)).is_blocked is True
    assert (await load_user(3)).is_blocked is False           # інша помилка — не блок
    assert (await load_user(1)).is_blocked is False


async def test_broadcast_with_no_users(db):
    bot = make_bot()
    with capture_bot() as sent:
        await h.cmd_broadcast(make_message(ADMIN, "/broadcast hi", bot=bot))
    assert "Надсилаю 0 юзерам" in sent[0].text and "Надіслано: 0" in sent[-1].text


# ─── /stats (адмін) ──────────────────────────────────────────────

async def test_stats_ignored_for_non_admin(db):
    with capture_bot() as sent:
        await h.cmd_stats(make_message(1, "/stats"))
    assert sent == []


async def test_stats_empty_db(db):
    with capture_bot() as sent:
        await h.cmd_stats(make_message(ADMIN, "/stats"))
    text = sent[0].text
    assert "Статистика" in text and "Всього: 0" in text and "Мови: —" in text and "немає" in text and "🟢" in text


async def test_stats_reflects_seeded_data(db):
    u1 = await seed_user(1, lang="uk")
    u2 = await seed_user(2, lang="uk", tier="premium")
    u3 = await seed_user(3, lang="en", blocked=True)
    await seed_tracker(u1, "https://rozetka.com.ua/ua/p1/")
    await seed_tracker(u2, "https://www.rozetka.com.ua/ua/p2/")
    await seed_tracker(u2, "https://prom.ua/p3.html")
    await seed_tracker(u3, "https://olx.ua/p4", status=TrackerStatus.ERROR)
    await seed_tracker(u3, "https://olx.ua/p5", status=TrackerStatus.PAUSED)
    async with get_session() as s:
        trackers = (await s.execute(select(PriceTracker))).scalars().all()
        s.add(PriceHistory(tracker_id=trackers[0].id, price=Decimal("1")))
        s.add(PriceHistory(tracker_id=trackers[0].id, price=Decimal("2"),
                           recorded_at=datetime.utcnow() - timedelta(days=2)))        # старіше 24 год
        s.add(Notification(user_id=u1.id, tracker_id=trackers[0].id, event_type=PriceEventType.DECREASE,
                           new_price=Decimal("1"), status=NotificationStatus.SENT))
        s.add(Notification(user_id=u1.id, tracker_id=trackers[0].id, event_type=PriceEventType.DECREASE,
                           new_price=Decimal("1"), status=NotificationStatus.FAILED))

    with capture_bot() as sent:
        await h.cmd_stats(make_message(ADMIN, "/stats"))
    text = sent[0].text
    assert "Всього: 3\n" in text and "Активні: 2\n" in text and "Premium: 1" in text
    assert "Мови: uk: 2, en: 1" in text
    assert "Всього: 5\n" in text and "Активні: 3\n" in text and "З помилками: 1" in text and "🟡" in text
    assert "Перевірок цін: 1" in text and "Сповіщень відправлено: 1" in text
    assert "• rozetka.com.ua: 2" in text and "• prom.ua: 1" in text and "olx.ua" not in text
    assert text.index("rozetka.com.ua: 2") < text.index("prom.ua: 1")


async def test_stats_health_bar_red(db):
    user = await seed_user(1)
    await seed_tracker(user, "https://a.example/p/1", status=TrackerStatus.ERROR)
    await seed_tracker(user, "https://a.example/p/2")
    with capture_bot() as sent:
        await h.cmd_stats(make_message(ADMIN, "/stats"))
    assert "🔴" in sent[0].text
