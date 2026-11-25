"""
Bot handlers — чиста версія.

Головне правило:
  event.from_user  → завжди реальна людина (і для Message, і для CallbackQuery)
  msg.from_user    → НЕ використовуємо для отримання юзера,
                     бо для callback msg — це повідомлення бота

Локалізація:
  lang = user.lang                    → "uk" | "en"
  t(lang, "key", **kw)                → текст
  fmt_money(amount, currency, lang)   → "1 234 грн" / "$12.99"
  Адмінські повідомлення (stats, activate, broadcast, feedback-форвард)
  не локалізуються — завжди українською.
"""
from __future__ import annotations

import re
from contextlib import suppress
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from html import escape as html_escape
import structlog

from app.bot.keyboards import (
    NOTIFY_FIELDS, digest_toggle_button, notification_keyboard, tracker_settings_keyboard,
)
from app.config import settings
from app.services import runtime_settings as rt
from app.database.session import get_session
from app.database.models import Feedback, PriceTracker, TrackerStatus, User
from app.i18n import LANGUAGES, country_label, fmt_date, fmt_money, fmt_percent, t
from app.bot.links import urls_from_message
from app.services.limits import extend_premium, sync_free_limit
from app.timeutil import now_kyiv
from app.services.scrapers.registry import PLATFORMS, platform_for, verified_platforms
from app.services.scrapers.scraper import ScraperFactory, ScrapeError

logger = structlog.get_logger(__name__)
router = Router(name="main")


class AddTrackerStates(StatesGroup):
    waiting_for_url = State()
    waiting_for_target_price = State()

