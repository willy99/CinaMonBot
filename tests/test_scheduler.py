"""Планувальник (app.scheduler.price_checker): події, перевірка ціни, сповіщення, диспетчер, закінчення Premium."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config import settings
from app.database.models import (
    Notification, NotificationStatus, PriceEventType, PriceHistory, PriceTracker, SubscriptionTier,
    TrackerStatus, User,
)
from app.database.session import get_session
from app.scheduler import price_checker as pc
from app.services.scrapers import scraper as scraper_mod
from app.services.scrapers.scraper import ProductData, ScrapeError
from tests.helpers import make_bot

ADMIN = int(settings.ADMIN_TELEGRAM_ID)
LIMIT = settings.FREE_TIER_MAX_ITEMS


# ─── Сіди ────────────────────────────────────────────────────────

async def seed_user(telegram_id: int, lang: str = "uk", tier: str = "free", premium_until: datetime | None = None,
                    blocked: bool = False) -> User:
    if tier == "premium" and premium_until is None:
        premium_until = datetime.utcnow() + timedelta(days=30)
    async with get_session() as s:
        user = User(telegram_id=telegram_id, username="tester", first_name="Tester", language_code=lang,
                    tier=tier, premium_until=premium_until, is_blocked=blocked)
        s.add(user)
    return user


async def seed_tracker(user: User, url: str = "https://shop.example/p/1", *, title: str | None = "Товар",
                       price: str | None = "1000", currency: str = "UAH", target: str | None = None,
                       available: bool = True, status: str = TrackerStatus.ACTIVE,
                       last_checked: datetime | None = None, errors: int = 0,
                       created_at: datetime | None = None, source: str | None = None) -> PriceTracker:
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    async with get_session() as s:
        tr = PriceTracker(
            user_id=user.id, url=url, canonical_url=url, source=source or (host[4:] if host.startswith("www.") else host),
            title=title, current_price=Decimal(price) if price else None, currency=currency,
            target_price=Decimal(target) if target else None, is_available=available, status=status,
            last_checked_at=last_checked, consecutive_errors=errors,
        )
        if created_at is not None:
            tr.created_at = created_at
        s.add(tr)
    return tr


def product(price: str | None = "900", currency: str = "UAH", title: str = "Новий товар", available: bool = True,
            url: str = "https://shop.example/p/1", image: str | None = "https://img/1.jpg") -> ProductData:
    return ProductData(title=title, price=Decimal(price) if price else None, old_price=None, currency=currency,
                       is_available=available, image_url=image, canonical_url=url, strategy="json-ld")


def patch_scrape(monkeypatch, result) -> AsyncMock:
    mock = AsyncMock(return_value=result) if not callable(result) or isinstance(result, (ProductData, ScrapeError)) \
        else AsyncMock(side_effect=result)
    monkeypatch.setattr(scraper_mod.AbstractScraper, "scrape", mock)
    return mock


async def load_tracker(tracker_id: str) -> PriceTracker | None:
    async with get_session() as s:
        return await s.get(PriceTracker, tracker_id)


async def load_user(user_id: str) -> User:
    async with get_session() as s:
        return await s.get(User, user_id)


async def notifications() -> list[Notification]:
    async with get_session() as s:
        return (await s.execute(select(Notification))).scalars().all()


async def history(tracker_id: str) -> list[PriceHistory]:
    async with get_session() as s:
        return (await s.execute(select(PriceHistory).where(PriceHistory.tracker_id == tracker_id))).scalars().all()


# ─── _detect_event ───────────────────────────────────────────────

D = Decimal


@pytest.mark.parametrize("old, new, target, old_av, new_av, expected", [
    (D("100"), D("100"), None, False, True, PriceEventType.RESTOCK),       # знову в наявності
    (D("100"), D("200"), None, False, True, PriceEventType.RESTOCK),       # restock важливіший за ціну
    (None, D("100"), None, False, True, PriceEventType.RESTOCK),
    (D("100"), D("90"), D("95"), True, True, PriceEventType.TARGET_REACHED),
    (D("100"), D("95"), D("95"), True, True, PriceEventType.TARGET_REACHED),  # рівно ціль
    (D("100"), D("99"), D("95"), True, True, None),                         # ціль не досягнута, падіння 1%
    (None, D("90"), D("95"), True, True, PriceEventType.TARGET_REACHED),    # перша перевірка з ціллю
    (D("100"), D("97"), None, True, True, PriceEventType.DECREASE),         # рівно 3%
    (D("100"), D("90"), None, True, True, PriceEventType.DECREASE),
    (D("100"), D("97.01"), None, True, True, None),                         # менше 3%
    (D("100"), D("110"), None, True, True, None),                           # подорожчання
    (D("100"), D("100"), None, True, True, None),                           # без змін
    (None, D("100"), None, True, True, None),                               # перша перевірка без цілі
    (D("100"), None, None, True, True, None),                               # ціна зникла
    (D("100"), D("100"), None, False, False, None),                         # немає → немає
    (None, None, None, False, False, None),
    # Наявність не блокує цінові події: падіння на недоступному товарі все одно DECREASE (поточна поведінка)
    (D("100"), D("50"), None, False, False, PriceEventType.DECREASE),
    (D("100"), D("50"), None, True, False, PriceEventType.DECREASE),
    (D("100"), D("90"), D("0"), True, True, PriceEventType.DECREASE),       # target 0 → як без цілі
])
def test_detect_event(old, new, target, old_av, new_av, expected):
    assert pc._detect_event(old_price=old, new_price=new, target_price=target,
                            old_available=old_av, new_available=new_av) == expected


# ─── check_single_price ──────────────────────────────────────────

async def test_check_single_price_success_updates_tracker_and_history(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_tracker(user, price="1000", currency="UAH", errors=3, available=False)
    patch_scrape(monkeypatch, product(price="990", currency="usd", title="Оновлена назва", available=True))
    bot = make_bot()

    await pc.check_single_price(tr, bot)

    fresh = await load_tracker(tr.id)
    assert fresh.current_price == D("990") and fresh.currency == "USD"
    assert fresh.title == "Оновлена назва" and fresh.image_url == "https://img/1.jpg"
    assert fresh.is_available is True and fresh.consecutive_errors == 0
    assert fresh.status == TrackerStatus.ACTIVE
    assert fresh.last_checked_at is not None
    assert abs(datetime.utcnow() - fresh.last_checked_at) < timedelta(minutes=1)

    rows = await history(tr.id)
    assert len(rows) == 1 and rows[0].price == D("990") and rows[0].is_available is True
    # was unavailable → RESTOCK → сповіщення
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 1
    assert "наявності" in bot.send_message.await_args.kwargs["text"]
    notes = await notifications()
    assert len(notes) == 1 and notes[0].event_type == PriceEventType.RESTOCK and notes[0].status == NotificationStatus.SENT


async def test_check_single_price_no_event_no_notification(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_tracker(user, price="1000")
    patch_scrape(monkeypatch, product(price="1000"))
    bot = make_bot()
    await pc.check_single_price(tr, bot)
    bot.send_message.assert_not_awaited()
    assert await notifications() == []
    assert len(await history(tr.id)) == 1


async def test_check_single_price_decrease_sends_notification(db, monkeypatch):
    user = await seed_user(1, lang="en")
    tr = await seed_tracker(user, price="1000", currency="USD")
    patch_scrape(monkeypatch, product(price="900", currency="USD"))
    bot = make_bot()
    await pc.check_single_price(tr, bot)
    text = bot.send_message.await_args.kwargs["text"]
    assert "dropped by 10%" in text and "$1,000" in text and "$900" in text


async def test_check_single_price_no_price_skips_history(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_tracker(user, price="1000")
    patch_scrape(monkeypatch, product(price=None, available=False))
    await pc.check_single_price(tr, make_bot())
    fresh = await load_tracker(tr.id)
    assert fresh.current_price is None and fresh.is_available is False
    assert await history(tr.id) == []


async def test_check_single_price_keeps_currency_when_scraper_gives_none(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_tracker(user, price="1000", currency="PLN")
    patch_scrape(monkeypatch, product(price="1000", currency=""))
    await pc.check_single_price(tr, make_bot())
    assert (await load_tracker(tr.id)).currency == "PLN"


async def test_check_single_price_error_increments_counter(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_tracker(user, price="1000", errors=0)
    patch_scrape(monkeypatch, ScrapeError(url=tr.canonical_url, reason="network"))
    bot = make_bot()
    await pc.check_single_price(tr, bot)
    fresh = await load_tracker(tr.id)
    assert fresh.consecutive_errors == 1 and fresh.status == TrackerStatus.ACTIVE
    assert fresh.current_price == D("1000")                       # ціна не чіпається
    assert fresh.last_checked_at is not None
    assert await history(tr.id) == []
    bot.send_message.assert_not_awaited()


async def test_check_single_price_fifth_error_disables_tracker(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_tracker(user, errors=4)
    patch_scrape(monkeypatch, ScrapeError(url=tr.canonical_url, reason="parse_failed"))
    await pc.check_single_price(tr, make_bot())
    fresh = await load_tracker(tr.id)
    assert fresh.consecutive_errors == 5 and fresh.status == TrackerStatus.ERROR


async def test_check_single_price_success_resets_errors(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_tracker(user, errors=4)
    patch_scrape(monkeypatch, product(price="1000"))
    await pc.check_single_price(tr, make_bot())
    fresh = await load_tracker(tr.id)
    assert fresh.consecutive_errors == 0 and fresh.status == TrackerStatus.ACTIVE


async def test_check_single_price_tracker_deleted_midway(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_tracker(user)

    async def scrape_and_delete(url):
        async with get_session() as s:
            await s.delete(await s.get(PriceTracker, tr.id))
        return product(price="900")

    mock = patch_scrape(monkeypatch, scrape_and_delete)
    bot = make_bot()
    await pc.check_single_price(tr, bot)                      # без винятку
    mock.assert_awaited_once_with(tr.canonical_url)
    assert await load_tracker(tr.id) is None
    bot.send_message.assert_not_awaited()
    assert await notifications() == []


async def test_check_single_price_non_url_is_skipped(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_tracker(user, url="not a url at all", source="x")
    mock = patch_scrape(monkeypatch, product())
    await pc.check_single_price(tr, make_bot())
    mock.assert_not_awaited()
    assert (await load_tracker(tr.id)).last_checked_at is None


# ─── _send_notification ──────────────────────────────────────────

async def _notify(bot, tracker_id: str, event: PriceEventType, old, new) -> None:
    async with get_session() as s:
        tr = await s.get(PriceTracker, tracker_id)
        await pc._send_notification(bot=bot, session=s, tracker=tr, event=event, old_price=old, new_price=new)


@pytest.mark.parametrize("lang, fragments", [
    ("uk", ["З'явився в наявності", "$1 234,50", "Перейти до товару"]),
    ("en", ["Back in stock", "$1,234.50", "Open product"]),
])
async def test_notification_restock_text(db, lang, fragments):
    user = await seed_user(1, lang=lang)
    tr = await seed_tracker(user, currency="USD", title="Restocked item")
    bot = make_bot()
    await _notify(bot, tr.id, PriceEventType.RESTOCK, None, D("1234.50"))
    kw = bot.send_message.await_args.kwargs
    assert kw["chat_id"] == 1 and kw["parse_mode"] == "HTML"
    for fragment in fragments:
        assert fragment in kw["text"], fragment
    assert "Restocked item" in kw["text"] and tr.canonical_url in kw["text"]


@pytest.mark.parametrize("lang, fragments", [
    ("uk", ["досягла твоєї цілі", "Ціна зараз: <b>900 грн</b>", "Твоя ціль: 950 грн"]),
    ("en", ["reached your target", "Price now: <b>₴900</b>", "Your target: ₴950"]),
])
async def test_notification_target_text(db, lang, fragments):
    user = await seed_user(1, lang=lang)
    tr = await seed_tracker(user, currency="UAH", target="950")
    bot = make_bot()
    await _notify(bot, tr.id, PriceEventType.TARGET_REACHED, D("1000"), D("900"))
    text = bot.send_message.await_args.kwargs["text"]
    for fragment in fragments:
        assert fragment in text, fragment


@pytest.mark.parametrize("lang, fragments", [
    # 234.50 / 1234.50 = 18.99% → int() обрізає до 18 (не округлює)
    ("uk", ["знизилась на 18%", "Було: <s>€1 234,50</s>", "Стало: <b>€1 000</b>", "Економія: €234,50"]),
    ("en", ["dropped by 18%", "Was: <s>€1,234.50</s>", "Now: <b>€1,000</b>", "You save: €234.50"]),
])
async def test_notification_decrease_text(db, lang, fragments):
    user = await seed_user(1, lang=lang)
    tr = await seed_tracker(user, currency="EUR")
    bot = make_bot()
    await _notify(bot, tr.id, PriceEventType.DECREASE, D("1234.50"), D("1000"))
    text = bot.send_message.await_args.kwargs["text"]
    for fragment in fragments:
        assert fragment in text, fragment


async def test_notification_row_sent_with_message_id(db):
    user = await seed_user(1)
    tr = await seed_tracker(user)
    await _notify(make_bot(), tr.id, PriceEventType.DECREASE, D("100"), D("90"))
    notes = await notifications()
    assert len(notes) == 1
    n = notes[0]
    assert n.status == NotificationStatus.SENT and n.telegram_message_id == 777
    assert n.user_id == user.id and n.tracker_id == tr.id
    assert n.event_type == PriceEventType.DECREASE and n.old_price == D("100") and n.new_price == D("90")
    assert n.error_message is None


async def test_notification_title_fallback_and_truncation(db):
    user = await seed_user(1, lang="en")
    tr = await seed_tracker(user, title=None)
    bot = make_bot()
    await _notify(bot, tr.id, PriceEventType.DECREASE, D("100"), D("90"))
    assert "📦 Item" in bot.send_message.await_args.kwargs["text"]

    tr2 = await seed_tracker(user, url="https://shop.example/p/2", title="T" * 100)
    await _notify(bot, tr2.id, PriceEventType.DECREASE, D("100"), D("90"))
    text = bot.send_message.await_args.kwargs["text"]
    assert "T" * 60 in text and "T" * 61 not in text


async def test_notification_blocked_exception_marks_user_and_fails_row(db):
    user = await seed_user(1)
    tr = await seed_tracker(user)
    bot = make_bot()
    bot.send_message = AsyncMock(side_effect=Exception("Telegram server says - Forbidden: bot was blocked by the user"))
    await _notify(bot, tr.id, PriceEventType.DECREASE, D("100"), D("90"))
    assert (await load_user(user.id)).bot_blocked is True
    notes = await notifications()
    assert len(notes) == 1 and notes[0].status == NotificationStatus.FAILED
    assert "bot was blocked" in notes[0].error_message and notes[0].telegram_message_id is None


async def test_notification_other_exception_fails_row_but_user_not_blocked(db):
    user = await seed_user(1)
    tr = await seed_tracker(user)
    bot = make_bot()
    bot.send_message = AsyncMock(side_effect=Exception("Bad Request: chat not found"))
    await _notify(bot, tr.id, PriceEventType.DECREASE, D("100"), D("90"))
    assert (await load_user(user.id)).bot_blocked is False
    notes = await notifications()
    assert len(notes) == 1 and notes[0].status == NotificationStatus.FAILED and "chat not found" in notes[0].error_message


async def test_notification_skipped_for_blocked_user(db):
    user = await seed_user(1, blocked=True)
    tr = await seed_tracker(user)
    bot = make_bot()
    await _notify(bot, tr.id, PriceEventType.DECREASE, D("100"), D("90"))
    bot.send_message.assert_not_awaited()
    assert await notifications() == []


# ─── dispatch_price_checks ───────────────────────────────────────

@pytest.fixture
def dispatch_spies(monkeypatch):
    """Підміняємо check_single_price і _check_domain_health — перевіряємо лише вибір трекерів."""
    checked: list[PriceTracker] = []

    async def fake_check(tracker, bot):
        checked.append(tracker)

    check = AsyncMock(side_effect=fake_check)
    health = AsyncMock()
    monkeypatch.setattr(pc, "check_single_price", check)
    monkeypatch.setattr(pc, "_check_domain_health", health)
    return checked, check, health


async def test_dispatch_respects_tier_intervals(db, dispatch_spies):
    checked, _, health = dispatch_spies
    premium = await seed_user(1, tier="premium")
    free = await seed_user(2)
    ago90 = datetime.utcnow() - timedelta(minutes=90)
    ago400 = datetime.utcnow() - timedelta(minutes=400)
    t_prem = await seed_tracker(premium, "https://a.example/p/1", last_checked=ago90)    # 90 > 60 → перевіряємо
    t_free_recent = await seed_tracker(free, "https://a.example/p/2", last_checked=ago90)  # 90 < 360 → ні
    t_free_due = await seed_tracker(free, "https://a.example/p/3", last_checked=ago400)   # 400 > 360 → так
    t_paused = await seed_tracker(free, "https://a.example/p/4", status=TrackerStatus.PAUSED)
    t_error = await seed_tracker(free, "https://a.example/p/5", status=TrackerStatus.ERROR)
    bot = make_bot()

    await pc.dispatch_price_checks(bot)

    ids = {t.id for t in checked}
    assert ids == {t_prem.id, t_free_due.id}
    assert t_free_recent.id not in ids and t_paused.id not in ids and t_error.id not in ids
    health.assert_awaited_once()
    assert health.await_args.args[0] is bot
    assert health.await_args.args[1] == {"a.example": [True, True]}


async def test_dispatch_never_checked_go_first(db, dispatch_spies):
    checked, _, _ = dispatch_spies
    user = await seed_user(1, tier="premium")
    old = await seed_tracker(user, "https://a.example/p/1", last_checked=datetime.utcnow() - timedelta(hours=10))
    never = await seed_tracker(user, "https://a.example/p/2", last_checked=None)
    older = await seed_tracker(user, "https://a.example/p/3", last_checked=datetime.utcnow() - timedelta(hours=20))
    await pc.dispatch_price_checks(make_bot())
    assert [t.id for t in checked] == [never.id, older.id, old.id]


async def test_dispatch_premium_expired_date_uses_free_interval(db, dispatch_spies):
    """tier=premium, але premium_until у минулому → інтервал free."""
    checked, _, _ = dispatch_spies
    user = await seed_user(1, tier="premium", premium_until=datetime.utcnow() - timedelta(days=1))
    await seed_tracker(user, last_checked=datetime.utcnow() - timedelta(minutes=90))
    await pc.dispatch_price_checks(make_bot())
    assert checked == []


async def test_dispatch_nothing_to_check(db, dispatch_spies):
    checked, check, health = dispatch_spies
    user = await seed_user(1)
    await seed_tracker(user, last_checked=datetime.utcnow())
    await pc.dispatch_price_checks(make_bot())
    check.assert_not_awaited()
    health.assert_not_awaited()


async def test_dispatch_caps_at_max_checks_per_run(db, dispatch_spies, monkeypatch):
    checked, _, _ = dispatch_spies
    monkeypatch.setattr(pc, "MAX_CHECKS_PER_RUN", 2)
    user = await seed_user(1)
    for i in range(4):
        await seed_tracker(user, f"https://a.example/p/{i}")
    await pc.dispatch_price_checks(make_bot())
    assert len(checked) == 2


async def test_dispatch_collects_failures_per_domain_and_continues(db, monkeypatch):
    user = await seed_user(1)
    ok1 = await seed_tracker(user, "https://good.example/p/1")
    bad = await seed_tracker(user, "https://bad.example/p/1")
    ok2 = await seed_tracker(user, "https://good.example/p/2")

    async def fake_check(tracker, bot):
        if tracker.id == bad.id:
            raise RuntimeError("boom")

    monkeypatch.setattr(pc, "check_single_price", AsyncMock(side_effect=fake_check))
    health = AsyncMock()
    monkeypatch.setattr(pc, "_check_domain_health", health)

    await pc.dispatch_price_checks(make_bot())      # виняток одного трекера не зупиняє решту

    results = health.await_args.args[1]
    assert sorted(results["good.example"]) == [True, True] and results["bad.example"] == [False]


async def test_dispatch_end_to_end_with_patched_scraper(db, monkeypatch):
    """Справжній check_single_price із замоканим scrape: оновлення БД і здоров'я домену."""
    user = await seed_user(1)
    tr = await seed_tracker(user, "https://shop.example/p/1", price="1000")
    patch_scrape(monkeypatch, product(price="900"))
    health = AsyncMock()
    monkeypatch.setattr(pc, "_check_domain_health", health)
    bot = make_bot()
    await pc.dispatch_price_checks(bot)
    assert (await load_tracker(tr.id)).current_price == D("900")
    bot.send_message.assert_awaited_once()
    assert health.await_args.args[1] == {"shop.example": [True]}


