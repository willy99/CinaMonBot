"""
Планувальник перевірки цін на базі APScheduler.

Замість Celery (потребує Redis + окремий процес),
використовуємо APScheduler який працює в тому ж процесі що й бот.

Для 0-2000 юзерів це абсолютно достатньо.
Якщо треба більше — можна мігрувати на Celery пізніше без зміни логіки.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from html import escape as html_escape
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import structlog
from aiogram.exceptions import TelegramRetryAfter
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.config import settings
from app.scheduler import breaker
from botkit.ops import heartbeat
from app.services import runtime_settings as rt
from app.database.session import get_session
from app.database.models import (
    PriceTracker, PriceHistory, Notification,
    TrackerStatus, PriceEventType, NotificationStatus,
)
from app.services.scrapers import scraper as scraper_mod
from app.services.scrapers.scraper import ScraperFactory, ScrapeError
from app.services.scrapers.scrape_log import prune_scrape_log
from app.i18n import t, fmt_money, fmt_percent
from app.scheduler.digest import send_weekly_digests

logger = structlog.get_logger(__name__)

# Глобальний scheduler — один на весь процес
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")

# НОВЕ: максимум перевірок за один запуск — обмежує пікове навантаження
# Після кінця торгів ще кілька годин перевіряємо (фінальна ціна, продовження +5 хв), далі — ні
ENDED_AUCTION_GRACE = timedelta(hours=6)
# Після кінця торгів ще трохи перевіряємо часто: ставка наприкінці продовжує лот на +5 хв, і так ловимо результат
AUCTION_END_TAIL = timedelta(minutes=10)
# Тік планувальника — щохвилини, а перевірка займає секунди: без допуску «раз на 2 хв» ставало б «раз на 3»
DUE_SLACK = timedelta(seconds=30)
# Трекер того ж посилання, що вже перевіряємо, підтягуємо, якщо минуло ≥ цієї частки його інтервалу
SHARED_REFRESH_FRACTION = 0.5
# Telegram: ≤ 30 повідомлень/с на бота — тримаємо запас; на RetryAfter чекаємо й пробуємо ще раз
TELEGRAM_MIN_GAP_SECONDS = 1 / 25
TELEGRAM_MAX_RETRY_WAIT_SECONDS = 60

# Одне посилання — один запит за прогін: кілька юзерів можуть стежити за тією самою сторінкою.
# dispatch кладе сюди {canonical_url: Task} на час прогону; поза прогоном (None) — звичайний scrape.
_run_scrapes: ContextVar[dict[str, asyncio.Task] | None] = ContextVar("_run_scrapes", default=None)
MAX_CHECKS_PER_RUN = 200


def setup_scheduler(bot) -> None:
    """
    Реєструємо задачі і стартуємо scheduler.
    Викликається один раз при запуску бота.
    """
    # Налаштування «на льоту» (/settings): перечитуємо перекриття з бази — rt.cached() у боті бачить зміни за ~10 с
    scheduler.add_job(
        rt.refresh,
        trigger="interval",
        seconds=10,
        id="runtime_settings_refresh",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),
    )
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
        minutes=1,   # щохвилини: Premium-лоти під кінець торгів — раз на 2 хв; решта — за своїм інтервалом
        args=[bot],
        id="dispatch_price_checks",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
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
    # Нагадування перед кінцем торгів (аукціони): лише читання бази, сайт не запитується
    from app.scheduler.reminders import send_ending_reminders
    scheduler.add_job(
        send_ending_reminders,
        trigger="interval",
        minutes=1,
        args=[bot],
        id="ending_reminders",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("scheduler_started", interval_minutes=1, max_per_run=MAX_CHECKS_PER_RUN)


async def handle_expired_premiums(bot) -> None:
    """
    Знаходить юзерів у яких щойно закінчився Premium
    і призупиняє трекери понад ліміт безкоштовного плану.
    Не видаляє — лише ставить PAUSED.
    """
    from sqlalchemy import select
    from datetime import datetime, timezone
    from app.database.models import User, SubscriptionTier

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
            n=rt.cached("FREE_TIER_MAX_ITEMS"),
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

    await _pause_blocked_domain_trackers()

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
    naive_now = now.replace(tzinfo=None)
    # Межа на магазин: різних сторінок за хвилину (прогін = хвилина). Надлишок рівномірно переходить на наступні
    # хвилини, а не б'є сплеском. Черга — за простроченням (last_checked + інтервал), а не за давністю перевірки:
    # інакше годинні трекери Premium стоять за 6-годинними free і першими випадають за межу (навантажувальний тест).
    default_cap = int(rt.cached("DOMAIN_MAX_CHECKS_PER_MINUTE"))
    overrides = _domain_cap_overrides()
    eligible: list[tuple[PriceTracker, int, datetime | None]] = []
    for tracker in trackers:
        if tracker.source and breaker.is_open(tracker.source):
            continue                  # магазин тимчасово недоступний — last_checked_at не чіпаємо
        if tracker.ends_at is not None and tracker.ends_at < naive_now - ENDED_AUCTION_GRACE:
            continue                  # торги давно завершені, фінальна ціна вже є — не смикаємо сайт
        interval = _check_interval_minutes(tracker, naive_now)
        last = tracker.last_checked_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        eligible.append((tracker, interval, last))
    eligible.sort(key=lambda e: (e[2] is not None, e[2] + timedelta(minutes=e[1]) if e[2] else now))

    to_check: list[PriceTracker] = []
    chosen: set[str] = set()
    urls_by_domain: dict[str, set[str]] = {}
    deferred = 0
    for tracker, interval, last in eligible:
        if last is not None and now < last + timedelta(minutes=interval) - DUE_SLACK:
            break                     # відсортовано за терміном — далі нікому ще не час
        urls = urls_by_domain.setdefault(tracker.source or "", set())
        if tracker.canonical_url not in urls and len(urls) >= overrides.get(tracker.source or "", default_cap):
            deferred += 1
            continue                  # межа магазину на цю хвилину — візьмемо наступним тіком
        urls.add(tracker.canonical_url)
        chosen.add(tracker.canonical_url)
        to_check.append(tracker)

        # НОВЕ: зупиняємось як тільки набрали ліміт
        # Решта трекерів дочекається наступного запуску (щохвилини)
        if len(to_check) >= MAX_CHECKS_PER_RUN:
            break

    # Сторінку однаково завантажуємо — тож оновлюємо й інших, хто за нею стежить і пройшов хоч пів інтервалу.
    # Запиту це не додає, а з часом трекери одного товару сходяться в один запит (тест на 1000 юзерах: −9 % запитів
    # уже за 15 хв) і падіння ціни на популярному товарі доходить до більшості одразу, а не впродовж 6 годин.
    picked = {t.id for t in to_check}
    joined = 0
    for tracker, interval, last in eligible:
        if len(to_check) >= MAX_CHECKS_PER_RUN:
            break
        if tracker.id in picked or tracker.canonical_url not in chosen:
            continue
        if last is not None and now < last + timedelta(minutes=interval * SHARED_REFRESH_FRACTION):
            continue
        to_check.append(tracker)
        joined += 1

    if not to_check:
        logger.info("dispatch_nothing_to_check", total=len(trackers))
        return

    logger.info("dispatch_checking", count=len(to_check), total=len(trackers), deferred_by_shop_cap=deferred,
                joined_same_url=joined)

    # Магазин за магазином паралельно, а всередині магазину — по черзі: запити до одного домену однаково йдуть
    # по одному (DomainRateLimiter). Коли всі трекери кидали в один gather, перевірки, що чекали паузи Rozetka
    # (чи 30-секундного відступу після 429), займали всі 5 слотів, і інші магазини стояли: прогін тягнувся 70–120 с
    # замість 20–30, планувальник пропускав тіки (навантажувальний тест).
    semaphore = asyncio.Semaphore(5)
    by_domain: dict[str, list[PriceTracker]] = {}
    for tracker in to_check:
        by_domain.setdefault(tracker.source or tracker.id, []).append(tracker)

    async def check_domain(trackers: list[PriceTracker]) -> None:
        for tracker in trackers:
            if tracker.source and breaker.is_open(tracker.source):
                # Магазин щойно відповів 429/антиботом і став на паузу — решту його сторінок цього прогону не
                # смикаємо (кожна знову ловила б 429). last_checked_at не чіпаємо.
                logger.info("dispatch_domain_paused_mid_run", domain=tracker.source,
                            skipped=len(trackers) - trackers.index(tracker))
                return
            async with semaphore:
                try:
                    await check_single_price(tracker, bot)
                except Exception as e:
                    logger.error("tracker_check_failed", tracker_id=tracker.id, error=str(e))

    token = _run_scrapes.set({})
    batch = scraper_mod.in_batch.set(True)
    try:
        await asyncio.gather(*[check_domain(ts) for ts in by_domain.values()])
    finally:
        scraper_mod.in_batch.reset(batch)
        _run_scrapes.reset(token)
    await _update_domain_blocks({t.source for t in to_check if t.source})
    logger.info("dispatch_finished", checked=len(to_check))


# Магазин «стабільно блокує», якщо за вікно було ≥ MIN спроб, жодної вдалої, і всі невдачі — «blocked»
# (антибот / 403 від магазину). 404 / зміна сторінки — проблема окремого товару; мережа / браузер / таймаут —
# наш бік (20.10 вночі зникав інтернет), такі збої магазин не блокують.
DOMAIN_BLOCK_WINDOW = timedelta(hours=24)
DOMAIN_BLOCK_MIN_ATTEMPTS = 5
DOMAIN_BLOCK_REASONS = frozenset({"blocked"})


async def _update_domain_blocks(domains: set[str], now: datetime | None = None) -> None:
    """
    Після перевірок: магазин, що стабільно нас блокує, — у domain_blocks (адмінка показує «заблоковано»),
    його трекери — PAUSED_DOMAIN. Адміну в Telegram нічого не шлемо (14.10: технічні алерти прибрано).
    """
    from collections import Counter
    from app.database.models import DomainBlock, ScrapeLog
    now = now or datetime.utcnow()
    for domain in sorted(domains):
        async with get_session() as session:
            if await session.get(DomainBlock, domain) is not None:
                continue
            rows = (await session.execute(
                select(ScrapeLog.ok, ScrapeLog.reason)
                .where(ScrapeLog.domain == domain, ScrapeLog.created_at >= now - DOMAIN_BLOCK_WINDOW)
            )).all()
            if (len(rows) < DOMAIN_BLOCK_MIN_ATTEMPTS or any(ok for ok, _ in rows)
                    or not all(reason in DOMAIN_BLOCK_REASONS for _, reason in rows)):
                continue
            reason = Counter(r for _, r in rows).most_common(1)[0][0]
            session.add(DomainBlock(domain=domain, reason=reason, blocked_at=now))
        paused = await _pause_blocked_domain_trackers({domain})
        logger.warning("domain_blocked", domain=domain, reason=reason, attempts=len(rows), paused=paused)


async def _pause_blocked_domain_trackers(domains: set[str] | None = None) -> int:
    """ACTIVE-трекери заблокованих магазинів → PAUSED_DOMAIN (і ті, що додали вже після блокування)."""
    from sqlalchemy import update
    from app.database.models import DomainBlock
    async with get_session() as session:
        if domains is None:
            domains = set((await session.execute(select(DomainBlock.domain))).scalars().all())
        if not domains:
            return 0
        res = await session.execute(
            update(PriceTracker)
            .where(PriceTracker.source.in_(domains), PriceTracker.status == TrackerStatus.ACTIVE)
            .values(status=TrackerStatus.PAUSED_DOMAIN)
        )
    return res.rowcount or 0


def _check_interval_minutes(tracker: PriceTracker, now: datetime) -> int:
    """
    Як часто перевіряти трекер (хв). Premium-лот за AUCTION_END_WINDOW_MINUTES до кінця торгів і ще
    AUCTION_END_TAIL після — раз на AUCTION_END_INTERVAL_MINUTES (ставка наприкінці продовжує лот на +5 хв;
    ends_at оновлюється при кожній перевірці). Решта — Premium / Free інтервал, магазини без змін.
    now — naive UTC, як ends_at у базі.
    """
    premium = tracker.user.is_premium
    if premium and tracker.ends_at is not None and tracker.is_available:
        window = timedelta(minutes=rt.cached("AUCTION_END_WINDOW_MINUTES"))
        if tracker.ends_at - window <= now < tracker.ends_at + AUCTION_END_TAIL:
            return int(rt.cached("AUCTION_END_INTERVAL_MINUTES"))
    return int(rt.cached("PREMIUM_CHECK_INTERVAL_MINUTES" if premium else "FREE_TIER_CHECK_INTERVAL_MINUTES"))


def _domain_cap_overrides() -> dict[str, int]:
    """DOMAIN_CAP_OVERRIDES="rozetka.com.ua=12,prom.ua=10" → {домен: межа}; кривий запис пропускаємо."""
    out: dict[str, int] = {}
    for part in (settings.DOMAIN_CAP_OVERRIDES or "").split(","):
        domain, _, value = part.partition("=")
        try:
            if domain.strip() and int(value) > 0:
                out[domain.strip().lower()] = int(value)
        except ValueError:
            logger.warning("bad_domain_cap_override", entry=part.strip()[:60])
    return out


async def _scrape_once_per_run(scraper, url: str):
    """У межах прогону dispatch — один запит на посилання (результат спільний для всіх юзерів)."""
    shared = _run_scrapes.get()
    if shared is None:
        return await scraper.scrape(url)
    task = shared.get(url)
    if task is None:
        task = shared[url] = asyncio.ensure_future(scraper.scrape(url))
    return await asyncio.shield(task)


async def check_single_price(tracker: PriceTracker, bot) -> None:
    """Перевіряє ціну одного товару і відправляє сповіщення якщо треба."""

    scraper = ScraperFactory.get(tracker.canonical_url)
    if not scraper:
        return

    result = await _scrape_once_per_run(scraper, tracker.canonical_url)
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
        # Аукціон: час закінчення може зсуватися (+5 хв за ставку наприкінці, переподання лота)
        new_end = _naive_utc(getattr(result, "ends_at", None))
        if new_end is not None:
            old_end = db_tracker.ends_at
            if old_end is not None and db_tracker.ending_notified_at is not None and new_end - old_end > timedelta(hours=2):
                db_tracker.ending_notified_at = None      # торги перенесли далеко — нагадаємо ще раз
            db_tracker.ends_at = new_end

        # Аукціон: результат торгів. Продано — далі перевіряти нічого: пауза (звільняє слот), у /list
        # «Лот продано». Не продано — стежимо далі: лот можуть перевиставити (тоді outcome знову None)
        if _is_auction(db_tracker):
            db_tracker.lot_outcome = getattr(result, "lot_outcome", None)
            if db_tracker.lot_outcome == "sold" and db_tracker.status == TrackerStatus.ACTIVE:
                db_tracker.status = TrackerStatus.PAUSED
                logger.info("auction_lot_sold_paused", tracker_id=db_tracker.id, price=str(result.price))

        # Пишемо в історію
        if result.price is not None:
            session.add(PriceHistory(
                tracker_id=db_tracker.id,
                price=result.price,
                is_available=result.is_available,
            ))

        # Чи вже сповіщали про досягнуту ціль — лише коли ціна зараз за межею цілі (інакше не потрібно)
        target_notified = False
        if db_tracker.target_price and result.price:
            past = (result.price >= db_tracker.target_price if db_tracker.target_direction == "above"
                    else result.price <= db_tracker.target_price)
            if past:
                target_notified = await _target_already_notified(session, db_tracker.id)

        # Визначаємо тип події
        event = _detect_event(
            old_price=old_price,
            new_price=result.price,
            target_price=db_tracker.target_price,
            old_available=old_available,
            new_available=result.is_available,
            target_notified=target_notified,
            target_direction=db_tracker.target_direction,
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
    - "muted_increase"  — подорожчання, а юзер не вмикав сповіщення про нього (за замовчуванням вимкнено)
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
            if now - last < timedelta(days=rt.cached("RESTOCK_COOLDOWN_DAYS")) and not cheaper:
                return "restock_cooldown"
        return None

    if event in (PriceEventType.DECREASE, PriceEventType.TARGET_REACHED):
        if not tracker.notify_drop:
            return "muted_drop"
    if event == PriceEventType.INCREASE and not tracker.notify_increase:
        return "muted_increase"
    if event == PriceEventType.DECREASE and _is_auction(tracker):
        return "auction_no_generic_drop"      # на аукціоні — лише ціль, яку юзер задав сам
    return None


async def _target_already_notified(session, tracker_id: str) -> bool:
    """Чи надсилали (успішно) по трекеру сповіщення TARGET_REACHED."""
    row = await session.execute(
        select(Notification.id).where(
            Notification.tracker_id == tracker_id,
            Notification.event_type == PriceEventType.TARGET_REACHED,
            Notification.status == NotificationStatus.SENT,
        ).limit(1)
    )
    return row.first() is not None


def _is_auction(tracker: PriceTracker) -> bool:
    from app.services.scrapers.registry import platform_for
    p = platform_for(tracker.source or "")
    return bool(p and p.auction)


def _naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _detect_event(
    old_price: Decimal | None,
    new_price: Decimal | None,
    target_price: Decimal | None,
    old_available: bool,
    new_available: bool,
    target_notified: bool = False,
    target_direction: str | None = None,
) -> PriceEventType | None:
    """
    Визначає чи потрібно сповіщення і якого типу.

    Ціль: below (типово) — ціна ≤ цілі; above (аукціони) — ціна ≥ цілі.
    target_notified — по трекеру вже надсилали TARGET_REACHED. Тоді ціль знову сповіщаємо лише
    коли ціна щойно перетнула межу (була по інший бік), а не на кожній перевірці, поки ціна за межею.
    """

    # Товар знову з'явився
    if not old_available and new_available:
        return PriceEventType.RESTOCK

    if target_price and new_price:
        if target_direction == "above":
            if new_price >= target_price:
                crossed = old_price is not None and old_price < target_price
                if crossed or not target_notified:
                    return PriceEventType.TARGET_REACHED
        # Ціна досягла цільової
        elif new_price <= target_price:
            crossed = old_price is not None and old_price > target_price
            if crossed or not target_notified:
                return PriceEventType.TARGET_REACHED

    # Ціна змінилась щонайменше на 3%: впала — DECREASE (крім цілі «перевищить»), зросла — INCREASE
    # (його надсилаємо лише з увімкненим notify_increase — див. _suppression_reason)
    if old_price and new_price and new_price != old_price:
        change_pct = (new_price - old_price) / old_price * 100
        if change_pct <= -3 and target_direction != "above":
            return PriceEventType.DECREASE
        if change_pct >= 3:
            return PriceEventType.INCREASE

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
    elif event == PriceEventType.TARGET_REACHED and tracker.target_direction == "above":
        # Аукціон: ставки перейшли межу, яку юзер задав
        text = t(
            lang, "notif_target_above",
            title=title_short,
            price=fmt_money(new_price, cur, lang),
            target=fmt_money(tracker.target_price, cur, lang),
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
    elif event == PriceEventType.INCREASE:
        diff = new_price - old_price if (old_price and new_price) else Decimal(0)
        text = t(
            lang, "notif_increase_auction" if _is_auction(tracker) else "notif_increase",
            pct=int(diff / old_price * 100) if old_price else 0,
            title=title_short,
            old=fmt_money(old_price, cur, lang),
            new=fmt_money(new_price, cur, lang),
            diff=fmt_money(diff, cur, lang),
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
    # Ціль досягнута — одразу запропонувати нову межу (ставка) / ціль (ціна) в тому ж напрямку
    retarget = None
    if event == PriceEventType.TARGET_REACHED:
        retarget = "above" if tracker.target_direction == "above" else "below"
    keyboard = notification_keyboard(lang, MUTE_KIND_BY_EVENT.get(event, "drop"), tracker.id, retarget=retarget)

    # Ціну й історію фіксуємо до походу в Telegram: інакше запис у SQLite тримає блокування бази, поки йде
    # мережевий запит (і всю паузу RetryAfter), а інші перевірки чекають його до busy_timeout
    await session.commit()
    try:
        # Єдиний адресат — власник трекера (tracker.user_id → User.telegram_id)
        msg = await _paced(lambda: bot.send_message(
            chat_id=user.telegram_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=False,
            reply_markup=keyboard,
        ))
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

_tg_lock = asyncio.Lock()
_tg_last_sent = 0.0


async def _paced(request):
    """Запит до Telegram (request() — одна відправка) не швидше за ~25/с на весь процес; на TelegramRetryAfter — чекаємо скільки сказали й ще раз.

    Без цього сплеск сповіщень (падіння ціни на популярному товарі) упирається в ліміт Telegram 30/с, і зайві
    повідомлення ставали FAILED — юзер їх так і не отримував.
    """
    global _tg_last_sent
    loop = asyncio.get_running_loop()
    for attempt in range(2):
        async with _tg_lock:
            wait = _tg_last_sent + TELEGRAM_MIN_GAP_SECONDS - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            _tg_last_sent = loop.time()
        try:
            return await request()
        except TelegramRetryAfter as e:
            if attempt or e.retry_after > TELEGRAM_MAX_RETRY_WAIT_SECONDS:
                raise
            logger.warning("telegram_retry_after", seconds=e.retry_after)
            await asyncio.sleep(e.retry_after)