@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel_add")
async def cmd_cancel(event: Message | CallbackQuery, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    msg = event.message if isinstance(event, CallbackQuery) else event
    if not msg or not event.from_user:
        return
    if current:
        user = await get_or_create_user(event.from_user)
        await msg.answer(t(user.lang, "cancelled"), reply_markup=None)
    if isinstance(event, CallbackQuery):
        await event.answer()

# ─── DB helpers ──────────────────────────────────────────────────

async def get_or_create_user(from_user, source: str | None = None) -> User:
    """Завжди передавай event.from_user — там реальна людина. source — лише для нового юзера (?start=<джерело>)."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    async with get_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == from_user.id)
        )).scalar_one_or_none()

        if not user:
            try:
                user = User(
                    telegram_id=from_user.id,
                    username=getattr(from_user, "username", None),
                    first_name=getattr(from_user, "first_name", None),
                    language_code=getattr(from_user, "language_code", None) or "uk",
                    source=source,
                )
                session.add(user)
                await session.flush()
                logger.info("new_user", telegram_id=from_user.id, source=source)
            except IntegrityError:
                # Race condition: інший запит вже створив юзера — просто завантажуємо
                await session.rollback()
                user = (await session.execute(
                    select(User).where(User.telegram_id == from_user.id)
                )).scalar_one()
        if user.bot_blocked:
            user.bot_blocked = False         # написав боту — отже, розблокував; сповіщення знову йдуть
        return user


async def count_trackers(user_id: str) -> int:
    """Усі товари юзера, крім помилкових (для "Залишилось: N")."""
    from sqlalchemy import select, func
    async with get_session() as session:
        return (await session.execute(
            select(func.count()).where(
                PriceTracker.user_id == user_id,
                PriceTracker.status != TrackerStatus.ERROR,
            )
        )).scalar_one()


async def count_active(user_id: str) -> int:
    """Скільки товарів реально перевіряється (ACTIVE)."""
    from sqlalchemy import select, func
    async with get_session() as session:
        return (await session.execute(
            select(func.count()).where(
                PriceTracker.user_id == user_id,
                PriceTracker.status == TrackerStatus.ACTIVE,
            )
        )).scalar_one()


async def get_trackers(user_id: str, include_paused: bool = False,
                       include_errors: bool = False) -> list[PriceTracker]:
    """include_errors — лише для /list: позиції з помилкою теж видно (в кінці), щоб вони не зникали мовчки."""
    from sqlalchemy import select
    async with get_session() as session:
        if include_paused:
            # Для /list і /delete — показуємо всі крім ERROR (для /list — і ERROR, в кінці)
            stmt = (
                select(PriceTracker)
                .where(PriceTracker.user_id == user_id,
                       *(() if include_errors else (PriceTracker.status != TrackerStatus.ERROR,)))
                .order_by((PriceTracker.status == TrackerStatus.ERROR).asc(),  # ERROR — останні
                          PriceTracker.status.asc(),  # ACTIVE перші, PAUSED після
                          PriceTracker.created_at.asc())
            )
        else:
            stmt = (
                select(PriceTracker)
                .where(PriceTracker.user_id == user_id,
                       PriceTracker.status == TrackerStatus.ACTIVE)
                .order_by(PriceTracker.created_at.desc())
            )
        return (await session.execute(stmt)).scalars().all()


def _main_menu(lang: str, is_premium: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=t(lang, "btn_add"), callback_data="add_tracker")],
        [
            InlineKeyboardButton(text=t(lang, "btn_list"), callback_data="my_list"),
            InlineKeyboardButton(text=t(lang, "btn_delete"), callback_data="delete_menu"),
        ],
    ]
    if not is_premium:
        buttons.append(
            [InlineKeyboardButton(text=t(lang, "btn_buy_premium"), callback_data="premium_info")]
        )
    buttons.append(
        [InlineKeyboardButton(text=t(lang, "btn_language"), callback_data="lang_menu")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─── /start ──────────────────────────────────────────────────────

@router.message(CommandStart())
def start_payload(text: str | None) -> str:
    """'/start wl_abc' → 'wl_abc'; лише символи, дозволені Telegram для deep link (A-Z a-z 0-9 _ -)."""
    parts = (text or "").split(maxsplit=1)
    if len(parts) < 2:
        return ""
    payload = parts[1].strip()
    return payload if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload) else ""


async def cmd_start(message: Message) -> None:
    if not message.from_user:
        return
    payload = start_payload(message.text)
    if payload.startswith("wl_"):
        # Хтось поділився своїм списком бажань — показуємо його замість привітання
        from app.bot.handlers.wishlist import show_shared_list
        viewer = await get_or_create_user(message.from_user, source="wishlist")
        await show_shared_list(message, viewer, payload[3:])
        return
    user = await get_or_create_user(message.from_user, source=payload[:32] or None)
    lang = user.lang
    name = message.from_user.first_name or t(lang, "default_name")

    # Статус плану: або активний Premium, або опис тарифів
    if user.is_premium:
        plan_block = t(lang, "start_premium_active")
    else:
        plan_block = t(
            lang, "plans",
            n=rt.cached("FREE_TIER_MAX_ITEMS"),
            price=rt.cached("PREMIUM_PRICE_UAH"),
        )

    await message.answer(
        f"{t(lang, 'start_greeting', name=name)}\n\n"
        f"{t(lang, 'start_intro')}\n\n"
        f"{plan_block}\n\n"
        f"{t(lang, 'start_links')}\n\n"
        f"{t(lang, 'contacts')}",
        reply_markup=_main_menu(lang, user.is_premium),
        parse_mode="HTML",
    )


# ─── /language ───────────────────────────────────────────────────

@router.message(Command("language"))
@router.callback_query(F.data == "lang_menu")
async def cmd_language(event: Message | CallbackQuery) -> None:
    msg = event.message if isinstance(event, CallbackQuery) else event
    if not msg or not event.from_user:
        return
    user = await get_or_create_user(event.from_user)

    buttons = [
        [InlineKeyboardButton(text=title, callback_data=f"set_lang:{code}")]
        for code, title in LANGUAGES.items()
    ]
    await msg.answer(
        t(user.lang, "language_prompt"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data.startswith("set_lang:"))
async def set_language(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    code = callback.data.split(":", 1)[1]
    if code not in LANGUAGES:
        await callback.answer()
        return

    from sqlalchemy import select
    async with get_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )).scalar_one_or_none()
        if not user:
            await callback.answer()
            return
        user.language_code = code
        is_premium = user.is_premium

    logger.info("language_changed", telegram_id=callback.from_user.id, lang=code)
    await callback.message.edit_text(
        t(code, "language_set"),
        reply_markup=_main_menu(code, is_premium),
        parse_mode="HTML",
    )
    await callback.answer()


# ─── /add ────────────────────────────────────────────────────────

# ScrapeError.reason → ключ i18n. Решта (network, timeout, max_retries_exceeded…) → fetch_failed
_SCRAPE_ERROR_KEYS = {
    "blocked": "err_blocked",
    "not_found": "err_not_found",
    "parse_failed": "err_parse_failed",
    "forbidden_target": "err_forbidden_target",
}

# Домени, про які адміна вже повідомили як про новий сайт (скидається з рестартом процесу)
_NEW_SITE_NOTIFIED: set[str] = set()


def _norm_host(host: str) -> str:
    host = (host or "").lower().strip()
    return host[4:] if host.startswith("www.") else host


async def _notify_admin_new_site(bot, domain: str, strategy: str, platform) -> None:
    """Сайт поза реєстром або зі статусом experimental уперше спарсився — одна нотатка адміну."""
    if platform is not None and platform.status != "experimental":
        return
    if not bot or not settings.ADMIN_TELEGRAM_ID or not domain or domain in _NEW_SITE_NOTIFIED:
        return
    _NEW_SITE_NOTIFIED.add(domain)
    label = "експериментальний" if platform is not None else "новий"
    try:
        await bot.send_message(
            chat_id=int(settings.ADMIN_TELEGRAM_ID),
            text=f"🆕 Новий сайт у трекерах: {domain} (strategy={strategy or '?'}, {label})",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("new_site_notify_failed", domain=domain, error=str(e))


@router.message(Command("add"))
@router.callback_query(F.data == "add_tracker")
async def cmd_add(event: Message | CallbackQuery, state: FSMContext) -> None:
    msg = event.message if isinstance(event, CallbackQuery) else event
    if not msg or not event.from_user:
        return
    user = await get_or_create_user(event.from_user)
    lang = user.lang

    await msg.answer(
        t(lang, "add_prompt"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "btn_cancel"), callback_data="cancel_add")]
        ]),
        parse_mode="HTML",
    )
    await state.set_state(AddTrackerStates.waiting_for_url)
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.message(AddTrackerStates.waiting_for_url)
async def process_url(message: Message, state: FSMContext) -> None:
    if not (message.text or message.caption) or not message.from_user:
        return

    user = await get_or_create_user(message.from_user)
    lang = user.lang

    # Посилання з будь-якого тексту: «Поділитися» з застосунку магазину дає «назва + посилання»
    urls = urls_from_message(message)
    if len(urls) > 1:
        await state.clear()
        from app.bot.bulk import bulk_add
        await bulk_add(message, user, urls)
        return
    url = urls[0] if urls else (message.text or message.caption or "").strip()
    scraper = ScraperFactory.get(url) if urls else None
    if not scraper:
        # Універсальний парсер приймає будь-який http(s) URL — None лише для не-посилань
        await message.answer(t(lang, "not_a_url"))
        return

    host = _norm_host(urlparse(url).netloc)
    platform = platform_for(host)
    # Відомі проблемні сайти — кажемо одразу, не ходимо за сторінкою
    if platform and platform.status == "needs_js":
        await message.answer(t(lang, "site_needs_js", name=platform.name), parse_mode="HTML")
        return
    if platform and platform.status == "blocked":
        await message.answer(t(lang, "site_blocked", name=platform.name), parse_mode="HTML")
        return

    # Перевірка ліміту
    count = await count_trackers(user.id)
    '''
    if not user.is_premium and count >= rt.cached("FREE_TIER_MAX_ITEMS"):
        await message.answer(
            f"⚠️ <b>Ліміт досягнуто!</b>\n\n"
            f"У тебе {count}/{rt.cached("FREE_TIER_MAX_ITEMS")} товарів на безкоштовному плані.\n"
            f"Перейди на Premium: /premium",
            parse_mode="HTML",
        )
        await state.clear()
        return
    '''
    wait = await message.answer(t(lang, "checking"))
    result = await scraper.scrape(url)

    if isinstance(result, ScrapeError):
        key = _SCRAPE_ERROR_KEYS.get(result.reason, "fetch_failed")
        await wait.edit_text(t(lang, key), parse_mode="HTML")
        return

    domain = getattr(scraper, "domain", "") or host
    # Новий або експериментальний сайт спрацював — повідомляємо адміна (раз на домен за процес)
    await _notify_admin_new_site(message.bot, domain, getattr(result, "strategy", ""), platform)

    currency = (result.currency or "UAH").upper()
    price_text = fmt_money(result.price, currency, lang) if result.price else t(lang, "price_unknown")
    avail = t(lang, "in_stock") if result.is_available else t(lang, "out_of_stock")

    if platform is not None and platform.auction:
        await _ask_auction_direction(wait, state, lang, url, result, domain, currency, price_text)
        return

    await state.update_data(
        url=url,
        canonical_url=result.canonical_url,
        source=domain,
        title=result.title,
        price=str(result.price) if result.price else None,
        currency=currency,
    )
    await wait.edit_text(
        t(lang, "ask_target", title=html_escape(result.title or "", quote=False), price=price_text, avail=avail, currency=currency),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "btn_skip"), callback_data="skip_target")]
        ]),
        parse_mode="HTML",
    )
    await state.set_state(AddTrackerStates.waiting_for_target_price)


def _auction_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_tdir_above"), callback_data="tdir:above")],
        [InlineKeyboardButton(text=t(lang, "btn_tdir_below"), callback_data="tdir:below")],
        [InlineKeyboardButton(text=t(lang, "btn_auction_no_target"), callback_data="skip_target")],
    ])


async def _ask_auction_direction(wait, state: FSMContext, lang: str, url: str, result, domain: str,
                                 currency: str, price_text: str) -> None:
    """Аукціон (Violity): ціна рухається ставками — питаємо, як сповіщати: перевищить чи впаде нижче."""
    from app.timeutil import fmt as _fmt
    title = html_escape(result.title or "", quote=False)
    if not result.is_available:
        await state.clear()
        await wait.edit_text(t(lang, "auction_ended_at_add", title=title, price=price_text), parse_mode="HTML")
        return
    ends_at = getattr(result, "ends_at", None)
    await state.update_data(
        url=url, canonical_url=result.canonical_url, source=domain, title=result.title,
        price=str(result.price) if result.price else None, currency=currency,
        auction=True, direction=None, ends_at=ends_at.isoformat() if ends_at else None,
    )
    await wait.edit_text(
        t(lang, "auction_card", title=title, price=price_text,
          end=_fmt(ends_at, "%d.%m.%Y %H:%M") if ends_at else "—"),
        reply_markup=_auction_keyboard(lang), parse_mode="HTML",
    )
    await state.set_state(AddTrackerStates.waiting_for_target_price)


@router.callback_query(F.data.in_({"tdir:above", "tdir:below"}))
async def choose_target_direction(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not callback.message:
        return
    user = await get_or_create_user(callback.from_user)
    lang = user.lang
    data = await state.get_data()
    if not data.get("auction"):
        await callback.answer()
        return
    direction = callback.data.split(":", 1)[1]
    await state.update_data(direction=direction)
    price = fmt_money(Decimal(data["price"]), data.get("currency"), lang) if data.get("price") else t(lang, "price_unknown")
    await callback.message.edit_text(
        t(lang, "ask_target_above" if direction == "above" else "ask_target_below",
          currency=data.get("currency") or "UAH", price=price),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "btn_auction_no_target"), callback_data="skip_target")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


def percent_target_price_up(baseline: Decimal, pct: Decimal) -> Decimal:
    """baseline × (1 + pct/100) — «сповісти, коли ціна виросте на N%» (аукціони)."""
    return (Decimal(baseline) * (Decimal(100) + Decimal(pct)) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )


TARGET_PERCENT_UP_MAX = Decimal("1000")


async def _process_auction_target(message: Message, state: FSMContext, user: User, data: dict, kind: str, value: Decimal) -> None:
    lang = user.lang
    direction = data.get("direction")
    if direction not in ("above", "below"):
        await message.answer(t(lang, "auction_choose_direction"), reply_markup=_auction_keyboard(lang), parse_mode="HTML")
        return
    current = Decimal(data["price"]) if data.get("price") else None
    target_percent = baseline = None
    if kind == "percent":
        top = TARGET_PERCENT_UP_MAX if direction == "above" else TARGET_PERCENT_MAX
        if not (TARGET_PERCENT_MIN <= value <= top) or current is None:
            await message.answer(t(lang, "target_percent_range" if current is not None else "target_percent_no_price"), parse_mode="HTML")
            return
        target = percent_target_price_up(current, value) if direction == "above" else percent_target_price(current, value)
        target_percent, baseline = value, current
    else:
        target = value
    if current is not None:
        cur_text = fmt_money(current, data.get("currency"), lang)
        if direction == "above" and target <= current:
            await message.answer(t(lang, "auction_above_invalid", price=cur_text), parse_mode="HTML")
            return
        if direction == "below" and target >= current:
            await message.answer(t(lang, "auction_below_invalid", price=cur_text), parse_mode="HTML")
            return
    await _save_tracker(message.from_user, state, message, target, target_percent=target_percent, baseline_price=baseline)


# Ціль у відсотках: межі "скажи, коли впаде на N%"
TARGET_PERCENT_MIN = Decimal("1")
TARGET_PERCENT_MAX = Decimal("90")


def parse_target(text: str) -> tuple[str, Decimal] | None:
    """
    Розбирає відповідь на «цільову ціну»:
      "4500", "4 500,50"            → ("amount", Decimal("4500"))      (> 0)
      "15%", "15 %", "-15%", "15.5%" → ("percent", Decimal("15"))      (1–90 включно)
    None — не число. ("percent", pct) повертається і поза межами 1–90 — межі перевіряє викликач,
    щоб відповісти саме про відсоток, а не загальним "введи число".
    """
    raw = (text or "").strip().replace(",", ".").replace(" ", " ")
    if not raw:
        return None
    is_percent = raw.endswith("%")
    if is_percent:
        raw = raw[:-1].strip()
        # "−15%" / "-15%" — мінус означає "зниження", а не від'ємне число
        raw = raw.lstrip("-−").strip()
    raw = raw.replace(" ", "")
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0:
        return None
    return ("percent" if is_percent else "amount", value)


def percent_target_price(baseline: Decimal, pct: Decimal) -> Decimal:
    """baseline × (1 − pct/100), округлено до копійок (ROUND_HALF_UP)."""
    return (Decimal(baseline) * (Decimal(100) - Decimal(pct)) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )


@router.message(AddTrackerStates.waiting_for_target_price)
async def process_target(message: Message, state: FSMContext) -> None:
    if not message.text or not message.from_user:
        return
    parsed = parse_target(message.text)
    if parsed is None:
        user = await get_or_create_user(message.from_user)
        await message.answer(t(user.lang, "target_invalid"), parse_mode="HTML")
        return

    kind, value = parsed
    data = await state.get_data()
    if data.get("auction"):
        user = await get_or_create_user(message.from_user)
        await _process_auction_target(message, state, user, data, kind, value)
        return
    if kind == "amount":
        await _save_tracker(message.from_user, state, message, value)
        return

    # Відсоток: 1–90 і лише коли відома поточна ціна (від неї рахуємо абсолютну ціль)
    user = await get_or_create_user(message.from_user)
    if not (TARGET_PERCENT_MIN <= value <= TARGET_PERCENT_MAX):
        await message.answer(t(user.lang, "target_percent_range"), parse_mode="HTML")
        return
    data = await state.get_data()
    if not data.get("price"):
        # Лишаємось у стані: юзер може ввести суму числом або натиснути «Пропустити»
        await message.answer(t(user.lang, "target_percent_no_price"), parse_mode="HTML")
        return
    baseline = Decimal(data["price"])
    await _save_tracker(
        message.from_user, state, message, percent_target_price(baseline, value),
        target_percent=value, baseline_price=baseline,
    )


@router.callback_query(F.data == "skip_target")
async def skip_target(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not callback.message:
        return
    await _save_tracker(callback.from_user, state, callback.message, None)
    await callback.answer()


async def create_tracker(user_id: str, *, url: str, canonical_url: str, source: str, title: str | None,
                         price, currency: str = "UAH", target_price=None, target_percent=None,
                         baseline_price=None, target_direction: str | None = None, ends_at=None,
                         remind_before_minutes: int | None = None) -> str | None:
    """
    Створює трекер і повертає його id; None — цей товар (canonical_url) у юзера вже є.
    Спільне для /add, масового додавання і «стежити» зі списку бажань друга.
    Ліміт безкоштовного плану застосовує окремо sync_free_limit().
    """
    from sqlalchemy import select
    async with get_session() as session:
        exists = (await session.execute(
            select(PriceTracker.id).where(
                PriceTracker.user_id == user_id,
                PriceTracker.canonical_url == canonical_url,
            )
        )).scalar_one_or_none()
        if exists:
            return None
        tracker = PriceTracker(
            user_id=user_id,
            url=url,
            canonical_url=canonical_url,
            source=source,
            title=title,
            current_price=Decimal(str(price)) if price not in (None, "") else None,
            currency=(currency or "UAH").upper(),
            target_price=target_price,
            target_percent=target_percent,
            baseline_price=baseline_price,
            target_direction=target_direction,
            ends_at=_to_naive_utc(ends_at),
            remind_before_minutes=remind_before_minutes,
            status=TrackerStatus.ACTIVE,
            # Явний час з мікросекундами: порядок "найстаріші активні" має бути детермінованим
            created_at=datetime.utcnow(),
        )
        session.add(tracker)
        await session.flush()
        return tracker.id


def _to_naive_utc(dt):
    """aware/ISO-рядок/naive → naive UTC (так зберігаємо в базі)."""
    from datetime import timezone as _tz
    if dt is None or dt == "":
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.astimezone(_tz.utc).replace(tzinfo=None) if dt.tzinfo else dt


DEFAULT_REMIND_MINUTES = 60


async def _save_tracker(from_user, state, msg, target_price, *, target_percent=None, baseline_price=None):
    """
    target_price — абсолютна ціль (або None). Для цілі у відсотках додатково target_percent
    (скільки %) і baseline_price (ціна на момент додавання): target_price уже порахований від них,
    тож планувальник порівнює new_price <= target_price як і раніше.
    """
    data = await state.get_data()
    await state.clear()

    user = await get_or_create_user(from_user)
    lang = user.lang
    currency = data.get("currency") or "UAH"

    auction = bool(data.get("auction"))
    direction = data.get("direction") if auction and target_price else None
    ends_at = _to_naive_utc(data.get("ends_at")) if auction else None
    remind = DEFAULT_REMIND_MINUTES if ends_at and ends_at > datetime.utcnow() else None
    new_id = await create_tracker(
        user.id, url=data["url"], canonical_url=data["canonical_url"], source=data["source"],
        title=data.get("title"), price=data.get("price"), currency=currency,
        target_price=target_price, target_percent=target_percent, baseline_price=baseline_price,
        target_direction=direction, ends_at=ends_at, remind_before_minutes=remind,
    )
    if new_id is None:
        await msg.answer(t(lang, "already_tracked"))
        return

    # Free: понад ліміт новий товар одразу йде на паузу і не перевіряється
    _, paused_now = await sync_free_limit(user.id)
    if not user.is_premium:
        if any(tr.id == new_id for tr in paused_now):
            slot_text = t(lang, "added_paused", n=rt.cached("FREE_TIER_MAX_ITEMS"))
        else:
            used = await count_active(user.id)
            slot_text = t(lang, "added_slots", used=used, n=rt.cached("FREE_TIER_MAX_ITEMS"))
    else:
        slot_text = ""

    if target_price and auction:
        key = "added_target_above" if direction == "above" else "added_target_below_auction"
        target_text = t(lang, key, price=fmt_money(target_price, currency, lang))
    elif target_price and target_percent:
        target_text = t(lang, "added_target_percent", pct=fmt_percent(target_percent, lang),
                        price=fmt_money(target_price, currency, lang))
    elif target_price:
        target_text = t(lang, "added_target", price=fmt_money(target_price, currency, lang))
    else:
        target_text = ""

    markup = None
    if remind:
        from app.bot.keyboards import reminder_keyboard
        from app.timeutil import fmt as _fmt
        target_text += t(lang, "added_reminder", end=_fmt(ends_at, "%d.%m %H:%M"))
        markup = reminder_keyboard(lang, new_id, remind)
    await msg.answer(
        t(lang, "added", title=html_escape((data.get("title") or "")[:60], quote=False), target=target_text, slots=slot_text),
        parse_mode="HTML", reply_markup=markup,
    )


# ─── /list ───────────────────────────────────────────────────────

@router.message(Command("list"))
async def cmd_list_msg(message: Message) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(message.from_user)
    await _show_list(user, message)


@router.callback_query(F.data == "my_list")
async def cmd_list_cb(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    user = await get_or_create_user(callback.from_user)
    await _show_list(user, callback.message)
    await callback.answer()


def _expect_line(tr: PriceTracker, lang: str, auction: bool) -> str:
    """
    Чого чекаємо по ціні — так само, як вирішує планувальник: ціль (з напрямком / у відсотках) або
    загальна знижка ≥3% (на аукціоні її немає); notify_drop=False глушить і ціль, і знижку.
    Плюс подорожчання ≥3%, якщо ввімкнене.
    """
    cur = tr.currency
    if auction and not tr.target_price:
        line = t(lang, "list_expect_lot")
    elif not tr.notify_drop:
        line = ""
    elif tr.target_price and tr.target_direction == "above":
        line = t(lang, "list_expect_above", price=fmt_money(tr.target_price, cur, lang))
    elif tr.target_price and tr.target_percent:
        line = t(lang, "list_expect_percent", pct=fmt_percent(tr.target_percent, lang),
                 price=fmt_money(tr.target_price, cur, lang))
    elif tr.target_price:
        line = t(lang, "list_expect_below", price=fmt_money(tr.target_price, cur, lang))
    else:
        line = t(lang, "list_expect_drop")
    if tr.notify_increase:
        line = f"{line} · {t(lang, 'list_expect_increase')}" if line else t(lang, "list_expect_increase_only")
    return line or t(lang, "list_expect_muted")


def list_item_lines(tr: PriceTracker, lang: str, now: datetime | None = None) -> list[str]:
    """
    Рядки під назвою товару в /list: ціна й магазин; для аукціону — кінець торгів і нагадування;
    і чого саме чекаємо (ціль / знижка / ставка / наявність), щоб не треба було відкривати картку.
    """
    from app.bot.keyboards import reminder_label
    from app.timeutil import fmt as _fmt
    now = now or datetime.utcnow()
    auction = _is_auction_source(tr.source)
    price_line = f"💰 {fmt_money(tr.current_price, tr.currency, lang)}"
    if tr.source:
        # Магазин — посиланням саме на товар: голий «rozetka.com.ua» Telegram сам лінкує на головну сайту
        shop = html_escape(tr.source, quote=False)
        url = tr.canonical_url or tr.url
        price_line += f" · <a href='{html_escape(url, quote=True)}'>{shop}</a>" if url else f" · {shop}"
    lines = [price_line]
    if tr.status == TrackerStatus.PAUSED_DOMAIN:
        return lines + [t(lang, "list_paused_domain")]     # магазин блокує перевірки — чекаємо розблокування
    if tr.status == TrackerStatus.ERROR:
        return lines + [t(lang, "list_error")]
    if auction:
        if tr.lot_outcome == "sold":
            return lines + [t(lang, "list_lot_sold")]
        if (tr.ends_at and tr.ends_at <= now) or not tr.is_available:
            return lines + [t(lang, "list_lot_unsold" if tr.lot_outcome == "unsold" else "list_auction_ended")]
        if tr.ends_at:
            end = t(lang, "list_auction_until", end=_fmt(tr.ends_at, "%d.%m %H:%M"))
            if tr.remind_before_minutes:
                end += " · " + t(lang, "list_auction_remind", value=reminder_label(lang, tr.remind_before_minutes))
            lines.append(end)
    elif not tr.is_available:
        return lines + [t(lang, "list_out_of_stock" if tr.notify_restock else "list_out_of_stock_muted")]
    return lines + [_expect_line(tr, lang, auction)]


async def _show_list(user: User, msg: Message) -> None:
    lang = user.lang
    if user.is_blocked:
        await msg.answer(t(lang, "blocked"))
        return
    # Перед показом перераховуємо ліміт: список завжди відображає реальний стан
    await sync_free_limit(user.id)
    trackers = await get_trackers(user.id, include_paused=True, include_errors=True)

    if not trackers:
        await msg.answer(
            t(lang, "list_empty"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t(lang, "btn_add"), callback_data="add_tracker")]
            ]),
        )
        return

    lines = [t(lang, "list_header", n=len(trackers))]
    display_limit = 40

    for i, tr in enumerate(trackers[:display_limit], 1):
        # Статус — із бази: ACTIVE перевіряється, PAUSED_LIMIT чекає на слот, PAUSED зупинений вручну
        if tr.status == TrackerStatus.PAUSED_LIMIT:
            status_icon = "⏳"
            status_text = t(lang, "list_paused_limit")
        elif tr.status == TrackerStatus.ERROR:
            status_icon = "⚠️"
            status_text = ""
        else:
            status_icon = "✅" if tr.status == TrackerStatus.ACTIVE else "⏸"
            status_text = ""

        # Формуємо блок товару: назва, ціна · магазин, [торги], чого чекаємо
        title = html_escape((tr.title or t(lang, "untitled"))[:40], quote=False)   # назва — з сайту магазину
        lines.append(f"{i}. {status_icon} <b>{title}</b>")
        item = list_item_lines(tr, lang)
        if status_text:
            item[0] += f" {status_text}"
        lines.extend(f"   {line}" for line in item)
        lines.append("")

    if len(trackers) > display_limit:
        lines.append(t(lang, "list_more", n=len(trackers) - display_limit))

    # Формуємо підвал з інформацією про план
    if user.is_premium:
        plan_info = t(lang, "plan_premium")
    else:
        active_count = sum(1 for tr in trackers if tr.status == TrackerStatus.ACTIVE)
        plan_info = t(lang, "plan_free", active=active_count, total=len(trackers))
        if any(tr.status == TrackerStatus.PAUSED_LIMIT for tr in trackers):
            plan_info += t(lang, "plan_free_limit_note")

    lines.append(f"\n{plan_info}")

    await msg.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=t(lang, "btn_add_short"), callback_data="add_tracker"),
                InlineKeyboardButton(text=t(lang, "btn_delete"), callback_data="delete_menu"),
                InlineKeyboardButton(text=t(lang, "btn_settings_short"), callback_data="settings_menu"),
            ],
            [InlineKeyboardButton(text=t(lang, "btn_share_list"), callback_data="share_menu")],
            [] if user.is_premium else [
                InlineKeyboardButton(text=t(lang, "btn_activate_premium"), callback_data="premium_info")
            ],
        ]),
        parse_mode="HTML",
        disable_web_page_preview=True,          # у списку десятки посилань — без прев'ю першого з них
    )


# ─── /delete ─────────────────────────────────────────────────────

@router.message(Command("delete"))
async def cmd_delete_msg(message: Message) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(message.from_user)
    await _show_delete_menu(user, message)


@router.callback_query(F.data == "delete_menu")
async def cmd_delete_cb(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    user = await get_or_create_user(callback.from_user)
    await _show_delete_menu(user, callback.message)
    await callback.answer()


async def _show_delete_menu(user: User, msg: Message) -> None:
    lang = user.lang
    # Показуємо і ті, що на паузі: видалення активного звільняє слот для них
    trackers = await get_trackers(user.id, include_paused=True)

    if not trackers:
        await msg.answer(t(lang, "delete_empty"))
        return

    buttons = []
    for tr in trackers:
        price_str = f" · {fmt_money(tr.current_price, tr.currency, lang)}" if tr.current_price else ""
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {(tr.title or t(lang, 'untitled'))[:35]}{price_str}",
            callback_data=f"del:{tr.id}",
        )])
    buttons.append([InlineKeyboardButton(text=t(lang, "btn_back"), callback_data="my_list")])

    await msg.answer(
        t(lang, "delete_prompt"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("del:"))
async def confirm_delete(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return

    tracker_id = callback.data.split(":", 1)[1]
    user = await get_or_create_user(callback.from_user)
    lang = user.lang

    async with get_session() as session:
        tracker = await session.get(PriceTracker, tracker_id)
        if not tracker:
            await callback.answer(t(lang, "not_found"))
            return
        if tracker.user_id != user.id:
            await callback.answer(t(lang, "no_access"))
            return
        title = html_escape((tracker.title or t(lang, "item"))[:50], quote=False)

    await callback.message.edit_text(
        t(lang, "delete_confirm", title=title),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=t(lang, "btn_yes"), callback_data=f"delok:{tracker_id}"),
                InlineKeyboardButton(text=t(lang, "btn_no"), callback_data="delete_menu"),
            ]
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delok:"))
async def do_delete(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return

    tracker_id = callback.data.split(":", 1)[1]
    user = await get_or_create_user(callback.from_user)
    lang = user.lang

    async with get_session() as session:
        tracker = await session.get(PriceTracker, tracker_id)
        if not tracker or tracker.user_id != user.id:
            await callback.answer(t(lang, "error"))
            return
        title = html_escape((tracker.title or t(lang, "item"))[:50], quote=False)
        await session.delete(tracker)

    # Звільнився слот — найстаріший товар з паузи стає активним
    activated, _ = await sync_free_limit(user.id)
    count_after = await count_trackers(user.id)
    text = t(lang, "deleted", title=title, n=count_after)
    if activated:
        titles = "\n".join(f"  ▶️ {html_escape((tr.title or t(lang, 'item'))[:40], quote=False)}" for tr in activated)
        text += t(lang, "deleted_activated", titles=titles)
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=t(lang, "btn_list_short"), callback_data="my_list"),
                InlineKeyboardButton(text=t(lang, "btn_add_short"), callback_data="add_tracker"),
            ]
        ]),
        parse_mode="HTML",
    )
    await callback.answer(t(lang, "deleted_toast"))


# ─── /settings — сповіщення по кожному товару ────────────────────
#
# Правило безпеки: кожен callback тут несе id трекера, і перед будь-якою дією
# ми перевіряємо, що tracker.user_id == юзер, який натиснув (callback.from_user).
# Чужий id → toast "Немає доступу" і жодних змін.

async def _own_tracker(callback: CallbackQuery, user: User, tracker_id: str) -> PriceTracker | None:
    """Трекер юзера або None (тоді юзер уже отримав toast not_found / no_access)."""
    lang = user.lang
    async with get_session() as session:
        tracker = await session.get(PriceTracker, tracker_id)
    if not tracker:
        await callback.answer(t(lang, "not_found"))
        return None
    if tracker.user_id != user.id:
        logger.warning("tracker_access_denied", telegram_id=callback.from_user.id, tracker_id=tracker_id,
                       action=(callback.data or "").split(":", 1)[0])
        await callback.answer(t(lang, "no_access"))
        return None
    return tracker


@router.message(Command("settings"))
async def cmd_settings_msg(message: Message) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(message.from_user)
    await _show_settings_menu(user, message, edit=False)


@router.callback_query(F.data == "settings_menu")
async def cmd_settings_cb(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    user = await get_or_create_user(callback.from_user)
    # Кнопка "◀️ Назад" з картки товару — перемальовуємо те саме повідомлення
    await _show_settings_menu(user, callback.message, edit=True)
    await callback.answer()


async def _show_settings_menu(user: User, msg: Message, *, edit: bool) -> None:
    lang = user.lang
    # Як у /delete: активні + на паузі, без ERROR
    trackers = await get_trackers(user.id, include_paused=True)
    send = msg.edit_text if edit else msg.answer

    if not trackers:
        await send(
            t(lang, "settings_empty"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t(lang, "btn_add"), callback_data="add_tracker")]
            ]),
        )
        return

    # Перший рядок — глобальний перемикач щотижневого дайджесту, далі товари
    buttons = [[digest_toggle_button(lang, user.digest_enabled)]]
    for tr in trackers:
        icon = "🔔" if (tr.notify_drop and tr.notify_restock) else "🔕"
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {(tr.title or t(lang, 'untitled'))[:40]}",
            callback_data=f"tset:{tr.id}",
        )])
    buttons.append([InlineKeyboardButton(text=t(lang, "btn_back"), callback_data="my_list")])

    await send(
        t(lang, "settings_prompt"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


# ─── Щотижневий дайджест: вимкнути / перемкнути ──────────────────
#
# Обидва callback'и змінюють лише прапорець User.digest_enabled того, хто натиснув
# (callback.from_user) — чужих даних тут немає.

async def _set_digest_enabled(telegram_id: int, enabled: bool | None) -> bool:
    """enabled=None → інвертувати. Повертає новий стан."""
    from sqlalchemy import select
    async with get_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )).scalar_one()
        user.digest_enabled = (not user.digest_enabled) if enabled is None else enabled
        new_state = user.digest_enabled
    logger.info("digest_toggled", telegram_id=telegram_id, enabled=new_state)
    return new_state


@router.callback_query(F.data == "digest:off")
async def digest_off(callback: CallbackQuery) -> None:
    """Кнопка "🔕 Вимкнути дайджест" під самим дайджестом."""
    if not callback.from_user or not callback.message:
        return
    user = await get_or_create_user(callback.from_user)
    await _set_digest_enabled(callback.from_user.id, False)
    try:
        # Прибираємо кнопку, щоб не натискали вдруге; текст дайджесту лишається
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as e:  # noqa: BLE001 — старе повідомлення могло стати нередагованим
        logger.warning("digest_keyboard_edit_failed", telegram_id=callback.from_user.id, error=str(e))
    await callback.answer(t(user.lang, "digest_off_toast"))


@router.callback_query(F.data == "digest:toggle")
async def digest_toggle(callback: CallbackQuery) -> None:
    """Рядок "📬 Щотижневий дайджест ✅/☐" у /settings — перемикає і перемальовує меню."""
    if not callback.from_user or not callback.message:
        return
    user = await get_or_create_user(callback.from_user)
    enabled = await _set_digest_enabled(callback.from_user.id, None)
    user.digest_enabled = enabled
    await _show_settings_menu(user, callback.message, edit=True)
    await callback.answer(t(user.lang, "digest_on_toast" if enabled else "digest_off_toast"))


async def _show_tracker_settings(msg: Message, user: User, tracker: PriceTracker, *, edit: bool) -> None:
    lang = user.lang
    text = t(
        lang, "settings_item",
        title=html_escape((tracker.title or t(lang, "item"))[:60], quote=False),
        price=fmt_money(tracker.current_price, tracker.currency, lang),
    )
    keyboard = tracker_settings_keyboard(
        lang, tracker.id, notify_drop=tracker.notify_drop, notify_restock=tracker.notify_restock,
        notify_increase=tracker.notify_increase,
        remind_minutes=tracker.remind_before_minutes, auction=_is_auction_source(tracker.source),
    )
    send = msg.edit_text if edit else msg.answer
    await send(text, reply_markup=keyboard, parse_mode="HTML")


def _is_auction_source(source: str | None) -> bool:
    p = platform_for(_norm_host(source or ""))
    return bool(p and p.auction)


async def _set_reminder(callback: CallbackQuery) -> tuple[User, PriceTracker, int] | None:
    """rem:<хв>:<id> / trem:<хв>:<id> → зберегти «за скільки нагадати» (0 — вимкнути) для свого трекера."""
    from app.bot.keyboards import REMIND_OPTIONS, reminder_label
    if not callback.from_user or not callback.message:
        return None
    user = await get_or_create_user(callback.from_user)
    lang = user.lang
    try:
        _, raw, tracker_id = callback.data.split(":", 2)
        minutes = int(raw)
    except ValueError:
        await callback.answer()
        return None
    if minutes not in (*REMIND_OPTIONS, 0):
        await callback.answer()
        return None
    async with get_session() as session:
        tracker = await session.get(PriceTracker, tracker_id)
        if not tracker:
            await callback.answer(t(lang, "not_found"))
            return None
        if tracker.user_id != user.id:
            logger.warning("tracker_access_denied", telegram_id=callback.from_user.id, tracker_id=tracker_id, action="remind")
            await callback.answer(t(lang, "no_access"))
            return None
        tracker.remind_before_minutes = minutes or None
        if minutes:
            tracker.ending_notified_at = None      # обрали інший час — нагадаємо за новим
    await callback.answer(t(lang, "settings_toast_reminder", value=reminder_label(lang, minutes)))
    return user, tracker, minutes


@router.callback_query(F.data.startswith("rem:"))
async def choose_reminder(callback: CallbackQuery) -> None:
    """Кнопки «1 год · 30 хв · 15 хв · 5 хв» під повідомленням «Додано!» — міняємо лише ✅."""
    from app.bot.keyboards import reminder_keyboard
    res = await _set_reminder(callback)
    if res:
        user, tracker, minutes = res
        with suppress(TelegramBadRequest):        # той самий вибір вдруге — «message is not modified»
            await callback.message.edit_reply_markup(reply_markup=reminder_keyboard(user.lang, tracker.id, minutes or None))


@router.callback_query(F.data.startswith("trem:"))
async def choose_reminder_in_settings(callback: CallbackQuery) -> None:
    res = await _set_reminder(callback)
    if res:
        user, tracker, _ = res
        with suppress(TelegramBadRequest):
            await _show_tracker_settings(callback.message, user, tracker, edit=True)


@router.callback_query(F.data.startswith("tset:"))
async def open_tracker_settings_inplace(callback: CallbackQuery) -> None:
    """Зі списку /settings — картка товару замість списку (те саме повідомлення)."""
    if not callback.from_user or not callback.message:
        return
    user = await get_or_create_user(callback.from_user)
    tracker = await _own_tracker(callback, user, callback.data.split(":", 1)[1])
    if not tracker:
        return
    await _show_tracker_settings(callback.message, user, tracker, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("tsettings:"))
async def open_tracker_settings_new(callback: CallbackQuery) -> None:
    """Кнопка "⚙️ Налаштування" під сповіщенням — нове повідомлення, сповіщення не чіпаємо."""
    if not callback.from_user or not callback.message:
        return
    user = await get_or_create_user(callback.from_user)
    tracker = await _own_tracker(callback, user, callback.data.split(":", 1)[1])
    if not tracker:
        return
    await _show_tracker_settings(callback.message, user, tracker, edit=False)
    await callback.answer()


def _set_notify_flag(tracker: PriceTracker, kind: str, enabled: bool) -> None:
    setattr(tracker, NOTIFY_FIELDS[kind], enabled)


@router.callback_query(F.data.startswith("ttoggle:"))
async def toggle_tracker_setting(callback: CallbackQuery) -> None:
    """Перемикач ✅/☐ на картці товару: ttoggle:<drop|restock|increase>:<tracker_id>."""
    if not callback.from_user or not callback.message:
        return
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or parts[1] not in NOTIFY_FIELDS:
        await callback.answer()
        return
    _, kind, tracker_id = parts
    user = await get_or_create_user(callback.from_user)
    lang = user.lang

    async with get_session() as session:
        tracker = await session.get(PriceTracker, tracker_id)
        if not tracker:
            await callback.answer(t(lang, "not_found"))
            return
        if tracker.user_id != user.id:
            logger.warning("tracker_access_denied", telegram_id=callback.from_user.id, tracker_id=tracker_id, action="ttoggle")
            await callback.answer(t(lang, "no_access"))
            return
        enabled = not getattr(tracker, NOTIFY_FIELDS[kind])
        _set_notify_flag(tracker, kind, enabled)

    logger.info("tracker_notify_toggled", telegram_id=callback.from_user.id, tracker_id=tracker_id, kind=kind, enabled=enabled)
    await _show_tracker_settings(callback.message, user, tracker, edit=True)
    await callback.answer(t(lang, "settings_toast_on" if enabled else "settings_toast_off"))


@router.callback_query(F.data.startswith("mute:") | F.data.startswith("unmute:"))
async def toggle_mute_from_notification(callback: CallbackQuery) -> None:
    """
    Кнопка під сповіщенням: mute:<restock|drop|increase>:<tracker_id> вимикає цей тип сповіщень
    по товару, unmute:... вмикає назад. Перемальовуємо лише клавіатуру — текст сповіщення лишається.
    """
    if not callback.from_user or not callback.message:
        return
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or parts[1] not in NOTIFY_FIELDS:
        await callback.answer()
        return
    action, kind, tracker_id = parts
    enabled = action == "unmute"
    user = await get_or_create_user(callback.from_user)
    lang = user.lang

    async with get_session() as session:
        tracker = await session.get(PriceTracker, tracker_id)
        if not tracker:
            await callback.answer(t(lang, "not_found"))
            return
        if tracker.user_id != user.id:
            logger.warning("tracker_access_denied", telegram_id=callback.from_user.id, tracker_id=tracker_id, action=action)
            await callback.answer(t(lang, "no_access"))
            return
        _set_notify_flag(tracker, kind, enabled)
        markup = callback.message.reply_markup
        had_retarget = bool(markup) and any((b.callback_data or "").startswith("newtarget:")
                                            for row in markup.inline_keyboard for b in row)
        retarget = _target_direction(tracker) if had_retarget else None

    logger.info("tracker_notify_toggled", telegram_id=callback.from_user.id, tracker_id=tracker_id, kind=kind, enabled=enabled)
    try:
        await callback.message.edit_reply_markup(
            reply_markup=notification_keyboard(lang, kind, tracker_id, muted=not enabled, retarget=retarget),
        )
    except Exception as e:  # noqa: BLE001 — старе повідомлення могло стати нередагованим
        logger.warning("notification_keyboard_edit_failed", tracker_id=tracker_id, error=str(e))
    await callback.answer(t(lang, f"{'unmute' if enabled else 'mute'}_toast_{kind}"))


# ─── Нова межа / ціль з-під сповіщення «ціль досягнута» ──────────

class NewTargetStates(StatesGroup):
    waiting = State()


def _target_direction(tracker: PriceTracker) -> str:
    """«above» — межа ставки (аукціон), «below» — ціль «впаде до» (типово)."""
    return "above" if tracker.target_direction == "above" else "below"


def _auction_over(tracker: PriceTracker) -> bool:
    return _is_auction_source(tracker.source) and bool(
        tracker.lot_outcome or not tracker.is_available
        or (tracker.ends_at and tracker.ends_at <= datetime.utcnow()))


@router.callback_query(F.data.startswith("newtarget:"))
async def ask_new_target(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка під «ціль досягнута»: попросити нову суму або відсоток у тому ж напрямку."""
    if not callback.from_user or not callback.message:
        return
    user = await get_or_create_user(callback.from_user)
    lang = user.lang
    tracker = await _own_tracker(callback, user, callback.data.split(":", 1)[1])
    if not tracker:
        return
    if _auction_over(tracker):
        await callback.message.answer(t(lang, "new_target_ended"))
        await callback.answer()
        return
    direction = _target_direction(tracker)
    await state.set_state(NewTargetStates.waiting)
    await state.update_data(tracker_id=tracker.id)
    cur = tracker.currency
    await callback.message.answer(
        t(lang, f"ask_new_target_{direction}",
          title=html_escape((tracker.title or t(lang, "item"))[:60], quote=False),
          price=fmt_money(tracker.current_price, cur, lang),
          target=fmt_money(tracker.target_price, cur, lang) if tracker.target_price else "—"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "btn_cancel"), callback_data="newtarget_cancel")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(NewTargetStates.waiting)
async def process_new_target(message: Message, state: FSMContext) -> None:
    """Сума («2500») або відсоток від поточної ціни («20%»): «above» — вище ставки, «below» — нижче ціни."""
    if not message.from_user:
        return
    user = await get_or_create_user(message.from_user)
    lang = user.lang
    parsed = parse_target(message.text or "")
    if parsed is None:
        await message.answer(t(lang, "target_invalid"), parse_mode="HTML")
        return
    kind, value = parsed
    tracker_id = (await state.get_data()).get("tracker_id")
    async with get_session() as session:
        tracker = await session.get(PriceTracker, tracker_id) if tracker_id else None
        if tracker is None or tracker.user_id != user.id:
            await state.clear()
            await message.answer(t(lang, "not_found"))
            return
        direction = _target_direction(tracker)
        current = tracker.current_price
        currency = tracker.currency
        target_percent = baseline = None
        if kind == "percent":
            top = TARGET_PERCENT_UP_MAX if direction == "above" else TARGET_PERCENT_MAX
            if current is None:
                await message.answer(t(lang, "target_percent_no_price"), parse_mode="HTML")
                return
            if not (TARGET_PERCENT_MIN <= value <= top):
                await message.answer(t(lang, "target_percent_range"), parse_mode="HTML")
                return
            target = percent_target_price_up(current, value) if direction == "above" else percent_target_price(current, value)
            target_percent, baseline = value, current
        else:
            target = value
        if current is not None:
            cur_text = fmt_money(current, currency, lang)
            if direction == "above" and target <= current:
                await message.answer(t(lang, "auction_above_invalid", price=cur_text), parse_mode="HTML")
                return
            if direction == "below" and target >= current:
                await message.answer(t(lang, "auction_below_invalid", price=cur_text), parse_mode="HTML")
                return
        # Стара ціль уже «сповіщена» — нову ловимо на перетині (поточна ціна по інший бік, див. _detect_event)
        tracker.target_price, tracker.target_percent, tracker.baseline_price = target, target_percent, baseline
    await state.clear()
    logger.info("tracker_target_changed", telegram_id=message.from_user.id, tracker_id=tracker_id,
                direction=direction, target=str(target))
    await message.answer(t(lang, f"new_target_saved_{direction}", price=fmt_money(target, currency, lang)),
                         parse_mode="HTML")


@router.callback_query(F.data == "newtarget_cancel")
async def cancel_new_target(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.from_user and callback.message:
        user = await get_or_create_user(callback.from_user)
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(t(user.lang, "cancelled"))
    await callback.answer()


# ─── /premium ────────────────────────────────────────────────────

@router.message(Command("premium"))
@router.callback_query(F.data == "premium_info")
async def cmd_premium(event: Message | CallbackQuery) -> None:
    msg = event.message if isinstance(event, CallbackQuery) else event
    if not msg or not event.from_user:
        return

    user = await get_or_create_user(event.from_user)
    lang = user.lang

    if user.is_premium:
        await msg.answer(
            t(lang, "premium_active", until=fmt_date(user.premium_until, lang)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t(lang, "btn_renew_premium"),
                    callback_data="pay_premium",
                )],
            ]),
            parse_mode="HTML",
        )
    else:
        await msg.answer(
            t(lang, "premium_offer", price=rt.cached("PREMIUM_PRICE_UAH")),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t(lang, "btn_pay", price=rt.cached("PREMIUM_PRICE_UAH")),
                    callback_data="pay_premium",
                )],
            ]),
            parse_mode="HTML",
        )

    if isinstance(event, CallbackQuery):
        await event.answer()