# ─── _check_domain_health ────────────────────────────────────────

async def test_domain_health_no_alert_under_three_results():
    bot = make_bot()
    await pc._check_domain_health(bot, {"rozetka.com.ua": [False, False]})
    bot.send_message.assert_not_awaited()


async def test_domain_health_no_alert_under_half_failures():
    bot = make_bot()
    await pc._check_domain_health(bot, {"rozetka.com.ua": [False, True, True, True]})
    bot.send_message.assert_not_awaited()


async def test_domain_health_alert_with_platform_name():
    bot = make_bot()
    await pc._check_domain_health(bot, {"rozetka.com.ua": [False, False, True], "ok.example": [True, True, True]})
    bot.send_message.assert_awaited_once()
    kw = bot.send_message.await_args.kwargs
    assert kw["chat_id"] == ADMIN
    assert "Rozetka" in kw["text"] and "rozetka.com.ua" in kw["text"]
    assert "2/3" in kw["text"] and "67%" in kw["text"]
    assert "перевірено" in kw["text"] and "selectors" in kw["text"]


async def test_domain_health_alert_unknown_domain():
    bot = make_bot()
    await pc._check_domain_health(bot, {"www.unknown.example": [False, False, False]})
    text = bot.send_message.await_args.kwargs["text"]
    assert "unknown.example" in text and "немає в реєстрі" in text and "100%" in text


