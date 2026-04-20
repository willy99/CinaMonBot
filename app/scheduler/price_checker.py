"""
Планувальник перевірки цін на базі APScheduler.

Замість Celery (потребує Redis + окремий процес),
використовуємо APScheduler який працює в тому ж процесі що й бот.

Для 0-2000 юзерів це абсолютно достатньо.
Якщо треба більше — можна мігрувати на Celery пізніше без зміни логіки.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.config import settings
from app.database.session import get_session
from app.database.models import (
    PriceTracker, PriceHistory, Notification,
    TrackerStatus, PriceEventType, NotificationStatus,
)
from app.services.scrapers.scraper import ScraperFactory, ScrapeError

logger = structlog.get_logger(__name__)

# Глобальний scheduler — один на весь процес
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")


def setup_scheduler(bot) -> None:
    """
    Реєструємо задачі і стартуємо scheduler.
    Викликається один раз при запуску бота.
    """
    scheduler.add_job(
        dispatch_price_checks,
        trigger="interval",
        minutes=settings.SCHEDULER_INTERVAL_MINUTES,
        args=[bot],
        id="dispatch_price_checks",
        replace_existing=True,
        # Перший запуск через 1 хвилину після старту
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    scheduler.start()
    logger.info("scheduler_started", interval_minutes=settings.SCHEDULER_INTERVAL_MINUTES)


async def dispatch_price_checks(bot) -> None:
    logger.info("dispatch_started")

    async with get_session() as session:
        stmt = (
            select(PriceTracker)
            .where(PriceTracker.status == TrackerStatus.ACTIVE)
            .options(joinedload(PriceTracker.user))
        )
        result = await session.execute(stmt)
        trackers = result.scalars().all()

    now = datetime.now(timezone.utc)
    to_check = []

    for tracker in trackers:
        user = tracker.user
        interval = (
            settings.PREMIUM_CHECK_INTERVAL_MINUTES
            if user.is_premium
            else settings.FREE_TIER_CHECK_INTERVAL_MINUTES
        )
        if tracker.last_checked_at is None:
            to_check.append(tracker)
        else:
            last = tracker.last_checked_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now >= last + timedelta(minutes=interval):
                to_check.append(tracker)

    if not to_check:
        logger.info("dispatch_nothing_to_check", total=len(trackers))
        return

    logger.info("dispatch_checking", count=len(to_check), total=len(trackers))

    semaphore = asyncio.Semaphore(5)
    results_by_domain: dict[str, list[bool]] = {}

    async def check_with_semaphore(tracker: PriceTracker) -> None:
        domain = tracker.source
        async with semaphore:
            try:
                await check_single_price(tracker, bot)
                results_by_domain.setdefault(domain, []).append(True)
            except Exception as e:
                logger.error("tracker_check_failed", tracker_id=tracker.id, error=str(e))
                results_by_domain.setdefault(domain, []).append(False)

    await asyncio.gather(*[check_with_semaphore(t) for t in to_check])
    await _check_domain_health(bot, results_by_domain)
    logger.info("dispatch_finished", checked=len(to_check))


async def _check_domain_health(bot, results_by_domain: dict[str, list[bool]]) -> None:
    admin_id = settings.ADMIN_TELEGRAM_ID
    if not admin_id:
        return

    for domain, results in results_by_domain.items():
        if len(results) < 3:
            continue
        fails = results.count(False)
        total = len(results)
        error_rate = fails / total
        if error_rate >= 0.5:
            logger.warning("domain_high_error_rate", domain=domain, error_rate=error_rate)
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"🚨 <b>Проблема з парсером!</b>\n\n"
                        f"🌐 Домен: <code>{domain}</code>\n"
                        f"❌ Помилок: {fails}/{total} ({error_rate:.0%})\n\n"
                        f"⚙️ Запущено автовідновлення через Claude AI.\n"
                        f"Якщо не зникне — перевір вручну:\n"
                        f"<code>python debug/debug_rozetka.py</code>"
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error("admin_alert_failed", error=str(e))


async def check_single_price(tracker: PriceTracker, bot) -> None:
    """Перевіряє ціну одного товару і відправляє сповіщення якщо треба."""

    scraper = ScraperFactory.get(tracker.canonical_url)
    if not scraper:
        return

    result = await scraper.scrape(tracker.canonical_url)
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        # Перезавантажуємо трекер в новій сесії
        db_tracker = await session.get(PriceTracker, tracker.id)
        if not db_tracker:
            return

        if isinstance(result, ScrapeError):
            db_tracker.consecutive_errors += 1
            db_tracker.last_checked_at = now

            if db_tracker.consecutive_errors >= 5:
                db_tracker.status = TrackerStatus.ERROR
                logger.warning(
                    "tracker_auto_disabled",
                    tracker_id=tracker.id,
                    reason=result.reason,
                )
            return

        # Зберігаємо попередню ціну для порівняння
        old_price = db_tracker.current_price
        old_available = db_tracker.is_available

        # Оновлюємо дані трекера
        db_tracker.current_price = result.price
        db_tracker.is_available = result.is_available
        db_tracker.title = result.title
        db_tracker.image_url = result.image_url
        db_tracker.last_checked_at = now
        db_tracker.consecutive_errors = 0

        # Пишемо в історію
        if result.price is not None:
            session.add(PriceHistory(
                tracker_id=db_tracker.id,
                price=result.price,
                is_available=result.is_available,
            ))

        # Визначаємо тип події
        event = _detect_event(
            old_price=old_price,
            new_price=result.price,
            target_price=db_tracker.target_price,
            old_available=old_available,
            new_available=result.is_available,
        )

        if event:
            # Відправляємо сповіщення
            await _send_notification(
                bot=bot,
                session=session,
                tracker=db_tracker,
                event=event,
                old_price=old_price,
                new_price=result.price,
            )


def _detect_event(
    old_price: Decimal | None,
    new_price: Decimal | None,
    target_price: Decimal | None,
    old_available: bool,
    new_available: bool,
) -> PriceEventType | None:
    """Визначає чи потрібно сповіщення і якого типу."""

    # Товар знову з'явився
    if not old_available and new_available:
        return PriceEventType.RESTOCK

    # Ціна досягла цільової
    if target_price and new_price and new_price <= target_price:
        return PriceEventType.TARGET_REACHED

    # Ціна впала більш ніж на 3%
    if old_price and new_price and new_price < old_price:
        drop_pct = (old_price - new_price) / old_price * 100
        if drop_pct >= 3:
            return PriceEventType.DECREASE

    return None


async def _send_notification(
    bot,
    session,
    tracker: PriceTracker,
    event: PriceEventType,
    old_price: Decimal | None,
    new_price: Decimal | None,
) -> None:
    """Формує і відправляє повідомлення в Telegram."""
    from sqlalchemy import select as sa_select
    from app.database.models import User

    user = await session.get(User, tracker.user_id)
    if not user or user.is_blocked:
        return

    # Формуємо текст залежно від типу події
    title_short = (tracker.title or "Товар")[:60]

    if event == PriceEventType.RESTOCK:
        text = (
            f"✅ <b>З'явився в наявності!</b>\n\n"
            f"📦 {title_short}\n"
            f"💰 Ціна: <b>{new_price:,.0f} грн</b>\n\n"
            f"🔗 <a href='{tracker.canonical_url}'>Перейти до товару</a>"
        )
    elif event == PriceEventType.TARGET_REACHED:
        text = (
            f"🎯 <b>Ціна досягла твоєї цілі!</b>\n\n"
            f"📦 {title_short}\n"
            f"💰 Ціна зараз: <b>{new_price:,.0f} грн</b>\n"
            f"🎯 Твоя ціль: {tracker.target_price:,.0f} грн\n\n"
            f"🔗 <a href='{tracker.canonical_url}'>Купити зараз</a>"
        )
    else:  # DECREASE
        diff = old_price - new_price if (old_price and new_price) else Decimal(0)
        pct = int(diff / old_price * 100) if old_price else 0
        text = (
            f"📉 <b>Ціна знизилась на {pct}%!</b>\n\n"
            f"📦 {title_short}\n"
            f"💰 Було: <s>{old_price:,.0f} грн</s>\n"
            f"💚 Стало: <b>{new_price:,.0f} грн</b>\n"
            f"💾 Економія: {diff:,.0f} грн\n\n"
            f"🔗 <a href='{tracker.canonical_url}'>Перейти до товару</a>"
        )

    try:
        msg = await bot.send_message(
            chat_id=user.telegram_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        session.add(Notification(
            user_id=user.id,
            tracker_id=tracker.id,
            event_type=event,
            old_price=old_price,
            new_price=new_price or Decimal(0),
            status=NotificationStatus.SENT,
            telegram_message_id=msg.message_id,
        ))
        logger.info("notification_sent", event=event, user=user.telegram_id)

    except Exception as e:
        # Якщо юзер заблокував бота
        if "bot was blocked" in str(e).lower():
            user.is_blocked = True
        session.add(Notification(
            user_id=user.id,
            tracker_id=tracker.id,
            event_type=event,
            old_price=old_price,
            new_price=new_price or Decimal(0),
            status=NotificationStatus.FAILED,
            error_message=str(e)[:200],
        ))
        logger.error("notification_failed", error=str(e))