"""
Щотижневий дайджест "що подешевшало за тиждень".

Правила (побажання власника):
- Отримують лише юзери з User.digest_enabled=True і не заблоковані; кожен — РІВНО одне
  повідомлення на свій telegram_id і лише зі своїми трекерами.
- Для кожного ACTIVE-трекера порівнюємо поточну ціну з ціною ~7 днів тому
  (останній запис PriceHistory на момент now−7д або раніше; якщо такого немає —
  найперший запис за останні 7 днів). Трекери з < 2 записами історії пропускаємо.
- У дайджест ідуть лише ті, що ПОДЕШЕВШАЛИ: сортування за % падіння, топ-5.
  Подорожчання — одним рядком-підсумком. Мінімум за 30 днів — у кожному рядку.
- Тихий тиждень (нічого не подешевшало) → повідомлення НЕ надсилається взагалі.
- last_digest_at ставимо після успішної відправки; повторно не шлемо раніше ніж через
  DIGEST_GUARD_DAYS днів (захист від повторного спрацювання після рестарту).

    text = await build_digest(user)            # None → нічого слати
    sent = await send_weekly_digests(bot)      # кількість надісланих
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from html import escape as html_escape

import structlog
from sqlalchemy import select

from app.bot.keyboards import digest_keyboard
from app.database.models import PriceHistory, PriceTracker, TrackerStatus, User
from app.database.session import get_session
from app.i18n import fmt_money, fmt_number, fmt_percent, t

logger = structlog.get_logger(__name__)

DIGEST_TOP_N = 5            # скільки найбільших падінь показувати
DIGEST_WINDOW_DAYS = 7      # "за тиждень"
DIGEST_MIN_WINDOW_DAYS = 30 # вікно для "мін. за 30 дн."
DIGEST_GUARD_DAYS = 6       # не слати повторно раніше ніж через стільки днів


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class DigestItem:
    title: str
    currency: str
    old_price: Decimal
    new_price: Decimal
    drop_pct: Decimal
    min_30d: Decimal


@dataclass
class DigestData:
    drops: list[DigestItem] = field(default_factory=list)   # уже відсортовані, уже обрізані до топ-N
    ups: int = 0                                             # скільки подорожчало


def _reference_price(history: list[PriceHistory], since: datetime) -> Decimal | None:
    """
    Ціна "тиждень тому": останній запис з recorded_at <= since; якщо такого немає —
    найперший запис після since. history має бути відсортована за recorded_at ASC.
    """
    before = [h for h in history if h.recorded_at is not None and h.recorded_at <= since]
    if before:
        return Decimal(before[-1].price)
    after = [h for h in history if h.recorded_at is not None and h.recorded_at > since]
    return Decimal(after[0].price) if after else None


def _pct_drop(old: Decimal, new: Decimal) -> Decimal:
    return ((old - new) / old * Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


async def collect_digest(user_id: str, now: datetime | None = None) -> DigestData:
    """Збирає дані дайджесту для юзера (без тексту). drops порожній → тиждень тихий."""
    now = now or _now_naive()
    week_ago = now - timedelta(days=DIGEST_WINDOW_DAYS)
    month_ago = now - timedelta(days=DIGEST_MIN_WINDOW_DAYS)

    async with get_session() as session:
        trackers = (await session.execute(
            select(PriceTracker).where(
                PriceTracker.user_id == user_id,
                PriceTracker.status == TrackerStatus.ACTIVE,
            )
        )).scalars().all()
        if not trackers:
            return DigestData()
        rows = (await session.execute(
            select(PriceHistory)
            .where(PriceHistory.tracker_id.in_([tr.id for tr in trackers]))
            .order_by(PriceHistory.recorded_at.asc())
        )).scalars().all()

    by_tracker: dict[str, list[PriceHistory]] = {}
    for h in rows:
        by_tracker.setdefault(h.tracker_id, []).append(h)

    drops: list[DigestItem] = []
    ups = 0
    for tr in trackers:
        history = by_tracker.get(tr.id, [])
        if len(history) < 2:
            continue
        old = _reference_price(history, week_ago)
        new = Decimal(tr.current_price) if tr.current_price is not None else Decimal(history[-1].price)
        if old is None or old <= 0:
            continue
        if new > old:
            ups += 1
            continue
        if new >= old:
            continue
        recent = [Decimal(h.price) for h in history if h.recorded_at is not None and h.recorded_at >= month_ago]
        min_30d = min(recent + [new])
        drops.append(DigestItem(
            title=(tr.title or "")[:60],
            currency=(tr.currency or "UAH").upper(),
            old_price=old, new_price=new,
            drop_pct=_pct_drop(old, new),
            min_30d=min_30d,
        ))

    drops.sort(key=lambda d: (d.drop_pct, d.old_price - d.new_price), reverse=True)
    return DigestData(drops=drops[:DIGEST_TOP_N], ups=ups)


def render_digest(data: DigestData, lang: str) -> str | None:
    """Текст дайджесту (HTML) або None, якщо нічого не подешевшало."""
    if not data.drops:
        return None
    lines = [t(lang, "digest_header")]
    for d in data.drops:
        lines.append(t(
            lang, "digest_item",
            title=html_escape(d.title or t(lang, "item")),
            old=fmt_number(d.old_price, lang),
            new=fmt_money(d.new_price, d.currency, lang),
            pct=fmt_percent(d.drop_pct, lang),
            min=fmt_number(d.min_30d, lang),
        ))
    if data.ups:
        lines.append(t(lang, "digest_ups", n=data.ups))
    lines.append(t(lang, "digest_hint"))
    return "\n".join(lines)


async def build_digest(user: User, now: datetime | None = None) -> str | None:
    """Готовий текст дайджесту для юзера мовою юзера; None → цього тижня слати нічого."""
    data = await collect_digest(user.id, now=now)
    return render_digest(data, user.lang)


def _within_guard(last: datetime | None, now: datetime) -> bool:
    if last is None:
        return False
    if last.tzinfo is not None:
        last = last.astimezone(timezone.utc).replace(tzinfo=None)
    return now - last < timedelta(days=DIGEST_GUARD_DAYS)


async def send_weekly_digests(bot, now: datetime | None = None) -> int:
    """
    Шле дайджест кожному юзеру, що на нього підписаний (digest_enabled) і не заблокований.
    Адресат завжди user.telegram_id власника даних; тихий тиждень → нічого.
    Помилка в одного юзера не зупиняє решту; "bot was blocked" → bot_blocked=True.
    Повертає кількість надісланих повідомлень.
    """
    now = now or _now_naive()
    async with get_session() as session:
        users = (await session.execute(
            select(User).where(User.digest_enabled.is_(True), User.is_blocked.is_(False), User.bot_blocked.is_(False))
        )).scalars().all()

    sent = 0
    for user in users:
        if _within_guard(user.last_digest_at, now):
            logger.info("digest_skipped_recent", telegram_id=user.telegram_id, last=str(user.last_digest_at))
            continue
        try:
            text = await build_digest(user, now=now)
        except Exception as e:  # noqa: BLE001 — зламані дані одного юзера не валять розсилку
            logger.error("digest_build_failed", telegram_id=user.telegram_id, error=str(e))
            continue
        if not text:
            continue
        try:
            # Єдиний адресат — сам юзер (user.telegram_id), лише його трекери, лише за згодою (digest_enabled)
            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=digest_keyboard(user.lang),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("digest_send_failed", telegram_id=user.telegram_id, error=str(e))
            if "bot was blocked" in str(e).lower():
                async with get_session() as session:
                    db_user = await session.get(User, user.id)
                    if db_user:
                        db_user.bot_blocked = True
            continue
        async with get_session() as session:
            db_user = await session.get(User, user.id)
            if db_user:
                db_user.last_digest_at = now
        sent += 1
        logger.info("digest_sent", telegram_id=user.telegram_id)

    logger.info("digest_finished", users=len(users), sent=sent)
    return sent