async def test_domain_health_exactly_half_alerts():
    bot = make_bot()
    await pc._check_domain_health(bot, {"x.example": [False, True, False, True]})
    bot.send_message.assert_awaited_once()


async def test_domain_health_send_exception_swallowed():
    bot = make_bot()
    bot.send_message = AsyncMock(side_effect=Exception("network"))
    await pc._check_domain_health(bot, {"x.example": [False, False, False]})   # без винятку
    bot.send_message.assert_awaited_once()


async def test_domain_health_disabled_without_admin(monkeypatch):
    monkeypatch.setattr(pc.settings, "ADMIN_TELEGRAM_ID", 0)
    bot = make_bot()
    await pc._check_domain_health(bot, {"x.example": [False, False, False]})
    bot.send_message.assert_not_awaited()


# ─── handle_expired_premiums / _pause_excess_trackers ────────────

async def _seed_trackers(user: User, n: int, prefix: str = "a") -> list[PriceTracker]:
    base = datetime.utcnow() - timedelta(days=10)
    return [
        await seed_tracker(user, f"https://{prefix}.example/p/{i}", title=f"Товар {i}", created_at=base + timedelta(hours=i))
        for i in range(n)
    ]


async def test_expired_premium_downgrades_and_pauses_newest(db):
    user = await seed_user(1, lang="uk", tier="premium", premium_until=datetime.utcnow() - timedelta(hours=1))
    trackers = await _seed_trackers(user, LIMIT + 2)
    bot = make_bot()

    await pc.handle_expired_premiums(bot)

    fresh = await load_user(user.id)
    assert fresh.tier == SubscriptionTier.FREE and fresh.premium_until is None and fresh.is_premium is False

    statuses = {tr.id: (await load_tracker(tr.id)).status for tr in trackers}
    for tr in trackers[:LIMIT]:
        assert statuses[tr.id] == TrackerStatus.ACTIVE          # найстаріші лишаються
    for tr in trackers[LIMIT:]:
        assert statuses[tr.id] == TrackerStatus.PAUSED_LIMIT          # найновіші на паузі через ліміт

    bot.send_message.assert_awaited_once()
    kw = bot.send_message.await_args.kwargs
    assert kw["chat_id"] == 1 and kw["parse_mode"] == "HTML"
    assert "Термін Premium закінчився" in kw["text"] and f"({LIMIT} товарів)" in kw["text"]
    for tr in trackers[LIMIT:]:
        assert f"⏸ {tr.title}" in kw["text"]
    for tr in trackers[:LIMIT]:
        assert f"⏸ {tr.title}" not in kw["text"]


