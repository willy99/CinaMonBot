"""
Запобіжники масштабування перевірок:
- одне посилання — один запит за прогін, хоч його відстежують кілька юзерів (результат — кожному);
- межа запитів на хвилину до одного магазину: надлишок — на наступні хвилини, найдавніші першими (без сплесків);
- випадковий розкид пауз між запитами до одного магазину.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from app.config import settings
from app.database.models import NotificationStatus
from app.scheduler import price_checker as pc
from app.services import runtime_settings as rt
from app.services.scrapers import scraper as scraper_mod
from tests.helpers import make_bot
from tests.test_scheduler import (  # noqa: F401
    dispatch_spies, load_tracker, notifications, patch_scrape, product, seed_tracker, seed_user,
)


# ─── Одне посилання — один запит за прогін ───────────────────────

async def test_same_url_for_several_users_is_fetched_once_per_run(db, monkeypatch):
    url = "https://shop.example/p/1"
    a, b = await seed_user(1), await seed_user(2)
    ta = await seed_tracker(a, url, price="1000")
    tb = await seed_tracker(b, url, price="1000")
    scrape = patch_scrape(monkeypatch, product(price="900"))
    monkeypatch.setattr(pc, "_update_domain_blocks", AsyncMock())
    bot = make_bot()
    await pc.dispatch_price_checks(bot)
    assert scrape.await_count == 1
    assert (await load_tracker(ta.id)).current_price == (await load_tracker(tb.id)).current_price == D("900")
    assert sorted(c.kwargs["chat_id"] for c in bot.send_message.await_args_list) == [1, 2]   # кожному — своє


async def test_direct_check_outside_dispatch_is_not_cached(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_tracker(user, price="1000")
    scrape = patch_scrape(monkeypatch, product(price="900"))
    await pc.check_single_price(tr, make_bot())
    await pc.check_single_price(await load_tracker(tr.id), make_bot())
    assert scrape.await_count == 2


# ─── Межа запитів на хвилину до магазину ─────────────────────────

def _cap(monkeypatch, n: int) -> None:
    monkeypatch.setattr(settings, "DOMAIN_MAX_CHECKS_PER_MINUTE", n)
    rt.reset_cache()


async def test_per_shop_cap_takes_oldest_and_leaves_rest_for_next_minutes(db, dispatch_spies, monkeypatch):
    checked, _, _ = dispatch_spies
    _cap(monkeypatch, 3)
    user = await seed_user(1, tier="premium")
    base = datetime.utcnow() - timedelta(hours=5)
    shop = [await seed_tracker(user, f"https://shop.example/p/{i}", last_checked=base + timedelta(minutes=i))
            for i in range(5)]
    other = await seed_tracker(user, "https://other.example/p/1", last_checked=base + timedelta(minutes=10))
    await pc.dispatch_price_checks(make_bot())
    ids = {t.id for t in checked}
    assert ids == {shop[0].id, shop[1].id, shop[2].id, other.id}           # 3 найдавніші + інший магазин


async def test_duplicate_urls_do_not_consume_the_cap(db, dispatch_spies, monkeypatch):
    checked, _, _ = dispatch_spies
    _cap(monkeypatch, 2)
    users = [await seed_user(i) for i in (1, 2, 3)]
    same = [await seed_tracker(u, "https://shop.example/p/1") for u in users]
    second = await seed_tracker(users[0], "https://shop.example/p/2")
    third = await seed_tracker(users[0], "https://shop.example/p/3")
    await pc.dispatch_price_checks(make_bot())
    ids = {t.id for t in checked}
    assert {t.id for t in same} | {second.id} <= ids and third.id not in ids   # 2 унікальні посилання


async def test_cap_queue_goes_by_due_time_so_premium_is_not_stuck_behind_free(db, dispatch_spies, monkeypatch):
    """Premium (раз на годину) прострочений на 30 хв, free (раз на 6 год) — на 10: під межею першим іде Premium."""
    checked, _, _ = dispatch_spies
    _cap(monkeypatch, 1)
    now = datetime.utcnow()
    free = await seed_tracker(await seed_user(1), "https://shop.example/p/1", last_checked=now - timedelta(minutes=370))
    prem = await seed_tracker(await seed_user(2, tier="premium"), "https://shop.example/p/2",
                              last_checked=now - timedelta(minutes=90))
    await pc.dispatch_price_checks(make_bot())
    assert [t.id for t in checked] == [prem.id]
    assert free.id not in {t.id for t in checked}


async def test_cap_override_per_shop(db, dispatch_spies, monkeypatch):
    checked, _, _ = dispatch_spies
    _cap(monkeypatch, 1)
    monkeypatch.setattr(settings, "DOMAIN_CAP_OVERRIDES", "shop.example=3, broken, other.example=x")
    user = await seed_user(1, tier="premium")
    for i in range(5):
        await seed_tracker(user, f"https://shop.example/p/{i}")
        await seed_tracker(user, f"https://other.example/p/{i}")
    await pc.dispatch_price_checks(make_bot())
    by_shop = [t.source for t in checked]
    assert by_shop.count("shop.example") == 3 and by_shop.count("other.example") == 1   # криві записи — default


async def test_same_url_trackers_past_half_interval_join_the_check(db, dispatch_spies, monkeypatch):
    checked, _, _ = dispatch_spies
    now = datetime.utcnow()
    url = "https://shop.example/p/1"
    due = await seed_tracker(await seed_user(1), url, last_checked=now - timedelta(minutes=400))
    half = await seed_tracker(await seed_user(2), url, last_checked=now - timedelta(minutes=200))     # 200 ≥ 180
    fresh = await seed_tracker(await seed_user(3), url, last_checked=now - timedelta(minutes=60))
    other = await seed_tracker(await seed_user(4), "https://shop.example/p/2", last_checked=now - timedelta(minutes=200))
    await pc.dispatch_price_checks(make_bot())
    ids = {t.id for t in checked}
    assert ids == {due.id, half.id} and fresh.id not in ids and other.id not in ids


async def test_joined_trackers_respect_max_checks_per_run(db, dispatch_spies, monkeypatch):
    checked, _, _ = dispatch_spies
    monkeypatch.setattr(pc, "MAX_CHECKS_PER_RUN", 2)
    now = datetime.utcnow()
    for i in range(4):
        await seed_tracker(await seed_user(i + 1), "https://shop.example/p/1",
                           last_checked=None if i == 0 else now - timedelta(minutes=300))
    await pc.dispatch_price_checks(make_bot())
    assert len(checked) == 2


async def test_slow_shop_does_not_hold_up_other_shops(db, monkeypatch):
    """Rozetka чекає паузу / відступ після 429 — інші магазини тим часом перевіряються (а не стоять за 5 слотами)."""
    import asyncio
    monkeypatch.setattr(pc, "_update_domain_blocks", AsyncMock())
    user = await seed_user(1, tier="premium")
    for i in range(6):
        await seed_tracker(user, f"https://slow.example/p/{i}")
    await seed_tracker(user, "https://fast.example/p/1")
    fast_done = asyncio.Event()
    order: list[str] = []

    async def fake_check(tracker, bot):
        order.append(tracker.source)
        if tracker.source == "slow.example":
            await fast_done.wait()            # «повільний» магазин звільниться лише після швидкого
        else:
            fast_done.set()
    monkeypatch.setattr(pc, "check_single_price", fake_check)
    await asyncio.wait_for(pc.dispatch_price_checks(make_bot()), timeout=2)
    assert order.count("slow.example") == 6 and "fast.example" in order


async def test_shop_paused_mid_run_skips_its_remaining_pages(db, monkeypatch):
    from app.scheduler import breaker
    breaker.reset()
    monkeypatch.setattr(pc, "_update_domain_blocks", AsyncMock())
    user = await seed_user(1, tier="premium")
    for i in range(4):
        await seed_tracker(user, f"https://limited.example/p/{i}")
    other = await seed_tracker(user, "https://other.example/p/1")
    seen: list[str] = []

    async def fake_check(tracker, bot):
        seen.append(tracker.id)
        if tracker.source == "limited.example":
            breaker.trip("limited.example", "blocked")            # перша ж сторінка — 429 після повторів
    monkeypatch.setattr(pc, "check_single_price", fake_check)
    try:
        await pc.dispatch_price_checks(make_bot())
    finally:
        breaker.reset()
    assert len(seen) == 2 and other.id in seen                        # 1 сторінка магазину + інший магазин


# ─── Telegram: темп і RetryAfter ─────────────────────────────────

def _retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=SendMessage(chat_id=1, text="x"), message="Too Many Requests", retry_after=seconds)


async def test_retry_after_waits_and_resends(db, monkeypatch):
    sleeps: list[float] = []
    real_sleep = pc.asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await real_sleep(0)
    monkeypatch.setattr(pc.asyncio, "sleep", fake_sleep)
    tr = await seed_tracker(await seed_user(1), price="1000")
    patch_scrape(monkeypatch, product(price="900"))
    bot = make_bot()
    bot.send_message.side_effect = [_retry_after(3), MagicMock(message_id=5)]
    await pc.check_single_price(tr, bot)
    assert bot.send_message.await_count == 2 and 3 in sleeps
    assert [n.status for n in await notifications()] == [NotificationStatus.SENT]


async def test_long_retry_after_is_not_waited_out(db, monkeypatch):
    tr = await seed_tracker(await seed_user(1), price="1000")
    patch_scrape(monkeypatch, product(price="900"))
    bot = make_bot()
    bot.send_message.side_effect = _retry_after(pc.TELEGRAM_MAX_RETRY_WAIT_SECONDS + 1)
    await pc.check_single_price(tr, bot)
    assert bot.send_message.await_count == 1
    assert [n.status for n in await notifications()] == [NotificationStatus.FAILED]
    assert (await load_tracker(tr.id)).current_price == D("900")                       # ціна однаково збережена


async def test_price_is_committed_before_telegram_call(db, monkeypatch):
    """Поки йде запит у Telegram, нова ціна вже в базі — запис не тримає блокування SQLite."""
    tr = await seed_tracker(await seed_user(1), price="1000")
    patch_scrape(monkeypatch, product(price="900"))
    seen: list = []

    async def send(**_kw):
        seen.append((await load_tracker(tr.id)).current_price)
        return MagicMock(message_id=1)
    bot = make_bot()
    bot.send_message.side_effect = send
    await pc.check_single_price(tr, bot)
    assert seen == [D("900")]


# ─── Розкид пауз між запитами ────────────────────────────────────

async def test_rate_limiter_adds_random_jitter(monkeypatch):
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
    monkeypatch.setattr(scraper_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(scraper_mod.random, "uniform", lambda a, b: b)           # максимальний розкид
    limiter = scraper_mod.DomainRateLimiter(delay=2.0)
    await limiter.acquire("shop.example")
    await limiter.acquire("shop.example")
    assert len(waits) == 1 and 2.9 < waits[0] <= 3.0                            # 2 с + до 50 %