# ─── /help ───────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(message.from_user)
    await message.answer(t(user.lang, "help"), parse_mode="HTML")


# ─── /sites ──────────────────────────────────────────────────────

def sites_text(lang: str) -> str:
    """Перевірені магазини по країнах + примітка про інші сайти і про ті, що не працюють."""
    by_country: dict[str, list[str]] = {}
    for p in verified_platforms():
        by_country.setdefault(p.country or "GLOBAL", []).append(p.name)
    # Україна першою, далі в порядку реєстру, GLOBAL — наприкінці
    order = sorted(by_country, key=lambda c: (0 if c == "UA" else 2 if c == "GLOBAL" else 1,
                                               list(by_country).index(c)))
    lines = [t(lang, "sites_header")]
    for c in order:
        lines.append(t(lang, "sites_country", country=country_label(c, lang), names=", ".join(by_country[c])))
    if len(lines) == 1:
        lines.append("—")
    lines.append("")
    lines.append(t(lang, "sites_other"))

    broken = [p.name for p in PLATFORMS if p.status in ("needs_js", "blocked")]
    if broken:
        lines.append("")
        if len(broken) <= 10:
            lines.append(t(lang, "sites_unsupported", names=", ".join(broken)))
        else:
            lines.append(t(lang, "sites_unsupported_many"))
    return "\n".join(lines)