async def test_expired_premium_notifies_in_english(db):
    user = await seed_user(1, lang="en", tier="premium", premium_until=datetime.utcnow() - timedelta(minutes=5))
    await _seed_trackers(user, LIMIT + 1)
    bot = make_bot()
    await pc.handle_expired_premiums(bot)
    text = bot.send_message.await_args.kwargs["text"]
    assert "Your Premium has expired" in text and f"({LIMIT} items)" in text


async def test_expired_premium_within_limit_not_notified(db):
    user = await seed_user(1, tier="premium", premium_until=datetime.utcnow() - timedelta(hours=1))
    trackers = await _seed_trackers(user, LIMIT)
    bot = make_bot()
    await pc.handle_expired_premiums(bot)
    assert (await load_user(user.id)).tier == SubscriptionTier.FREE
    statuses = [(await load_tracker(tr.id)).status for tr in trackers]
    assert statuses == [TrackerStatus.ACTIVE] * LIMIT
    bot.send_message.assert_not_awaited()


async def test_active_premium_and_free_users_untouched(db):
    still_premium = await seed_user(1, tier="premium", premium_until=datetime.utcnow() + timedelta(days=3))
    free = await seed_user(2)
    await _seed_trackers(still_premium, LIMIT + 2, prefix="p")
    await _seed_trackers(free, LIMIT + 2, prefix="f")
    bot = make_bot()
    await pc.handle_expired_premiums(bot)
    assert (await load_user(still_premium.id)).tier == SubscriptionTier.PREMIUM
    async with get_session() as s:
        paused = (await s.execute(select(PriceTracker).where(PriceTracker.status == TrackerStatus.PAUSED))).scalars().all()
    assert paused == []
    bot.send_message.assert_not_awaited()


