"""
Запобіжники масштабування перевірок:
- одне посилання — один запит за прогін, хоч його відстежують кілька юзерів (результат — кожному);
- межа запитів на хвилину до одного магазину: надлишок — на наступні хвилини, найдавніші першими (без сплесків);
- випадковий розкид пауз між запитами до одного магазину.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D
from unittest.mock import AsyncMock

from app.config import settings
from app.scheduler import price_checker as pc
from app.services import runtime_settings as rt
from app.services.scrapers import scraper as scraper_mod
from tests.helpers import make_bot
from tests.test_scheduler import dispatch_spies, load_tracker, patch_scrape, product, seed_tracker, seed_user  # noqa: F401


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