@router.message(Command("sites"))
async def cmd_sites(message: Message) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(message.from_user)
    await message.answer(sites_text(user.lang), parse_mode="HTML", disable_web_page_preview=True)


# ─── Feedback ────────────────────────────────────────────────────

class FeedbackState(StatesGroup):
    waiting = State()


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(message.from_user)
    await message.answer(t(user.lang, "feedback_prompt"), parse_mode="HTML")
    await state.set_state(FeedbackState.waiting)


_FEEDBACK_TYPE_LABEL = {
    "photo": "🖼 Фото", "document": "📎 Файл", "video": "🎥 Відео", "voice": "🎤 Голосове",
    "audio": "🎵 Аудіо", "sticker": "🙂 Стікер", "other": "📦 Інше",
}


def _feedback_media(message: Message) -> tuple[str, str | None, str | None, str | None]:
    """(content_type, file_id, file_name, mime_type) для повідомлення у /feedback."""
    if message.photo:
        p = message.photo[-1]  # найбільший розмір
        return "photo", p.file_id, None, "image/jpeg"
    if message.document:
        d = message.document
        return "document", d.file_id, d.file_name, d.mime_type
    if message.video:
        v = message.video
        return "video", v.file_id, v.file_name, v.mime_type
    if message.voice:
        return "voice", message.voice.file_id, None, message.voice.mime_type
    if message.audio:
        a = message.audio
        return "audio", a.file_id, a.file_name, a.mime_type
    if message.sticker:
        return "sticker", message.sticker.file_id, None, None
    if message.text:
        return "text", None, None, None
    return "other", None, None, None