async def test_expired_premium_blocked_user_paused_but_not_notified(db):
    user = await seed_user(1, tier="premium", premium_until=datetime.utcnow() - timedelta(hours=1), blocked=True)
    trackers = await _seed_trackers(user, LIMIT + 1)
    bot = make_bot()
    await pc.handle_expired_premiums(bot)
    assert (await load_tracker(trackers[-1].id)).status == TrackerStatus.PAUSED_LIMIT
    bot.send_message.assert_not_awaited()


async def test_pause_excess_ignores_already_paused_and_send_failure(db):
    user = await seed_user(1)
    trackers = await _seed_trackers(user, LIMIT + 1)
    await seed_tracker(user, "https://z.example/p/paused", status=TrackerStatus.PAUSED)
    bot = make_bot()
    bot.send_message = AsyncMock(side_effect=Exception("network"))
    await pc._pause_excess_trackers(user, bot)                    # помилка відправки не кидається
    assert (await load_tracker(trackers[-1].id)).status == TrackerStatus.PAUSED_LIMIT
    assert (await load_tracker(trackers[0].id)).status == TrackerStatus.ACTIVE
    bot.send_message.assert_awaited_once()


# ─── setup_scheduler ─────────────────────────────────────────────

def test_setup_scheduler_registers_jobs(monkeypatch):
    started: list[bool] = []
    monkeypatch.setattr(pc.scheduler, "start", lambda *a, **k: started.append(True))
    bot = make_bot()
    try:
        pc.setup_scheduler(bot)
        assert started == [True]
        jobs = {job.id: job for job in pc.scheduler.get_jobs()}
        assert set(jobs) == {"handle_expired_premiums", "dispatch_price_checks", "prune_scrape_log", "weekly_digest"}

        expired = jobs["handle_expired_premiums"]
        assert expired.func is pc.handle_expired_premiums and expired.args == (bot,)
        assert isinstance(expired.trigger, IntervalTrigger) and expired.trigger.interval == timedelta(hours=1)

        dispatch = jobs["dispatch_price_checks"]
        assert dispatch.func is pc.dispatch_price_checks and dispatch.args == (bot,)
        assert isinstance(dispatch.trigger, IntervalTrigger) and dispatch.trigger.interval == timedelta(minutes=5)
        now = datetime.now(timezone.utc)
        assert timedelta(seconds=30) < dispatch.next_run_time - now <= timedelta(minutes=1)
        assert timedelta(minutes=1, seconds=30) < expired.next_run_time - now <= timedelta(minutes=2)

        prune = jobs["prune_scrape_log"]
        assert prune.func is pc.prune_scrape_log
        assert isinstance(prune.trigger, CronTrigger) and "hour='4'" in str(prune.trigger)

        # Щотижневий дайджест — понеділок 10:00 за Києвом, із ботом в аргументах
        from app.scheduler import digest as dg
        weekly = jobs["weekly_digest"]
        assert weekly.func is dg.send_weekly_digests and weekly.args == (bot,)
        assert isinstance(weekly.trigger, CronTrigger)
        assert "day_of_week='mon'" in str(weekly.trigger) and "hour='10'" in str(weekly.trigger)
    finally:
        pc.scheduler.remove_all_jobs()



