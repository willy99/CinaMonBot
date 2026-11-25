"""
Магазин, що стабільно блокує перевірки: замість алерту адміну — позначка «заблоковано» в адмінці
(domain_blocks) і позиції юзерів на паузі (PAUSED_DOMAIN). Розблоковує адмін — позиції повертаються.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from app.admin import queries as q
from app.bot.handlers import main as h
from app.config import settings
from app.database.models import DomainBlock, PriceTracker, ScrapeLog, TrackerStatus
from app.database.session import get_session
from app.scheduler import price_checker as pc
from app.services.limits import sync_free_limit
from app.services.scrapers.scrape_log import record_scrape
from tests.helpers import make_bot
from tests.test_scheduler import dispatch_spies, load_tracker, seed_tracker, seed_user  # noqa: F401

SHOP = "shop.example"


async def fails(n: int, reason: str = "blocked", domain: str = SHOP) -> None:
    for _ in range(n):
        await record_scrape(domain, False, reason=reason)


async def blocks() -> dict[str, DomainBlock]:
    async with get_session() as s:
        return {b.domain: b for b in (await s.execute(select(DomainBlock))).scalars().all()}


# ─── Критерій і пауза ────────────────────────────────────────────

async def test_stable_blocking_marks_domain_and_pauses_trackers(db):
    user = await seed_user(1)
    a = await seed_tracker(user, f"https://{SHOP}/p/1")
    b = await seed_tracker(user, f"https://{SHOP}/p/2", status=TrackerStatus.PAUSED)        # вручну — не чіпаємо
    other = await seed_tracker(user, "https://other.example/p/1")
    await fails(5)
    await pc._update_domain_blocks({SHOP, "other.example"})

    bl = await blocks()
    assert set(bl) == {SHOP} and bl[SHOP].reason == "blocked"
    assert (await load_tracker(a.id)).status == TrackerStatus.PAUSED_DOMAIN
    assert (await load_tracker(b.id)).status == TrackerStatus.PAUSED
    assert (await load_tracker(other.id)).status == TrackerStatus.ACTIVE


async def test_not_blocked_on_too_few_attempts_or_any_success(db):
    await fails(4)
    await pc._update_domain_blocks({SHOP})
    assert await blocks() == {}
    await record_scrape(SHOP, True, strategy="json-ld")
    await fails(3)
    await pc._update_domain_blocks({SHOP})
    assert await blocks() == {}                                   # 7 невдач, але була вдала спроба


async def test_item_level_failures_do_not_block_shop(db):
    """404 / зміна сторінки — проблема конкретного товару (той трекер іде до ERROR), а не магазину."""
    await fails(4)
    await fails(1, reason="http_404")
    await pc._update_domain_blocks({SHOP})
    assert await blocks() == {}


async def test_network_failures_do_not_block_shop(db):
    """Збій мережі — на нашому боці (нічний обрив інтернету), а не «магазин блокує»."""
    await fails(5, reason="network")
    await fails(3, reason="timeout")
    await pc._update_domain_blocks({SHOP})
    assert await blocks() == {}


async def test_old_failures_are_ignored(db):
    async with get_session() as s:
        for _ in range(6):
            s.add(ScrapeLog(domain=SHOP, ok=False, reason="blocked", strategy=None, healed=False, duration_ms=0,
                            created_at=datetime.utcnow() - timedelta(hours=30)))
    await fails(2)
    await pc._update_domain_blocks({SHOP})
    assert await blocks() == {}


async def test_no_admin_alert_anymore(db):
    """Алерт «🚨 Проблема з парсером» прибрано: функції, що слала його адміну, більше немає."""
    assert not hasattr(pc, "_check_domain_health")


# ─── Розклад: заблокований магазин не смикаємо ───────────────────

async def test_dispatch_pauses_new_trackers_of_blocked_domain_and_skips_them(db, dispatch_spies):
    checked, _, _ = dispatch_spies
    user = await seed_user(1)
    tr = await seed_tracker(user, f"https://{SHOP}/p/9")           # додали вже після блокування
    ok = await seed_tracker(user, "https://other.example/p/1")
    async with get_session() as s:
        s.add(DomainBlock(domain=SHOP, reason="blocked"))
    await pc.dispatch_price_checks(make_bot())
    assert [t.id for t in checked] == [ok.id]
    assert (await load_tracker(tr.id)).status == TrackerStatus.PAUSED_DOMAIN


# ─── Юзер: /list і ліміт ─────────────────────────────────────────

async def test_list_shows_shop_blocking(db):
    user = await seed_user(1)
    tr = await seed_tracker(user, f"https://{SHOP}/p/1", status=TrackerStatus.PAUSED_DOMAIN)
    assert h.list_item_lines(await load_tracker(tr.id), "uk")[1:] == ["🚫 Магазин зараз блокує перевірки — позиція на паузі"]
    assert h.list_item_lines(await load_tracker(tr.id), "en")[1:] == ["🚫 The shop is blocking checks — item paused"]


async def test_free_limit_ignores_domain_paused(db):
    user = await seed_user(1)
    tr = await seed_tracker(user, f"https://{SHOP}/p/1", status=TrackerStatus.PAUSED_DOMAIN)
    await sync_free_limit(user.id)
    assert (await load_tracker(tr.id)).status == TrackerStatus.PAUSED_DOMAIN


# ─── Адмінка: позначка і розблокування ───────────────────────────

async def test_admin_marks_blocked_and_unblock_resumes(db):
    user = await seed_user(1)
    tr = await seed_tracker(user, f"https://{SHOP}/p/1")
    await fails(5)
    await pc._update_domain_blocks({SHOP})

    row = next(r for r in await q.platforms_table() if r["domain"] == SHOP)
    assert row["status"] == "blocked" and row["auto_blocked"] is True
    assert row["blocked_reason"] == "blocked" and row["paused"] == 1 and row["blocked_at"]
    assert [r["domain"] for r in await q.platforms_table(status="blocked") if r["domain"] == SHOP] == [SHOP]
    before = (await q.platforms_overview()).blocked
    detail = await q.platform_detail(SHOP)
    assert detail["auto_blocked"] is True and detail["paused_domain"] == 1

    assert await q.unblock_domain(SHOP) == 1
    assert await blocks() == {}
    fresh = await load_tracker(tr.id)
    assert fresh.status == TrackerStatus.ACTIVE and fresh.consecutive_errors == 0
    assert (await q.platforms_overview()).blocked == before - 1
    assert next(r for r in await q.platforms_table() if r["domain"] == SHOP)["auto_blocked"] is False


async def test_unblock_respects_free_limit(db, monkeypatch):
    """Поки магазин був заблокований, слоти могли зайняти інші товари — після розблокування ліміт перераховуємо."""
    monkeypatch.setattr(settings, "FREE_TIER_MAX_ITEMS", 1)
    from app.services import runtime_settings as rt
    rt.reset_cache()
    user = await seed_user(1)
    blocked = await seed_tracker(user, f"https://{SHOP}/p/1", status=TrackerStatus.PAUSED_DOMAIN,
                                 created_at=datetime.utcnow() - timedelta(days=2))
    other = await seed_tracker(user, "https://other.example/p/1", created_at=datetime.utcnow() - timedelta(days=1))
    async with get_session() as s:
        s.add(DomainBlock(domain=SHOP, reason="blocked"))
    await q.unblock_domain(SHOP)
    statuses = {(await load_tracker(blocked.id)).status, (await load_tracker(other.id)).status}
    assert statuses == {TrackerStatus.ACTIVE, TrackerStatus.PAUSED_LIMIT}


async def test_admin_platform_card_shows_block_and_unblocks(db, user):
    from nicegui import ui
    from app.database.models import AdminAction
    from tests.admin_helpers import login, make_admin, make_tracker, make_user, reset_auth_state, table_with
    reset_auth_state()
    await make_admin()
    alice = await make_user(1001, "Alice", "alice")
    tr = await make_tracker(alice.id, source=SHOP, title="Товар")
    async with get_session() as s:
        s.add(DomainBlock(domain=SHOP, reason="blocked"))
        (await s.get(PriceTracker, tr.id)).status = TrackerStatus.PAUSED_DOMAIN

    await login(user)
    await user.open("/platforms")
    row = next(r for r in table_with(user, "checks_30d").rows if r["domain"] == SHOP)
    assert row["status"] == "blocked" and row["auto_blocked"] is True
    user.find(kind=ui.table).trigger("rowClick", args=[None, row])
    await user.should_see("Заблоковано автоматично", retries=20)
    await user.should_see("Позицій юзерів на паузі: 1")
    user.find("Розблокувати й відновити позиції").click()
    for _ in range(60):                                   # обробник кнопки асинхронний — чекаємо на БД
        if (await load_tracker(tr.id)).status == TrackerStatus.ACTIVE:
            break
        await asyncio.sleep(0.05)
    assert (await load_tracker(tr.id)).status == TrackerStatus.ACTIVE and await blocks() == {}
    async with get_session() as s:
        actions = [a.action for a in (await s.execute(select(AdminAction))).scalars().all()]
    assert "domain_unblock" in actions