@router.message(FeedbackState.waiting)
async def process_feedback(message: Message, state: FSMContext) -> None:
    """
    Приймає будь-яке повідомлення у стані фідбеку: текст, фото, файл, відео,
    голосове, стікер... Завжди чистить стан, зберігає рядок Feedback у базі,
    потім шле адміну шапку + копію медіа. Якщо доставити адміну не вдалося —
    рядок у базі лишається, юзер усе одно отримує подяку.
    """
    if not message.from_user:
        return
    await state.clear()
    user = await get_or_create_user(message.from_user)
    lang = user.lang

    content_type, file_id, file_name, mime_type = _feedback_media(message)
    text = (message.text or message.caption or "").strip() or None

    fb = Feedback(
        user_id=user.id,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        language_code=user.language_code,
        text=text,
        content_type=content_type,
        file_id=file_id,
        file_name=file_name,
        mime_type=mime_type,
        telegram_message_id=message.message_id,
        chat_id=message.chat.id,
    )
    async with get_session() as session:
        session.add(fb)
        await session.flush()
    feedback_id = fb.id
    logger.info("feedback_saved", feedback_id=feedback_id, telegram_id=message.from_user.id, content_type=content_type)

    username = f"@{message.from_user.username}" if message.from_user.username else "без юзернейму"
    header = (
        f"💬 <b>Feedback</b>\n\n"
        f"👤 {html_escape(message.from_user.first_name or '')} {html_escape(username)} [{lang}]\n"
        f"🆔 <code>{message.from_user.id}</code>"
    )
    if content_type != "text":
        label = _FEEDBACK_TYPE_LABEL.get(content_type, _FEEDBACK_TYPE_LABEL["other"])
        header += f"\n{label}" + (f": {html_escape(file_name)}" if file_name else "")
    header += f"\n\n{html_escape(text)}" if text else ""

    delivered = False
    forwarded_id: int | None = None
    try:
        if not message.bot:
            raise RuntimeError("no bot instance")
        await message.bot.send_message(chat_id=settings.ADMIN_TELEGRAM_ID, text=header, parse_mode="HTML")
        if content_type != "text":
            # copy_message зберігає фото/файл і підпис, але без "forwarded from" — юзер не деанонімізується
            copied = await message.bot.copy_message(
                chat_id=settings.ADMIN_TELEGRAM_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            forwarded_id = int(copied.message_id)
        delivered = True
    except Exception as e:
        logger.warning("feedback_admin_delivery_failed", feedback_id=feedback_id, error=str(e))

    if forwarded_id is not None:
        async with get_session() as session:
            row = await session.get(Feedback, feedback_id)
            if row:
                row.forwarded_message_id = forwarded_id

    await message.answer(t(lang, "feedback_thanks" if delivered else "feedback_received"))


# ─── /reply (тільки адмін): відповідь на фідбек прямо з Telegram ─

@router.message(Command("reply"))
async def cmd_reply(message: Message) -> None:
    """
    Використання: /reply 123456789 Дякую, виправив!
    Шле текст юзеру від імені бота і позначає його останній фідбек як "replied".
    """
    if not message.from_user or not message.bot:
        return
    if message.from_user.id != int(settings.ADMIN_TELEGRAM_ID):
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].lstrip("-").isdigit() or not parts[2].strip():
        await message.answer(
            "Використання:\n<code>/reply TELEGRAM_ID текст відповіді</code>\n\n"
            "Приклад: <code>/reply 123456789 Дякую, виправив!</code>",
            parse_mode="HTML",
        )
        return
    target_id, reply_text = int(parts[1]), parts[2].strip()

    from sqlalchemy import select as sa_select
    from datetime import datetime, timezone
    async with get_session() as session:
        target = (await session.execute(
            sa_select(User).where(User.telegram_id == target_id)
        )).scalar_one_or_none()
    target_lang = target.lang if target else "uk"

    try:
        await message.bot.send_message(
            chat_id=target_id,
            text=t(target_lang, "feedback_reply_header", text=html_escape(reply_text)),
            parse_mode="HTML",
        )
    except Exception as e:
        await message.answer(f"❌ Не вдалося надіслати: {e}")
        return

    async with get_session() as session:
        fb = (await session.execute(
            sa_select(Feedback).where(Feedback.telegram_id == target_id)
            .order_by(Feedback.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if fb:
            fb.status = "replied"
            fb.reply_text = reply_text
            fb.replied_at = datetime.now(timezone.utc).replace(tzinfo=None)
            fb.replied_by = f"telegram:{message.from_user.id}"

    await message.answer(
        "✅ Відповідь надіслано" + (" і фідбек позначено як «відповіли»" if fb else " (фідбеку від цього юзера в базі немає)")
    )


# ─── /broadcast ВИМКНЕНО (аудит 2026-10-13, N3) ─────────────────
# Розсилав текст УСІМ користувачам одразу, без попереднього перегляду й підтвердження: одна помилка —
# безповоротно в усіх, а викрадений Telegram адміна = готовий канал розсилки. Якщо колись знадобиться —
# лише з переглядом, кнопкою «Надіслати», журналом у admin_actions і лімітом, краще з адмінки (2FA).
#
# # ─── Broadcast (тільки для адміна) ───────────────────────────────
#
# @router.message(Command("broadcast"))
# async def cmd_broadcast(message: Message) -> None:
#     if not message.from_user or not message.bot:
#         return
#
#     # Тільки адмін може надсилати broadcast
#     if message.from_user.id != int(settings.ADMIN_TELEGRAM_ID):
#         return
#
#     text = (message.text or "").removeprefix("/broadcast").strip()
#     if not text:
#         await message.answer(
#             "Використання:\n<code>/broadcast Текст повідомлення</code>",
#             parse_mode="HTML",
#         )
#         return
#
#     from sqlalchemy import select as sa_select
#     async with get_session() as session:
#         users = (await session.execute(
#             sa_select(User).where(User.is_blocked == False, User.bot_blocked == False)
#         )).scalars().all()
#
#     await message.answer(f"⏳ Надсилаю {len(users)} юзерам...")
#
#     sent = failed = 0
#     for user in users:
#         try:
#             await message.bot.send_message(
#                 chat_id=user.telegram_id,
#                 text=text,
#                 parse_mode="HTML",
#             )
#             sent += 1
#             # Telegram дозволяє ~30 повідомлень/сек групам і ~20/сек юзерам
#             await asyncio.sleep(0.05)
#         except Exception as e:
#             if "bot was blocked" in str(e).lower():
#                 async with get_session() as s:
#                     u = await s.get(User, user.id)
#                     if u:
#                         u.bot_blocked = True
#             failed += 1
#
#     await message.answer(
#         f"✅ <b>Broadcast завершено</b>\n\n"
#         f"📤 Надіслано: {sent}\n"
#         f"❌ Не вдалось: {failed}",
#         parse_mode="HTML",
#     )


# ─── /stats (тільки адмін) ───────────────────────────────────────

@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not message.from_user:
        return
    if message.from_user.id != settings.ADMIN_TELEGRAM_ID:
        return

    from sqlalchemy import select, func
    from app.database.models import PriceHistory, Notification, NotificationStatus

    async with get_session() as session:

        # Юзери
        total_users = (await session.execute(
            select(func.count()).select_from(User)
        )).scalar_one()

        active_users = (await session.execute(
            select(func.count()).select_from(User)
            .where(User.is_blocked == False, User.bot_blocked == False)
        )).scalar_one()

        premium_users = (await session.execute(
            select(func.count()).select_from(User)
            .where(User.tier == "premium")
        )).scalar_one()

        # Трекери
        total_trackers = (await session.execute(
            select(func.count()).select_from(PriceTracker)
        )).scalar_one()

        active_trackers = (await session.execute(
            select(func.count()).select_from(PriceTracker)
            .where(PriceTracker.status == TrackerStatus.ACTIVE)
        )).scalar_one()

        error_trackers = (await session.execute(
            select(func.count()).select_from(PriceTracker)
            .where(PriceTracker.status == TrackerStatus.ERROR)
        )).scalar_one()

        # Перевірки за останні 24 години
        from datetime import datetime, timezone, timedelta
        since = datetime.now(timezone.utc) - timedelta(hours=24)

        checks_24h = (await session.execute(
            select(func.count()).select_from(PriceHistory)
            .where(PriceHistory.recorded_at >= since)
        )).scalar_one()

        # Сповіщення за останні 24 години
        notif_24h = (await session.execute(
            select(func.count()).select_from(Notification)
            .where(
                Notification.created_at >= since,
                Notification.status == NotificationStatus.SENT,
            )
        )).scalar_one()

        # Домени — скільки трекерів на кожен сайт
        domain_rows = (await session.execute(
            select(PriceTracker.source, func.count().label("cnt"))
            .where(PriceTracker.status == TrackerStatus.ACTIVE)
            .group_by(PriceTracker.source)
            .order_by(func.count().desc())
        )).all()

        # Мови юзерів
        lang_rows = (await session.execute(
            select(User.language_code, func.count().label("cnt"))
            .group_by(User.language_code)
            .order_by(func.count().desc())
        )).all()

    domains_text = "\n".join(
        f"  • {row.source}: {row.cnt}" for row in domain_rows
    ) or "  немає"
    langs_text = ", ".join(f"{row.language_code}: {row.cnt}" for row in lang_rows) or "—"

    # Прогрес-бар для помилок
    error_pct = int(error_trackers / total_trackers * 100) if total_trackers else 0
    health_bar = "🟢" if error_pct < 10 else "🟡" if error_pct < 30 else "🔴"

    await message.answer(
        f"📊 <b>Статистика ЦінаБот</b>\n"
        f"<i>{now_kyiv().strftime('%d.%m.%Y %H:%M')} Київ</i>\n\n"

        f"👥 <b>Юзери</b>\n"
        f"  Всього: {total_users}\n"
        f"  Активні: {active_users}\n"
        f"  Premium: {premium_users} 💰\n"
        f"  Мови: {langs_text}\n\n"

        f"🔍 <b>Трекери</b>\n"
        f"  Всього: {total_trackers}\n"
        f"  Активні: {active_trackers}\n"
        f"  З помилками: {error_trackers} {health_bar}\n\n"

        f"📈 <b>За останні 24 год</b>\n"
        f"  Перевірок цін: {checks_24h}\n"
        f"  Сповіщень відправлено: {notif_24h}\n\n"

        f"🌐 <b>Майданчики</b>\n"
        f"{domains_text}",
        parse_mode="HTML",
    )


# ─── Оплата Premium ──────────────────────────────────────────────

# ─── Оплата через IBAN ───────────────────────────────────────────

@router.callback_query(F.data == "pay_premium")
async def cmd_pay_premium(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return

    user = await get_or_create_user(callback.from_user)
    lang = user.lang

    if user.is_premium:
        await callback.answer(t(lang, "already_premium_alert"), show_alert=True)
        return

    if not settings.CARD_NUMBER:
        # Реквізитів у .env немає — не показуємо порожню інструкцію
        await callback.message.answer(t(lang, "pay_unavailable"), parse_mode="HTML")
        await callback.answer()
        return

    # Формуємо унікальне призначення платежу щоб легко знайти в банку
    payment_ref = f"PREM-{user.telegram_id}"

    await callback.message.answer(
        t(
            lang, "pay_instructions",
            price=rt.cached("PREMIUM_PRICE_UAH"),
            card=settings.CARD_NUMBER,
            ref=payment_ref,
        ),
        parse_mode="HTML",
    )
    await callback.answer()

    # Повідомляємо адміна про намір оплати
    if settings.ADMIN_TELEGRAM_ID and callback.bot:
        username = f"@{callback.from_user.username}" if callback.from_user.username else "без юзернейму"
        try:
            await callback.bot.send_message(
                chat_id=int(settings.ADMIN_TELEGRAM_ID),
                text=(
                    f"💰 <b>Хтось хоче Premium!</b>\n\n"
                    f"👤 {callback.from_user.first_name} {username} [{lang}]\n"
                    f"🆔 Telegram ID: <code>{callback.from_user.id}</code>\n"
                    f"🔑 Ref: <code>{payment_ref}</code>\n\n"
                    f"Для активації після оплати:\n"
                    f"<code>/activate {callback.from_user.id}</code>"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


# ─── /activate (тільки адмін) ────────────────────────────────────

@router.message(Command("activate"))
async def cmd_activate(message: Message) -> None:
    """
    Ручна активація Premium адміном після перевірки оплати.
    Використання: /activate 123456789
    Або з терміном: /activate 123456789 60  (60 днів)
    """
    if not message.from_user:
        return
    if message.from_user.id != settings.ADMIN_TELEGRAM_ID:
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "Використання:\n"
            "<code>/activate TELEGRAM_ID</code>\n"
            "<code>/activate TELEGRAM_ID 60</code> (днів)\n\n"
            "Приклад: <code>/activate 123456789</code>",
            parse_mode="HTML",
        )
        return

    try:
        target_telegram_id = int(parts[1])
        days = int(parts[2]) if len(parts) > 2 else 30
    except ValueError:
        await message.answer("❌ Невірний формат. Telegram ID має бути числом.")
        return

    from sqlalchemy import select
    from datetime import datetime, timezone, timedelta

    async with get_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == target_telegram_id)
        )).scalar_one_or_none()

        if not user:
            await message.answer(f"❌ Юзер з ID {target_telegram_id} не знайдений.")
            return

        was_premium = user.is_premium
        until = extend_premium(user, days)
        name = user.first_name or "Юзер"
        user_lang = user.lang
        user_db_id = user.id
        until_str = fmt_date(until, user_lang)

    # Premium знімає ліміт — вмикаємо товари, що чекали на слот
    activated, _ = await sync_free_limit(user_db_id)

    await message.answer(
        f"✅ <b>Premium {'продовжено' if was_premium else 'активовано'}!</b>\n\n"
        f"👤 {name} (ID: {target_telegram_id})\n"
        f"📅 Діє до: {until_str} (+{days} днів)"
        + (f"\n▶️ Знято з паузи товарів: {len(activated)}" if activated else ""),
        parse_mode="HTML",
    )

    # Повідомляємо юзера — його мовою
    try:
        await message.bot.send_message(
            chat_id=target_telegram_id,
            text=t(user_lang, "premium_activated", until=until_str),
            parse_mode="HTML",
        )
    except Exception as e:
        await message.answer(f"⚠️ Не вдалось повідомити юзера: {e}")



'''


@router.callback_query(F.data == "pay_premium")
async def cmd_pay_premium(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return

    # Якщо LiqPay не налаштований — показуємо заглушку
    if not settings.LIQPAY_PUBLIC_KEY:
        await callback.message.answer(
            "⏳ Оплата ще налаштовується.\n"
            "Напиши адміну: @твій_юзернейм"
        )
        await callback.answer()
        return

    user = await get_or_create_user(callback.from_user)

    if user.is_premium:
        await callback.answer("У тебе вже є Premium! ⭐", show_alert=True)
        return

    from app.bot.payments import create_payment_url
    payment_url = create_payment_url(user.id, user.telegram_id)

    await callback.message.answer(
        f"💳 <b>Оплата Premium</b>\n\n"
        f"Сума: <b>{rt.cached("PREMIUM_PRICE_UAH")} грн</b>\n"
        f"Термін: 30 днів\n\n"
        f"Після оплати натисни кнопку нижче щоб активувати.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"💳 Перейти до оплати",
                url=payment_url,
            )],
            [InlineKeyboardButton(
                text="✅ Я оплатив — активувати",
                callback_data=f"check_payment:{user.id}",
            )],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("check_payment:"))
async def check_payment(callback: CallbackQuery) -> None:
    """
    Юзер натиснув "Я оплатив" — перевіряємо через LiqPay API.
    Polling підхід: шукаємо останній успішний платіж цього юзера.
    """
    if not callback.from_user or not callback.message:
        return

    user_id = callback.data.split(":", 1)[1]
    user = await get_or_create_user(callback.from_user)

    # Перевірка що це той самий юзер
    if user.id != user_id:
        await callback.answer("Помилка", show_alert=True)
        return

    await callback.answer("⏳ Перевіряю оплату...")

    from app.bot.payments import check_payment_status, activate_premium
    import time

    # Шукаємо order_id для цього юзера за останні 30 хвилин
    # Перебираємо можливі order_id (по timestamp)
    now = int(time.time())
    found = False

    for ts in range(now, now - 1800, -1):  # 30 хвилин назад
        order_id = f"premium_{user_id}_{ts}"
        status = await check_payment_status(order_id)

        if status == "success":
            await activate_premium(user_id)
            await callback.message.edit_text(
                "🎉 <b>Premium активовано!</b>\n\n"
                "✅ Необмежена кількість товарів\n"
                # "✅ Перевірка щогодини\n\n"
                "Дякую за підтримку! 🙏",
                parse_mode="HTML",
            )
            found = True
            break
        elif status in ("wait_accept", "processing"):
            await callback.message.edit_text(
                "⏳ Платіж обробляється...\n"
                "Спробуй ще раз через хвилину.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="🔄 Перевірити ще раз",
                        callback_data=f"check_payment:{user_id}",
                    )]
                ]),
            )
            found = True
            break

    if not found:
        await callback.message.edit_text(
            "❌ Оплату не знайдено.\n\n"
            "Якщо ти щойно оплатив — зачекай 1-2 хвилини і спробуй знову.\n"
            "Якщо проблема не зникає — напиши /feedback",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Перевірити ще раз",
                    callback_data=f"check_payment:{user_id}",
                )]
            ]),
        )
'''

@router.message(Command("info"))
async def cmd_info(message: Message) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(message.from_user)
    lang = user.lang
    await message.answer(
        t(
            lang, "info",
            n=rt.cached("FREE_TIER_MAX_ITEMS"),
            free_hours=rt.cached("FREE_TIER_CHECK_INTERVAL_MINUTES") // 60,
            price=rt.cached("PREMIUM_PRICE_UAH"),
            contacts=t(lang, "contacts"),
        ),
        parse_mode="HTML",
    )