async def test_notification_escapes_shop_title_and_url(db):
    """Назва з сайту магазину й URL користувача не ламають HTML (інакше Telegram відхилить сповіщення)."""
    user = await seed_user(1)
    tr = await seed_tracker(user, title="Чохол <Samsung> AT&T", url="https://shop.example/item'><b>x")
    bot = make_bot()
    await _notify(bot, tr.id, PriceEventType.DECREASE, D("1000"), D("900"))
    text = bot.send_message.await_args.kwargs["text"]
    assert "Чохол &lt;Samsung&gt; AT&amp;T" in text and "<Samsung>" not in text
    assert "href='https://shop.example/item&#x27;&gt;&lt;b&gt;x'" in text


async def test_transient_error_pauses_domain_not_tracker(db, monkeypatch):
    """A4: антибот / мережа — пауза домену; 5-та така помилка поспіль ще не робить трекер ERROR."""
    from app.scheduler import breaker
    user = await seed_user(1)
    tr = await seed_tracker(user, errors=4)
    patch_scrape(monkeypatch, ScrapeError(url=tr.canonical_url, reason="blocked"))
    await pc.check_single_price(tr, make_bot())
    fresh = await load_tracker(tr.id)
    assert fresh.consecutive_errors == 5 and fresh.status == TrackerStatus.ACTIVE
    assert breaker.is_open(tr.source)


