"""
Ліміт безкоштовного плану і продовження Premium — одне місце для всіх правил.

Правило: у Free-юзера активні лише FREE_TIER_MAX_ITEMS найстаріших трекерів
(за датою додавання); решта отримують статус PAUSED_LIMIT і не перевіряються.
У Premium-юзера всі PAUSED_LIMIT вмикаються. Ручна пауза адміном (PAUSED)
і помилкові (ERROR) трекери не чіпаються.

Викликається після кожної події, що змінює ситуацію:
    додано товар · видалено товар · активовано Premium · закінчився Premium

    activated, paused = await sync_free_limit(user_id)
    until = await extend_premium(user, days)   # всередині відкритої сесії
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select

from app.database.models import PriceTracker, SubscriptionTier, TrackerStatus, User
from app.database.session import get_session

logger = structlog.get_logger(__name__)


async def sync_free_limit(user_id: str) -> tuple[list[PriceTracker], list[PriceTracker]]:
    """
    Перераховує статуси ACTIVE ↔ PAUSED_LIMIT для юзера.
    Повертає (увімкнені, поставлені на паузу) — трекери, статус яких змінився.
    """
    activated: list[PriceTracker] = []
    paused: list[PriceTracker] = []
    async with get_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return activated, paused
        limit = user.active_tracker_limit
        trackers = (await session.execute(
            select(PriceTracker)
            .where(
                PriceTracker.user_id == user_id,
                PriceTracker.status.in_([TrackerStatus.ACTIVE, TrackerStatus.PAUSED_LIMIT]),
            )
            .order_by(PriceTracker.created_at.asc(), PriceTracker.id.asc())
        )).scalars().all()

        for i, tr in enumerate(trackers):
            want = TrackerStatus.ACTIVE if i < limit else TrackerStatus.PAUSED_LIMIT
            if tr.status != want:
                tr.status = want
                (activated if want == TrackerStatus.ACTIVE else paused).append(tr)

    if activated or paused:
        logger.info("free_limit_synced", user_id=user_id, limit=limit,
                    activated=len(activated), paused=len(paused))
    return activated, paused


def extend_premium(user: User, days: int, now: datetime | None = None) -> datetime:
    """
    Продовжує Premium на N днів: якщо він ще діє — від поточної дати закінчення,
    інакше — від сьогодні. Змінює user у відкритій сесії, повертає нову дату.
    """
    now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    current = user.premium_until
    if current is not None and current.tzinfo is not None:
        current = current.astimezone(timezone.utc).replace(tzinfo=None)
    base = current if (user.tier == SubscriptionTier.PREMIUM and current and current > now) else now
    user.tier = SubscriptionTier.PREMIUM
    user.premium_until = base + timedelta(days=days)
    return user.premium_until


async def sync_all_free_limits() -> int:
    """
    Перераховує ліміт для всіх юзерів, у яких стан не відповідає правилу
    (Free з активними понад ліміт або Premium із товарами на паузі через ліміт).
    Викликається при старті бота і раз на годину. Повертає кількість змінених трекерів.
    """
    from sqlalchemy import func

    changed = 0
    async with get_session() as session:
        active_over = (await session.execute(
            select(PriceTracker.user_id)
            .where(PriceTracker.status == TrackerStatus.ACTIVE)
            .group_by(PriceTracker.user_id)
            .having(func.count() > 0)
        )).scalars().all()
        paused_any = (await session.execute(
            select(PriceTracker.user_id)
            .where(PriceTracker.status == TrackerStatus.PAUSED_LIMIT)
            .distinct()
        )).scalars().all()
    for user_id in set(active_over) | set(paused_any):
        activated, paused = await sync_free_limit(user_id)
        changed += len(activated) + len(paused)
    if changed:
        logger.info("free_limits_resynced", changed=changed)
    return changed
