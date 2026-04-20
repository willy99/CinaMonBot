"""
Celery tasks — фонові задачі для перевірки цін.

Архітектура:
- check_single_price: перевіряє один товар (викликається для кожного трекера)
- dispatch_price_checks: планувальник, що запускає check_single_price масово
- Використовуємо asyncio.run() для async коду всередині Celery task

Важливо:
- Celery task є атомарною одиницею — кожен товар незалежно
- При помилці — тільки цей товар отримує consecutive_errors++
- При 5+ помилках — трекер автоматично ставиться на паузу
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import UUID

import structlog
from celery import Celery
from celery.schedules import crontab
from sqlalchemy import select, and_

from app.config import settings

logger = structlog.get_logger(__name__)

# ─── Celery App ──────────────────────────────────────────────────────────────

celery_app = Celery(
    "priceguard",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Europe/Kyiv",
    enable_utc=True,
    # Важливо: обмежуємо час виконання задачі
    task_soft_time_limit=60,    # soft: raise SoftTimeLimitExceeded
    task_time_limit=90,         # hard: kill process
    # Retry при краші воркера
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Автоматично видаляємо старі результати
    result_expires=3600,
)

# ─── Periodic Tasks (Beat Schedule) ──────────────────────────────────────────

celery_app.conf.beat_schedule = {
    # Кожні 15 хвилин запускаємо диспетчер
    # Він сам вирішує які трекери треба перевірити зараз
    "dispatch-price-checks": {
        "task": "app.worker.tasks.dispatch_price_checks",
        "schedule": crontab(minute="*/15"),
    },
}


# ─── Tasks ───────────────────────────────────────────────────────────────────

@celery_app.task(
    name="app.worker.tasks.check_single_price",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def check_single_price(self, tracker_id: str) -> dict:
    """
    Перевіряє ціну одного товару і зберігає результат у БД.
    Якщо ціна змінилась — ставить задачу в чергу на відправку сповіщення.
    """
    return asyncio.run(_check_single_price_async(tracker_id))


async def _check_single_price_async(tracker_id: str) -> dict:
    from app.database.session import get_session
    from app.database.models import PriceTracker, PriceHistory, TrackerStatus
    from app.services.scrapers.scraper import ScraperFactory, ScrapeError
    from datetime import datetime, timezone

    async with get_session() as session:
        # Завантажуємо трекер
        tracker = await session.get(PriceTracker, UUID(tracker_id))
        if not tracker or tracker.status != TrackerStatus.ACTIVE:
            return {"status": "skipped", "reason": "not_active"}

        # Отримуємо правильний скрапер
        scraper = ScraperFactory.get(tracker.canonical_url)
        if not scraper:
            return {"status": "error", "reason": "no_scraper"}

        # Скрапимо
        result = await scraper.scrape(tracker.canonical_url)

        if isinstance(result, ScrapeError):
            # Збільшуємо лічильник помилок
            tracker.consecutive_errors += 1
            tracker.last_checked_at = datetime.now(timezone.utc)

            if tracker.consecutive_errors >= 5:
                tracker.status = TrackerStatus.ERROR
                logger.warning(
                    "tracker_auto_paused",
                    tracker_id=tracker_id,
                    reason=result.reason,
                )

            return {"status": "error", "reason": result.reason}

        # Успіх — оновлюємо трекер
        old_price = tracker.current_price
        tracker.current_price = result.price
        tracker.is_available = result.is_available
        tracker.title = result.title
        tracker.image_url = result.image_url
        tracker.last_checked_at = datetime.now(timezone.utc)
        tracker.consecutive_errors = 0

        # Записуємо в історію (якщо ціна є)
        if result.price is not None:
            history = PriceHistory(
                tracker_id=tracker.id,
                price=result.price,
                is_available=result.is_available,
            )
            session.add(history)

        await session.flush()

        # Перевіряємо чи треба надіслати сповіщення
        should_notify = _should_notify(
            old_price=old_price,
            new_price=result.price,
            target_price=tracker.target_price,
            old_available=tracker.is_available,
            new_available=result.is_available,
        )

        if should_notify:
            # Ставимо в чергу відправку сповіщення
            send_price_notification.delay(
                tracker_id=tracker_id,
                user_id=str(tracker.user_id),
                old_price=str(old_price) if old_price else None,
                new_price=str(result.price) if result.price else None,
                event_type=should_notify,
            )

        return {
            "status": "ok",
            "price": str(result.price),
            "notified": bool(should_notify),
        }


def _should_notify(
    old_price: Decimal | None,
    new_price: Decimal | None,
    target_price: Decimal | None,
    old_available: bool,
    new_available: bool,
) -> str | None:
    """
    Визначає чи треба надсилати сповіщення і якого типу.
    Повертає тип події або None.
    """
    # Товар з'явився в наявності
    if not old_available and new_available:
        return "restock"

    # Ціна впала нижче цільової
    if target_price and new_price and new_price <= target_price:
        return "target_reached"

    # Ціна знизилась більш ніж на 3%
    if old_price and new_price and new_price < old_price:
        drop_pct = (old_price - new_price) / old_price * 100
        if drop_pct >= 3:
            return "decrease"

    return None


@celery_app.task(name="app.worker.tasks.send_price_notification")
def send_price_notification(
    tracker_id: str,
    user_id: str,
    old_price: str | None,
    new_price: str | None,
    event_type: str,
) -> dict:
    """Відправляє Telegram-повідомлення юзеру."""
    return asyncio.run(
        _send_notification_async(tracker_id, user_id, old_price, new_price, event_type)
    )


async def _send_notification_async(
    tracker_id: str,
    user_id: str,
    old_price: str | None,
    new_price: str | None,
    event_type: str,
) -> dict:
    from app.database.session import get_session
    from app.database.models import User, PriceTracker, Notification, PriceEventType, NotificationStatus
    from app.bot.notifications import send_price_alert
    from decimal import Decimal
    from uuid import UUID

    async with get_session() as session:
        user = await session.get(User, UUID(user_id))
        tracker = await session.get(PriceTracker, UUID(tracker_id))

        if not user or not tracker or user.is_blocked:
            return {"status": "skipped"}

        try:
            message_id = await send_price_alert(
                telegram_id=user.telegram_id,
                tracker=tracker,
                event_type=event_type,
                old_price=Decimal(old_price) if old_price else None,
                new_price=Decimal(new_price) if new_price else None,
            )
            notification = Notification(
                user_id=user.id,
                tracker_id=tracker.id,
                event_type=PriceEventType(event_type),
                old_price=Decimal(old_price) if old_price else None,
                new_price=Decimal(new_price) if new_price else None,
                status=NotificationStatus.SENT,
                telegram_message_id=message_id,
            )
        except Exception as e:
            notification = Notification(
                user_id=user.id,
                tracker_id=tracker.id,
                event_type=PriceEventType(event_type),
                old_price=Decimal(old_price) if old_price else None,
                new_price=Decimal(new_price) if new_price else None,
                status=NotificationStatus.FAILED,
                error_message=str(e),
            )
            logger.error("notification_failed", error=str(e))

        session.add(notification)
        return {"status": "ok"}


@celery_app.task(name="app.worker.tasks.dispatch_price_checks")
def dispatch_price_checks() -> dict:
    """
    Диспетчер: знаходить всі трекери що потребують перевірки
    і запускає check_single_price для кожного.
    """
    return asyncio.run(_dispatch_async())


async def _dispatch_async() -> dict:
    from app.database.session import get_session
    from app.database.models import PriceTracker, TrackerStatus, User, SubscriptionTier
    from datetime import datetime, timezone, timedelta
    from sqlalchemy.orm import joinedload

    async with get_session() as session:
        now = datetime.now(timezone.utc)

        # Завантажуємо активні трекери з інформацією про юзера
        stmt = (
            select(PriceTracker)
            .join(PriceTracker.user)
            .where(PriceTracker.status == TrackerStatus.ACTIVE)
            .options(joinedload(PriceTracker.user))
        )
        result = await session.execute(stmt)
        trackers = result.scalars().all()

        dispatched = 0
        for tracker in trackers:
            user = tracker.user
            interval_minutes = (
                settings.PREMIUM_CHECK_INTERVAL_MINUTES
                if user.is_premium
                else settings.FREE_TIER_CHECK_INTERVAL_MINUTES
            )

            # Чи настав час перевіряти?
            if tracker.last_checked_at is None:
                should_check = True
            else:
                next_check = tracker.last_checked_at + timedelta(minutes=interval_minutes)
                should_check = now >= next_check

            if should_check:
                check_single_price.delay(str(tracker.id))
                dispatched += 1

        logger.info("dispatch_complete", total=len(trackers), dispatched=dispatched)
        return {"dispatched": dispatched, "total": len(trackers)}
