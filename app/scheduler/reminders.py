"""
Разові нагадування перед кінцем торгів (аукціони, Violity).

Час закінчення записується при додаванні лота й оновлюється звичайними перевірками
(PriceTracker.ends_at, UTC). Тут сайт НЕ запитується: щохвилини дивимось у базу і шлемо
нагадування, коли до кінця лишилось ≤ remind_before_minutes (60 або 30) хвилин.

Гарантії (перевіряє tests/test_auction.py):
- кожен отримує лише нагадування про СВІЙ трекер на свій telegram_id;
- один раз на лот (ending_notified_at); якщо торги перенесли на >2 год — price_checker скидає позначку;
- нічого після кінця торгів (ends_at > now), нічого вимкненим (remind_before_minutes порожній/0),
  заблокованим адміном чи тим, хто заблокував бота, і трекерам не в статусі ACTIVE;
- після рестарту бота нагадування, вікно якого вже почалося, приходить з реальним залишком часу.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape as html_escape

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.database.models import Notification, NotificationStatus, PriceEventType, PriceTracker, TrackerStatus, User
from app.database.session import get_session
from app.i18n import fmt_money, t
from app.timeutil import fmt

logger = structlog.get_logger(__name__)

MAX_REMIND_MINUTES = 60


def due(tracker: PriceTracker, now: datetime) -> bool:
    """Чи час нагадувати (now — наївний UTC)."""
    if tracker.ends_at is None or tracker.ending_notified_at is not None:
        return False
    minutes = tracker.remind_before_minutes or 0
    if minutes <= 0 or tracker.status != TrackerStatus.ACTIVE:
        return False
    return tracker.ends_at - timedelta(minutes=minutes) <= now < tracker.ends_at


def render(tracker: PriceTracker, lang: str, now: datetime) -> str:
    left = max(1, int((tracker.ends_at - now).total_seconds() // 60))
    return t(
        lang, "notif_ending",
        left=t(lang, "minutes_short", n=left),
        title=html_escape((tracker.title or t(lang, "item"))[:60], quote=False),
        price=fmt_money(tracker.current_price, tracker.currency, lang) if tracker.current_price else t(lang, "price_unknown"),
        end=fmt(tracker.ends_at, "%d.%m %H:%M"),
        url=html_escape(tracker.canonical_url or "", quote=True),
    )


async def send_ending_reminders(bot, now: datetime | None = None) -> int:
    """Надсилає нагадування, час яких настав. Повертає кількість надісланих."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    sent = 0
    async with get_session() as s:
        rows = (await s.execute(
            select(PriceTracker, User)
            .join(User, User.id == PriceTracker.user_id)
            .where(
                PriceTracker.ends_at.isnot(None),
                PriceTracker.ending_notified_at.is_(None),
                PriceTracker.ends_at > now,
                PriceTracker.ends_at <= now + timedelta(minutes=MAX_REMIND_MINUTES),
                PriceTracker.status == TrackerStatus.ACTIVE,
                User.is_blocked.is_(False),
                User.bot_blocked.is_(False),
            )
        )).all()
        for tracker, user in rows:
            if not due(tracker, now):
                continue
            tracker.ending_notified_at = now          # раз на лот — навіть якщо відправка не вдалась
            lang = user.lang
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=t(lang, "btn_tracker_settings"), callback_data=f"tsettings:{tracker.id}"),
            ]])
            try:
                msg = await bot.send_message(
                    chat_id=user.telegram_id, text=render(tracker, lang, now),
                    parse_mode="HTML", reply_markup=keyboard,
                )
                status, err, mid = NotificationStatus.SENT, None, getattr(msg, "message_id", None)
                sent += 1
            except Exception as e:  # noqa: BLE001
                if "bot was blocked" in str(e).lower():
                    user.bot_blocked = True
                status, err, mid = NotificationStatus.FAILED, str(e)[:200], None
                logger.warning("ending_reminder_failed", tracker_id=tracker.id, error=str(e))
            s.add(Notification(
                user_id=user.id, tracker_id=tracker.id, event_type=PriceEventType.ENDING,
                old_price=None, new_price=tracker.current_price or Decimal(0), status=status,
                telegram_message_id=mid, error_message=err,
            ))
    if sent:
        logger.info("ending_reminders_sent", count=sent)
    return sent