async def test_dispatch_skips_paused_domain_and_success_reopens(db, monkeypatch):
    from app.scheduler import breaker
    user = await seed_user(1)
    tr = await seed_tracker(user)
    mock = patch_scrape(monkeypatch, product(price="900"))
    breaker.trip(tr.source, "blocked")
    await pc.dispatch_price_checks(make_bot())
    mock.assert_not_awaited()                                   # домен на паузі — не ходимо
    breaker.reset()
    breaker.trip(tr.source, "blocked", now=0.0)                 # пауза давно минула
    await pc.dispatch_price_checks(make_bot())
    mock.assert_awaited_once()
    assert not breaker.snapshot()                               # успіх закрив запобіжник


async def test_transient_errors_eventually_disable_dead_link(db, monkeypatch):
    from app.scheduler import breaker
    user = await seed_user(1)
    tr = await seed_tracker(user, errors=breaker.TRANSIENT_ERROR_LIMIT - 1)
    patch_scrape(monkeypatch, ScrapeError(url=tr.canonical_url, reason="blocked"))
    await pc.check_single_price(tr, make_bot())
    assert (await load_tracker(tr.id)).status == TrackerStatus.ERROR


async def test_dispatch_beats_heartbeat_even_when_idle(db):
    from app.services import heartbeat
    await pc.dispatch_price_checks(make_bot())
    st = await heartbeat.status()
    assert st["seen"] and not st["stale"]
