"""Правила безкоштовного ліміту та продовження Premium (app/services/limits.py) + дашборд."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.admin import queries as q
from app.config import settings
from app.database.models import PriceTracker, SubscriptionTier, TrackerStatus, User
from app.database.session import get_session
from app.services.limits import extend_premium, sync_free_limit

LIMIT = settings.FREE_TIER_MAX_ITEMS


async def _user(tier="free", until=None) -> User:
    async with get_session() as s:
        u = User(telegram_id=1, first_name="T", language_code="uk", tier=tier, premium_until=until)
        s.add(u); await s.flush()
    return u


async def _trackers(u: User, n: int, status=TrackerStatus.ACTIVE) -> list[PriceTracker]:
    base = datetime.utcnow() - timedelta(days=1)
    out = []
    async with get_session() as s:
        for i in range(n):
            tr = PriceTracker(user_id=u.id, url=f"https://x.example/p/{i}", canonical_url=f"https://x.example/p/{i}",
                              source="x.example", title=f"T{i}", status=status, created_at=base + timedelta(minutes=i))
            s.add(tr); out.append(tr)
    return out


async def _statuses(u: User) -> list[str]:
    from sqlalchemy import select
    async with get_session() as s:
        rows = (await s.execute(select(PriceTracker).where(PriceTracker.user_id == u.id)
                                .order_by(PriceTracker.created_at))).scalars().all()
        return [r.status for r in rows]


async def test_free_user_keeps_oldest_active(db):
    u = await _user()
    await _trackers(u, LIMIT + 3)
    activated, paused = await sync_free_limit(u.id)
    assert len(activated) == 0 and [p.title for p in paused] == [f"T{LIMIT}", f"T{LIMIT + 1}", f"T{LIMIT + 2}"]
    assert await _statuses(u) == [TrackerStatus.ACTIVE] * LIMIT + [TrackerStatus.PAUSED_LIMIT] * 3


async def test_sync_is_idempotent(db):
    u = await _user()
    await _trackers(u, LIMIT + 1)
    await sync_free_limit(u.id)
    assert await sync_free_limit(u.id) == ([], [])


async def test_premium_user_everything_active(db):
    u = await _user(tier="premium", until=datetime.utcnow() + timedelta(days=10))
    await _trackers(u, LIMIT + 4, status=TrackerStatus.PAUSED_LIMIT)
    activated, paused = await sync_free_limit(u.id)
    assert len(activated) == LIMIT + 4 and paused == []


async def test_manual_pause_and_error_untouched(db):
    u = await _user()
    await _trackers(u, 2)
    async with get_session() as s:
        s.add(PriceTracker(user_id=u.id, url="https://x.example/m", canonical_url="https://x.example/m", source="x.example",
                           title="manual", status=TrackerStatus.PAUSED, created_at=datetime.utcnow() - timedelta(days=5)))
        s.add(PriceTracker(user_id=u.id, url="https://x.example/e", canonical_url="https://x.example/e", source="x.example",
                           title="broken", status=TrackerStatus.ERROR, created_at=datetime.utcnow() - timedelta(days=5)))
    await sync_free_limit(u.id)
    assert sorted(await _statuses(u)) == sorted([TrackerStatus.PAUSED, TrackerStatus.ERROR, TrackerStatus.ACTIVE, TrackerStatus.ACTIVE])


async def test_expired_premium_counts_as_free(db):
    u = await _user(tier="premium", until=datetime.utcnow() - timedelta(days=1))
    await _trackers(u, LIMIT + 1)
    _, paused = await sync_free_limit(u.id)
    assert len(paused) == 1


async def test_unknown_user_is_noop(db):
    assert await sync_free_limit("nope") == ([], [])


def test_extend_premium_from_now_when_free():
    u = User(telegram_id=1, tier="free", premium_until=None)
    now = datetime(2026, 10, 11, 12, 0)
    assert extend_premium(u, 30, now) == datetime(2026, 11, 10, 12, 0)
    assert u.tier == SubscriptionTier.PREMIUM


def test_extend_premium_adds_to_active_subscription():
    now = datetime(2026, 10, 11, 12, 0)
    u = User(telegram_id=1, tier="premium", premium_until=now + timedelta(days=100))
    assert extend_premium(u, 30, now) == now + timedelta(days=130)


def test_extend_premium_expired_subscription_restarts_from_now():
    now = datetime(2026, 10, 11, 12, 0)
    u = User(telegram_id=1, tier="premium", premium_until=now - timedelta(days=3))
    assert extend_premium(u, 7, now) == now + timedelta(days=7)


async def test_dashboard_set_premium_unpauses_and_revoke_pauses(db):
    u = await _user()
    await _trackers(u, LIMIT + 2)
    await sync_free_limit(u.id)
    assert (await _statuses(u)).count(TrackerStatus.PAUSED_LIMIT) == 2
    await q.set_premium(u.id, 30)
    assert all(st == TrackerStatus.ACTIVE for st in await _statuses(u))
    await q.revoke_premium(u.id)
    assert (await _statuses(u)).count(TrackerStatus.PAUSED_LIMIT) == 2


async def test_dispatch_skips_paused_limit(db, monkeypatch):
    """Планувальник перевіряє лише ACTIVE: товари на паузі через ліміт не парсяться."""
    from app.scheduler import price_checker as pc
    u = await _user()
    await _trackers(u, LIMIT + 2)
    await sync_free_limit(u.id)
    checked = []
    async def fake_check(tracker, bot): checked.append(tracker.title)
    monkeypatch.setattr(pc, "check_single_price", fake_check)
    monkeypatch.setattr(pc, "_check_domain_health", lambda *a, **k: __import__("asyncio").sleep(0))
    await pc.dispatch_price_checks(None)
    assert sorted(checked) == sorted(f"T{i}" for i in range(LIMIT))


async def test_sync_all_free_limits_fixes_legacy_data(db):
    """Старі дані: Free-юзер із 7 активними, Premium-юзер із товарами на паузі через ліміт."""
    from app.services.limits import sync_all_free_limits
    free = await _user()
    await _trackers(free, LIMIT + 2)
    async with get_session() as s:
        prem = User(telegram_id=2, first_name="P", language_code="uk", tier="premium",
                    premium_until=datetime.utcnow() + timedelta(days=5))
        s.add(prem); await s.flush()
    await _trackers(prem, 3, status=TrackerStatus.PAUSED_LIMIT)
    assert await sync_all_free_limits() == 5
    assert (await _statuses(free)).count(TrackerStatus.PAUSED_LIMIT) == 2
    assert all(st == TrackerStatus.ACTIVE for st in await _statuses(prem))
    assert await sync_all_free_limits() == 0
