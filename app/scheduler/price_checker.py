"""
Планувальник перевірки цін на базі APScheduler.

Замість Celery (потребує Redis + окремий процес),
використовуємо APScheduler який працює в тому ж процесі що й бот.

Для 0-2000 юзерів це абсолютно достатньо.
Якщо треба більше — можна мігрувати на Celery пізніше без зміни логіки.
"""
from __future__ import annotations

import asyncio
from html import escape as html_escape
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.scheduler import breaker
from botkit.ops import heartbeat
from app.config import settings
from app.database.session import get_session
from app.database.models import (
    PriceTracker, PriceHistory, Notification,
    TrackerStatus, PriceEventType, NotificationStatus,
)
from app.services.scrapers.scraper import ScraperFactory, ScrapeError
from app.services.scrapers.registry import platform_for, platform_name
from app.services.scrapers.scrape_log import prune_scrape_log
from app.i18n import t, fmt_money, fmt_percent
from app.scheduler.digest import send_weekly_digests

logger = structlog.get_logger(__name__)

# Глобальний scheduler — один на весь процес
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")

# НОВЕ: максимум перевірок за один запуск — обмежує пікове навантаження
MAX_CHECKS_PER_RUN = 200


def setup_scheduler(bot) -> None:
    """
    Реєструємо задачі і стартуємо scheduler.
    Викликається один раз при запуску бота.
    """
    # Перевірка закінчення Premium — раз на годину
    scheduler.add_job(
        handle_expired_premiums,
        trigger="interval",
        hours=1,
        args=[bot],
        id="handle_expired_premiums",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
    )

    scheduler.add_job(
        dispatch_price_checks,
        trigger="interval",
        minutes=5,   # НОВЕ: кожні 5 хв замість SCHEDULER_INTERVAL_MINUTES для рівномірності
        args=[bot],
        id="dispatch_price_checks",
        replace_existing=True,
        # Перший запуск через 1 хвилину після старту
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    # Чистимо журнал парсингу (scrape_log) — щодня о 04:00 за Києвом, лишаємо 30 днів
    scheduler.add_job(
        prune_scrape_log,
        trigger="cron",
        hour=4,
        id="prune_scrape_log",
        replace_existing=True,
    )
    # Щотижневий дайджест "що подешевшало" — щопонеділка о 10:00 за Києвом
    # (шлеться лише тим, хто не вимкнув digest_enabled, і лише коли щось подешевшало)
    scheduler.add_job(
        send_weekly_digests,
        trigger="cron",
        day_of_week="mon",
        hour=10,
        args=[bot],
        id="weekly_digest",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("scheduler_started", interval_minutes=5, max_per_run=MAX_CHECKS_PER_RUN)


async def handle_expired_premiums(bot) -> None:
    """
    Знаходить юзерів у яких щойно закінчився Premium
    і призупиняє трекери понад ліміт безкоштовного плану.
    Не видаляє — лише ставить PAUSED.
    """
    from sqlalchemy import select
    from datetime import datetime, timezone
    from app.database.models import User, SubscriptionTier
    from app.config import settings

    async with get_session() as session:
        now = datetime.now(timezone.utc)

        # Знаходимо юзерів у яких premium_until вже минув але tier ще "premium"
        stmt = select(User).where(
            User.tier == SubscriptionTier.PREMIUM,
            User.premium_until < now,
        )
        expired_users = (await session.execute(stmt)).scalars().all()

        for user in expired_users:
            # Переводимо на free
            user.tier = SubscriptionTier.FREE
            user.premium_until = None
            logger.info("premium_expired", user_id=user.id, telegram_id=user.telegram_id)

    # Для кожного expired user — призупиняємо зайві трекери
    for user in expired_users:
        await _pause_excess_trackers(user, bot)

    # Страховка раз на годину: якщо десь стан розійшовся з правилом — вирівнюємо мовчки
    from app.services.limits import sync_all_free_limits
    await sync_all_free_limits()


async def _pause_excess_trackers(user, bot) -> None:
    """
    Після закінчення Premium: активними лишаються FREE_TIER_MAX_ITEMS найстаріших,
    решта → PAUSED_LIMIT (через sync_free_limit). Повідомляє юзера, якщо щось поставлено на паузу.
    """
    from app.config import settings
    from app.services.limits import sync_free_limit

    _, paused = await sync_free_limit(user.id)
    if not paused:
        return  # все в межах ліміту

    lang = user.lang
    paused_titles = [html_escape((tr.title or t(lang, "item"))[:40], quote=False) for tr in paused]   # HTML-повідомлення
    logger.info("trackers_paused_on_expiry", user_id=user.id, paused_count=len(paused))

    # Повідомляємо юзера
    if not user.is_blocked and not user.bot_blocked and bot:
        paused_list = "\n".join(f"  ⏸ {title}" for title in paused_titles)
        msg_text = t(
            lang, "premium_expired",
            n=settings.FREE_TIER_MAX_ITEMS,
            paused=paused_list,
        )
        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=msg_text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("expiry_notify_failed", error=str(e))


async def dispatch_price_checks(bot) -> None:
    try:
        await _dispatch_price_checks(bot)
    finally:
        try:
            await heartbeat.beat(heartbeat.SCHEDULER, paused=breaker.snapshot())
        except Exception as e:  # noqa: BLE001 — heartbeat не має ламати планувальник
            logger.warning("heartbeat_failed", error=str(e)[:200])


async def _dispatch_price_checks(bot) -> None:
    logger.info("dispatch_started")

    async with get_session() as session:
        # НОВЕ: сортуємо по last_checked_at ASC — найстаріші перевіряємо першими
        # NULL (ніколи не перевірялись) йдуть самими першими
        stmt = (
            select(PriceTracker)
            .where(PriceTracker.status == TrackerStatus.ACTIVE)
            .options(joinedload(PriceTracker.user))
            .order_by(PriceTracker.last_checked_at.asc().nullsfirst())
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
        if tracker.source and breaker.is_open(tracker.source):
            continue                  # магазин тимчасово недоступний — last_checked_at не чіпаємо
        if tracker.last_checked_at is None:
            to_check.append(tracker)
        else:
            last = tracker.last_checked_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now >= last + timedelta(minutes=interval):
                to_check.append(tracker)

        # НОВЕ: зупиняємось як тільки набрали ліміт
        # Решта трекерів дочекається наступного запуску через 5 хвилин
        if len(to_check) >= MAX_CHECKS_PER_RUN:
            break

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


_PLATFORM_STATUS_UA = {
    "verified": "перевірено",
    "experimental": "експериментально",
    "needs_js": "потрібен JS",
    "blocked": "блокує запити",
}


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
            platform = platform_for(domain)
            status_text = (
                f"{_PLATFORM_STATUS_UA.get(platform.status, platform.status)}"
                + (f" · {platform.strategy}" if platform.strategy else "")
                if platform else "немає в реєстрі (універсальний парсер)"
            )
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"🚨 <b>Проблема з парсером: {platform_name(domain)}</b>\n\n"
                        f"🌐 Домен: <code>{domain}</code>\n"
                        f"📒 Статус у реєстрі: {status_text}\n"
                        f"❌ Помилок: {fails}/{total} ({error_rate:.0%})\n\n"
                        f"⚙️ Запущено автовідновлення через Claude AI.\n"
                        f"Якщо не зникне — дивись /platforms у дашборді."
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
            transient = breaker.is_transient(result.reason)
            if transient and tracker.source:
                breaker.trip(tracker.source, result.reason)     # пауза домену, а не вирок товару
            limit = breaker.TRANSIENT_ERROR_LIMIT if transient else breaker.ERROR_LIMIT

            if db_tracker.consecutive_errors >= limit:
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
        if result.currency:
            db_tracker.currency = result.currency.upper()
        db_tracker.is_available = result.is_available
        db_tracker.title = result.title
        db_tracker.image_url = result.image_url
        db_tracker.last_checked_at = now
        db_tracker.consecutive_errors = 0
        if tracker.source:
            breaker.success(tracker.source)

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
            # Налаштування трекера і антиспам: придушене сповіщення лише логуємо,
            # рядок Notification НЕ створюємо (юзер нічого не отримував)
            reason = _suppression_reason(
                tracker=db_tracker, event=event, old_price=old_price, new_price=result.price, now=now,
            )
            if reason:
                logger.info(
                    "notification_suppressed",
                    tracker_id=db_tracker.id,
                    event_type=str(event),
                    reason=reason,
                )
                return

            # Відправляємо сповіщення
            await _send_notification(
                bot=bot,
                session=session,
                tracker=db_tracker,
                event=event,
                old_price=old_price,
                new_price=result.price,
            )


def _suppression_reason(
    tracker: PriceTracker,
    event: PriceEventType,
    old_price: Decimal | None,
    new_price: Decimal | None,
    now: datetime,
) -> str | None:
    """
    Чому НЕ слати сповіщення про подію (None → слати):
    - "muted_restock"   — юзер вимкнув сповіщення про наявність по цьому товару
    - "muted_drop"      — юзер вимкнув сповіщення про зниження/ціль по цьому товару
    - "restock_cooldown"— RESTOCK уже надсилали менше ніж RESTOCK_COOLDOWN_DAYS днів тому
                          (товар "мигає" в наявності); виняток — ціна стала нижчою, ніж була
    """
    if event == PriceEventType.RESTOCK:
        if not tracker.notify_restock:
            return "muted_restock"
        last = tracker.last_restock_notified_at
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            cheaper = bool(old_price and new_price and new_price < old_price)
            if now - last < timedelta(days=settings.RESTOCK_COOLDOWN_DAYS) and not cheaper:
                return "restock_cooldown"
        return None

    if event in (PriceEventType.DECREASE, PriceEventType.TARGET_REACHED):
        if not tracker.notify_drop:
            return "muted_drop"
    return None


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
    if not user or user.is_blocked or user.bot_blocked:
        return

    # Формуємо текст залежно від типу події — мовою і валютою юзера/трекера
    lang = user.lang
    cur = tracker.currency
    # Назва й URL — з сайту магазину / від користувача: екрануємо, інакше «AT&T» чи «<Samsung>» ламають
    # сповіщення (Telegram 400), а апостроф у URL виходить з атрибута href='…'
    title_short = html_escape((tracker.title or t(lang, "item"))[:60], quote=False)
    safe_url = html_escape(tracker.canonical_url or "", quote=True)

    if event == PriceEventType.RESTOCK:
        text = t(
            lang, "notif_restock",
            title=title_short,
            price=fmt_money(new_price, cur, lang),
            url=safe_url,
        )
    elif event == PriceEventType.TARGET_REACHED and tracker.target_percent:
        # Ціль у відсотках ("впаде на 15%") — показуємо відсоток і базову ціну
        text = t(
            lang, "notif_target_percent",
            pct=fmt_percent(tracker.target_percent, lang),
            title=title_short,
            price=fmt_money(new_price, cur, lang),
            baseline=fmt_money(tracker.baseline_price, cur, lang),
            target=fmt_money(tracker.target_price, cur, lang),
            url=safe_url,
        )
    elif event == PriceEventType.TARGET_REACHED:
        text = t(
            lang, "notif_target",
            title=title_short,
            price=fmt_money(new_price, cur, lang),
            target=fmt_money(tracker.target_price, cur, lang),
            url=safe_url,
        )
    else:  # DECREASE
        diff = old_price - new_price if (old_price and new_price) else Decimal(0)
        pct = int(diff / old_price * 100) if old_price else 0
        text = t(
            lang, "notif_decrease",
            pct=pct,
            title=title_short,
            old=fmt_money(old_price, cur, lang),
            new=fmt_money(new_price, cur, lang),
            diff=fmt_money(diff, cur, lang),
            url=safe_url,
        )

    # Кнопка "🔕 Не сповіщати…" для саме цього типу події + "⚙️ Налаштування"
    from app.bot.keyboards import MUTE_KIND_BY_EVENT, notification_keyboard
    keyboard = notification_keyboard(lang, MUTE_KIND_BY_EVENT.get(event, "drop"), tracker.id)

    try:
        # Єдиний адресат — власник трекера (tracker.user_id → User.telegram_id)
        msg = await bot.send_message(
            chat_id=user.telegram_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=False,
            reply_markup=keyboard,
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
        if event == PriceEventType.RESTOCK:
            # Точка відліку для антиспаму RESTOCK (див. _suppression_reason)
            tracker.last_restock_notified_at = datetime.now(timezone.utc).replace(tzinfo=None)
        logger.info("notification_sent", event_type=str(event), user=user.telegram_id)

    except Exception as e:
        # Якщо юзер заблокував бота
        if "bot was blocked" in str(e).lower():
            user.bot_blocked = True          # не адмінське блокування — знімається, щойно юзер напише боту
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