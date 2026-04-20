"""
Bot handlers — чиста версія.

Головне правило:
  event.from_user  → завжди реальна людина (і для Message, і для CallbackQuery)
  msg.from_user    → НЕ використовуємо для отримання юзера,
                     бо для callback msg — це повідомлення бота
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
import structlog
import asyncio
from app.config import settings
from app.database.session import get_session
from app.database.models import PriceTracker, TrackerStatus, User
from app.services.scrapers.scraper import ScraperFactory, ScrapeError

logger = structlog.get_logger(__name__)
router = Router(name="main")


class AddTrackerStates(StatesGroup):
    waiting_for_url = State()
    waiting_for_target_price = State()

class FeedbackState(StatesGroup):
    waiting = State()

# ─── DB helpers ──────────────────────────────────────────────────

async def get_or_create_user(from_user) -> User:
    """Завжди передавай event.from_user — там реальна людина."""
    from sqlalchemy import select
    async with get_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == from_user.id)
        )).scalar_one_or_none()

        if not user:
            user = User(
                telegram_id=from_user.id,
                username=getattr(from_user, "username", None),
                first_name=getattr(from_user, "first_name", None),
                language_code=getattr(from_user, "language_code", None) or "uk",
            )
            session.add(user)
            await session.flush()
            logger.info("new_user", telegram_id=from_user.id)
        return user


async def count_trackers(user_id: str) -> int:
    from sqlalchemy import select, func
    async with get_session() as session:
        return (await session.execute(
            select(func.count()).where(
                PriceTracker.user_id == user_id,
                PriceTracker.status != TrackerStatus.ERROR,
            )
        )).scalar_one()


async def get_trackers(user_id: str) -> list[PriceTracker]:
    from sqlalchemy import select
    async with get_session() as session:
        return (await session.execute(
            select(PriceTracker)
            .where(
                PriceTracker.user_id == user_id,
                PriceTracker.status != TrackerStatus.ERROR,
            )
            .order_by(PriceTracker.created_at.desc())
        )).scalars().all()


# ─── /start ──────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not message.from_user:
        return
    await get_or_create_user(message.from_user)
    name = message.from_user.first_name or "друже"
    await message.answer(
        f"👋 Привіт, {name}!\n\n"
        f"Я <b>PriceGuard</b> — стежу за цінами на Rozetka, OLX, Prom.ua 📉\n\n"
        f"<b>Безкоштовно:</b> до {settings.FREE_TIER_MAX_ITEMS} товарів\n"
        f"<b>Premium ({settings.PREMIUM_PRICE_UAH} грн/міс):</b> необмежено + щогодинна перевірка",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Додати товар", callback_data="add_tracker")],
            [
                InlineKeyboardButton(text="📋 Мої товари", callback_data="my_list"),
                InlineKeyboardButton(text="🗑 Видалити", callback_data="delete_menu"),
            ],
            [InlineKeyboardButton(text="⭐ Premium", callback_data="premium_info")],
        ]),
        parse_mode="HTML",
    )


# ─── /add ────────────────────────────────────────────────────────

@router.message(Command("add"))
@router.callback_query(F.data == "add_tracker")
async def cmd_add(event: Message | CallbackQuery, state: FSMContext) -> None:
    msg = event.message if isinstance(event, CallbackQuery) else event
    if not msg:
        return

    domains = "\n".join(f"• {d}" for d in ScraperFactory.supported_domains())
    await msg.answer(
        f"🔗 <b>Надішли посилання на товар</b>\n\n{domains}\n\n"
        f"<i>Просто встав URL зі сторінки товару</i>",
        parse_mode="HTML",
    )
    await state.set_state(AddTrackerStates.waiting_for_url)
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.message(AddTrackerStates.waiting_for_url)
async def process_url(message: Message, state: FSMContext) -> None:
    if not message.text or not message.from_user:
        return

    url = message.text.strip()
    scraper = ScraperFactory.get(url)
    if not scraper:
        await message.answer(
            f"❌ Цей сайт не підтримується.\n"
            f"Підтримую: {', '.join(ScraperFactory.supported_domains())}",
        )
        return

    # Перевірка ліміту
    user = await get_or_create_user(message.from_user)
    count = await count_trackers(user.id)

    if not user.is_premium and count >= settings.FREE_TIER_MAX_ITEMS:
        await message.answer(
            f"⚠️ <b>Ліміт досягнуто!</b>\n\n"
            f"У тебе {count}/{settings.FREE_TIER_MAX_ITEMS} товарів на безкоштовному плані.\n"
            f"Перейди на Premium: /premium",
            parse_mode="HTML",
        )
        await state.clear()
        return

    wait = await message.answer("⏳ Перевіряю товар...")
    result = await scraper.scrape(url)

    if isinstance(result, ScrapeError):
        await wait.edit_text("❌ Не вдалося отримати інформацію. Перевір посилання.")
        return

    price_text = f"{result.price:,.0f} грн" if result.price else "невідома"
    avail = "✅" if result.is_available else "❌ немає"

    await state.update_data(
        url=url,
        canonical_url=result.canonical_url,
        source=scraper.domain,
        title=result.title,
        price=str(result.price) if result.price else None,
    )
    await wait.edit_text(
        f"📦 <b>{result.title}</b>\n\n"
        f"💰 {price_text}  {avail}\n\n"
        f"Встановити цільову ціну? (сповіщу коли впаде)\n"
        f"Введи суму або натисни «Пропустити»",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустити", callback_data="skip_target")]
        ]),
        parse_mode="HTML",
    )
    await state.set_state(AddTrackerStates.waiting_for_target_price)


@router.message(AddTrackerStates.waiting_for_target_price)
async def process_target(message: Message, state: FSMContext) -> None:
    if not message.text or not message.from_user:
        return
    try:
        target = Decimal(message.text.strip().replace(",", ".").replace(" ", ""))
        if target <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        await message.answer("❌ Введи суму числом: <code>4500</code>", parse_mode="HTML")
        return
    await _save_tracker(message.from_user, state, message, target)


@router.callback_query(F.data == "skip_target")
async def skip_target(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not callback.message:
        return
    await _save_tracker(callback.from_user, state, callback.message, None)
    await callback.answer()


async def _save_tracker(from_user, state, msg, target_price):
    data = await state.get_data()
    await state.clear()

    user = await get_or_create_user(from_user)
    count = await count_trackers(user.id)

    if not user.is_premium and count >= settings.FREE_TIER_MAX_ITEMS:
        await msg.answer(f"⚠️ Ліміт {settings.FREE_TIER_MAX_ITEMS} товарів. /premium")
        return

    async with get_session() as session:
        from sqlalchemy import select
        exists = (await session.execute(
            select(PriceTracker).where(
                PriceTracker.user_id == user.id,
                PriceTracker.canonical_url == data["canonical_url"],
            )
        )).scalar_one_or_none()

        if exists:
            await msg.answer("⚠️ Цей товар вже відстежується!")
            return

        session.add(PriceTracker(
            user_id=user.id,
            url=data["url"],
            canonical_url=data["canonical_url"],
            source=data["source"],
            title=data.get("title"),
            current_price=Decimal(data["price"]) if data.get("price") else None,
            target_price=target_price,
            status=TrackerStatus.ACTIVE,
        ))

    slot_text = (
        f"\n\n<i>Слотів використано: {count + 1}/{settings.FREE_TIER_MAX_ITEMS}</i>"
        if not user.is_premium else ""
    )
    target_text = f"\n🎯 Ціль: {target_price:,.0f} грн" if target_price else ""

    await msg.answer(
        f"✅ <b>Додано!</b>\n\n"
        f"📦 {data.get('title', '')[:60]}"
        f"{target_text}{slot_text}",
        parse_mode="HTML",
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


async def _show_list(user: User, msg: Message) -> None:
    trackers = await get_trackers(user.id)

    if not trackers:
        await msg.answer(
            "📭 Ти ще нічого не відстежуєш.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Додати товар", callback_data="add_tracker")]
            ]),
        )
        return

    lines = [f"📋 <b>Твої товари ({len(trackers)}):</b>\n"]
    for i, t in enumerate(trackers, 1):
        price = f"{t.current_price:,.0f} грн" if t.current_price else "—"
        icon = "✅" if t.status == TrackerStatus.ACTIVE else "⏸"
        target = f" → 🎯{t.target_price:,.0f}" if t.target_price else ""
        lines.append(f"{i}. {icon} {(t.title or 'Без назви')[:40]}\n   💰 {price}{target}")

    if user.is_premium:
        plan = "⭐ Premium"
    else:
        plan = f"Безкоштовний ({len(trackers)}/{settings.FREE_TIER_MAX_ITEMS})"
    lines.append(f"\n<i>{plan}</i>")

    await msg.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Додати", callback_data="add_tracker"),
                InlineKeyboardButton(text="🗑 Видалити", callback_data="delete_menu"),
            ],
        ]),
        parse_mode="HTML",
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
    trackers = await get_trackers(user.id)

    if not trackers:
        await msg.answer("📭 Немає товарів для видалення.")
        return

    buttons = []
    for t in trackers:
        price_str = f" · {t.current_price:,.0f}₴" if t.current_price else ""
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {(t.title or 'Без назви')[:35]}{price_str}",
            callback_data=f"del:{t.id}",
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="my_list")])

    await msg.answer(
        "🗑 <b>Вибери товар для видалення:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("del:"))
async def confirm_delete(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return

    tracker_id = callback.data.split(":", 1)[1]
    user = await get_or_create_user(callback.from_user)

    async with get_session() as session:
        tracker = await session.get(PriceTracker, tracker_id)
        if not tracker:
            await callback.answer("Товар не знайдено")
            return
        if tracker.user_id != user.id:
            await callback.answer("❌ Немає доступу")
            return
        title = (tracker.title or "Товар")[:50]

    await callback.message.edit_text(
        f"❓ <b>Видалити?</b>\n\n📦 {title}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Так", callback_data=f"delok:{tracker_id}"),
                InlineKeyboardButton(text="❌ Ні", callback_data="delete_menu"),
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

    async with get_session() as session:
        tracker = await session.get(PriceTracker, tracker_id)
        if not tracker or tracker.user_id != user.id:
            await callback.answer("Помилка")
            return
        title = (tracker.title or "Товар")[:50]
        await session.delete(tracker)

    count_after = await count_trackers(user.id)
    await callback.message.edit_text(
        f"✅ <b>Видалено!</b>\n\n📦 {title}\n\n"
        f"<i>Залишилось: {count_after} товарів</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Список", callback_data="my_list"),
                InlineKeyboardButton(text="➕ Додати", callback_data="add_tracker"),
            ]
        ]),
        parse_mode="HTML",
    )
    await callback.answer("Видалено ✅")


# ─── /feedback ────────────────────────────────────────────────────
@router.message(Command("broadcast"), F.from_user.id == settings.ADMIN_TELEGRAM_ID)
async def cmd_broadcast(message: Message) -> None:
    # Текст після команди: /broadcast Увага! Планове обслуговування о 22:00
    text = message.text.removeprefix("/broadcast").strip()
    if not text:
        await message.answer("Вкажи текст: /broadcast Текст повідомлення")
        return

    async with get_session() as session:
        from sqlalchemy import select
        users = (await session.execute(
            select(User).where(User.is_blocked == False)
        )).scalars().all()

    sent = 0
    failed = 0
    for user in users:
        try:
            await message.bot.send_message(user.telegram_id, text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)  # 20 повідомлень/сек — ліміт Telegram
        except Exception:
            failed += 1

    await message.answer(f"✅ Надіслано: {sent}\n❌ Не вдалось: {failed}")

@router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext) -> None:
    await message.answer("💬 Напиши своє повідомлення — я передам розробнику:")
    await state.set_state(FeedbackState.waiting)

@router.message(FeedbackState.waiting)
async def process_feedback(message: Message, state: FSMContext) -> None:
    await state.clear()
    # Пересилаємо тобі
    await message.bot.send_message(
        settings.ADMIN_TELEGRAM_ID,
        f"💬 Feedback від @{message.from_user.username} "
        f"(id={message.from_user.id}):\n\n{message.text}"
    )
    await message.answer("✅ Дякую! Я обов'язково прочитаю.")

# ─── /premium ────────────────────────────────────────────────────

@router.message(Command("premium"))
@router.callback_query(F.data == "premium_info")
async def cmd_premium(event: Message | CallbackQuery) -> None:
    msg = event.message if isinstance(event, CallbackQuery) else event
    if not msg:
        return
    await msg.answer(
        f"⭐ <b>PriceGuard Premium</b> — {settings.PREMIUM_PRICE_UAH} грн/місяць\n\n"
        f"✅ Необмежена кількість товарів\n"
        f"✅ Перевірка щогодини (замість 6 год)\n"
        f"✅ Графік зміни цін\n\n"
        f"<i>Оплата: Visa/Mastercard/Monobank</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"💳 Оплатити {settings.PREMIUM_PRICE_UAH} грн",
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
    await message.answer(
        "<b>Команди:</b>\n\n"
        "/add — додати товар\n"
        "/list — мої товари\n"
        "/delete — видалити товар\n"
        "/premium — підписка\n\n"
        "<b>Як працює:</b>\n"
        "Надсилай посилання → отримуй сповіщення коли ціна впаде 📉",
        parse_mode="HTML",
    )