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
import asyncio
import structlog

from app.config import settings
from app.database.session import get_session
from app.database.models import PriceTracker, TrackerStatus, User
from app.services.scrapers.scraper import ScraperFactory, ScrapeError

logger = structlog.get_logger(__name__)
router = Router(name="main")


class AddTrackerStates(StatesGroup):
    waiting_for_url = State()
    waiting_for_target_price = State()


# ─── DB helpers ──────────────────────────────────────────────────

async def get_or_create_user(from_user) -> User:
    """Завжди передавай event.from_user — там реальна людина."""
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
                )
                session.add(user)
                await session.flush()
                logger.info("new_user", telegram_id=from_user.id)
            except IntegrityError:
                # Race condition: інший запит вже створив юзера — просто завантажуємо
                await session.rollback()
                user = (await session.execute(
                    select(User).where(User.telegram_id == from_user.id)
                )).scalar_one()
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
    user = await get_or_create_user(message.from_user)
    name = message.from_user.first_name or "друже"

    # Текст залежить від того чи є Premium
    if user.is_premium:
        f"ℹ️ <b>ЦінаБот — моніторинг цін</b>\n\n"
        f"Автоматично стежу за цінами на Rozetka, OLX, Prom.ua "
        f"і сповіщаю коли ціна знизилась 📉\n\n"

        subtitle = "⭐ У тебе активний Premium — насолоджуйся!"
    else:
        subtitle = (

            f"🆓 Безкоштовно: до {settings.FREE_TIER_MAX_ITEMS} товарів\n"
            f"⭐ Premium {settings.PREMIUM_PRICE_UAH} грн/міс: кількість товарів у списку необмежено\n\n"
            
            f"/info - для детальної інформації.\n"
            f"/help - для доступних команд.\n"
        )

    # Кнопки — Premium тільки якщо не куплено
    buttons = [
        [InlineKeyboardButton(text="➕ Додати товар", callback_data="add_tracker")],
        [
            InlineKeyboardButton(text="📋 Мої товари", callback_data="my_list"),
            InlineKeyboardButton(text="🗑 Видалити", callback_data="delete_menu"),
        ],
    ]
    if not user.is_premium:
        buttons.append(
            [InlineKeyboardButton(text="⭐ Купити Premium", callback_data="premium_info")]
        )

    await message.answer(
        f"👋 Привіт, {name}!\n\n"
        
        f"<b>ЦінаБот</b> — автоматичний моніторинг цін на Rozetka, OLX, Prom.ua "
        f"і сповіщення, коли ціна знизилась 📉\n\n"
        
        f"{subtitle}\n\n"
    
        f"🆓 Безкоштовно: до {settings.FREE_TIER_MAX_ITEMS} товарів\n"
        f"⭐ Premium {settings.PREMIUM_PRICE_UAH} грн/міс: кількість товарів у списку необмежено\n\n"

        f"/info - для детальної інформації.\n"
        f"/help - для доступних команд.\n"

        "━━━━━━━━━━━━━━━\n"
        "📞 <b>Контакти для звернень:</b>\n"
        "Email: willy2005@gmail.com\n"
        "Зворотній зв'язок у боті: /feedback\n\n"

        "━━━━━━━━━━━━━━━\n"
        "🏢 <b>Виконавець:</b>\n"
        "👤 Фізична особа Желнов Павло,\n"
        "📍 м. Павлоград, Україна\n\n",

    reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
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


# ─── /premium ────────────────────────────────────────────────────

@router.message(Command("premium"))
@router.callback_query(F.data == "premium_info")
async def cmd_premium(event: Message | CallbackQuery) -> None:
    msg = event.message if isinstance(event, CallbackQuery) else event
    if not msg or not event.from_user:
        return

    user = await get_or_create_user(event.from_user)

    if user.is_premium:
        from datetime import timezone
        until = user.premium_until
        until_str = until.strftime("%d.%m.%Y") if until else "—"
        await msg.answer(
            f"⭐ <b>У тебе активний Premium!</b>\n\n"
            f"📅 Діє до: <b>{until_str}</b>\n\n"
            f"✅ Необмежена кількість товарів\n"
            f"✅ Пріоритетна перевірка цін\n\n"
            f"Дякуємо за підтримку! 🙏\n\n"
            f"<i>Для продовження підписки натисни кнопку нижче після закінчення терміну.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Продовжити Premium",
                    callback_data="pay_premium",
                )],
            ]),
            parse_mode="HTML",
        )
    else:
        await msg.answer(
            f"⭐ <b>Premium</b> — {settings.PREMIUM_PRICE_UAH} грн/місяць\n\n"
            f"✅ Необмежена кількість товарів\n"
            f"✅ Пріоритетна перевірка цін\n\n"
            f"<i>Оплата: Visa / Mastercard / Monobank</i>",
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
        "/feedback — відгук, ідея\n\n"
        "/info — інфо про сервіс\n\n"
        "<b>Як працює:</b>\n"
        "Надсилай посилання → отримуй сповіщення коли ціна впаде 📉",
        parse_mode="HTML",
    )


# ─── Feedback ────────────────────────────────────────────────────

class FeedbackState(StatesGroup):
    waiting = State()


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext) -> None:
    await message.answer(
        "💬 <b>Напиши своє повідомлення</b>\n\n"
        "Ідея, баг, питання — ділись, а я прочитаю і постараюсь відповісти.",
        parse_mode="HTML",
    )
    await state.set_state(FeedbackState.waiting)


@router.message(FeedbackState.waiting)
async def process_feedback(message: Message, state: FSMContext) -> None:
    if not message.text or not message.from_user or not message.bot:
        return
    await state.clear()

    username = f"@{message.from_user.username}" if message.from_user.username else "без юзернейму"

    try:
        await message.bot.send_message(
            chat_id=settings.ADMIN_TELEGRAM_ID,
            text=(
                f"💬 <b>Feedback</b>\n\n"
                f"👤 {message.from_user.first_name} {username}\n"
                f"🆔 <code>{message.from_user.id}</code>\n\n"
                f"{message.text}"
            ),
            parse_mode="HTML",
        )
        await message.answer("✅ Дякую! Я обов'язково прочитаю.")
    except Exception:
        await message.answer("✅ Отримано, дякую!")


# ─── Broadcast (тільки для адміна) ───────────────────────────────

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message) -> None:
    if not message.from_user or not message.bot:
        return

    # Тільки адмін може надсилати broadcast
    if message.from_user.id != settings.ADMIN_TELEGRAM_ID:
        return

    text = (message.text or "").removeprefix("/broadcast").strip()
    if not text:
        await message.answer(
            "Використання:\n<code>/broadcast Текст повідомлення</code>",
            parse_mode="HTML",
        )
        return

    from sqlalchemy import select as sa_select
    async with get_session() as session:
        users = (await session.execute(
            sa_select(User).where(User.is_blocked == False)
        )).scalars().all()

    await message.answer(f"⏳ Надсилаю {len(users)} юзерам...")

    sent = failed = 0
    for user in users:
        try:
            await message.bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                parse_mode="HTML",
            )
            sent += 1
            # Telegram дозволяє ~30 повідомлень/сек групам і ~20/сек юзерам
            await asyncio.sleep(0.05)
        except Exception as e:
            if "bot was blocked" in str(e).lower():
                async with get_session() as s:
                    u = await s.get(User, user.id)
                    if u:
                        u.is_blocked = True
            failed += 1

    await message.answer(
        f"✅ <b>Broadcast завершено</b>\n\n"
        f"📤 Надіслано: {sent}\n"
        f"❌ Не вдалось: {failed}",
        parse_mode="HTML",
    )


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
            .where(User.is_blocked == False)
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

    domains_text = "\n".join(
        f"  • {row.source}: {row.cnt}" for row in domain_rows
    ) or "  немає"

    # Прогрес-бар для помилок
    error_pct = int(error_trackers / total_trackers * 100) if total_trackers else 0
    health_bar = "🟢" if error_pct < 10 else "🟡" if error_pct < 30 else "🔴"

    await message.answer(
        f"📊 <b>Статистика ЦінаБот</b>\n"
        f"<i>{datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC</i>\n\n"

        f"👥 <b>Юзери</b>\n"
        f"  Всього: {total_users}\n"
        f"  Активні: {active_users}\n"
        f"  Premium: {premium_users} 💰\n\n"

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
        f"Сума: <b>{settings.PREMIUM_PRICE_UAH} грн</b>\n"
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


@router.message(Command("info"))
async def cmd_info(message: Message) -> None:
    await message.answer(
        "ℹ️ <b>ЦінаБот — моніторинг цін</b>\n\n"
        "Автоматично стежу за цінами на Rozetka, OLX, Prom.ua "
        "і сповіщаю коли ціна знизилась 📉\n\n"

        "Додай товар → отримай сповіщення коли ціна впаде 📉\n\n"
        "📍 Де шукаю знижки:\n"
        "- Rozetka — так\n"
        "- OLX — так\n"  
        "- Prom.ua — так\n"
        "- У сусіда в гаражі — в стадії розробки 😅.\n\n"

        "━━━━━━━━━━━━━━━\n"
        "📦 <b>Послуги та ціни:</b>\n"
        "🆓 Безкоштовний план:\n"
        "  • до 5 товарів одночасно\n"
        "  • перевірка кожні 12 годин\n"
        "  • сповіщення при зниженні ціни\n\n"
        
        f"⭐ Premium — {settings.PREMIUM_PRICE_UAH} грн/місяць:\n"
        "  • необмежена кількість товарів\n"
        "  • перевірка кожні 2 години\n\n"
        
        "💳 <b>Оплата:</b> LiqPay (Visa / Mastercard / Monobank / ПриватБанк)\n\n"

        "━━━━━━━━━━━━━━━\n"
        "🔄 <b>Умови повернення:</b>\n"
        "Повернення коштів протягом 14 днів з моменту оплати, "
        "якщо послуга не надавалась або не відповідає опису. "
        
        "Для повернення — звернутись на email нижче.\n\n"
        
        "━━━━━━━━━━━━━━━\n"
        "📞 <b>Контакти для звернень:</b>\n"
        "Email: willy2005@gmail.com\n"
        "Зворотній зв'язок у боті: /feedback\n\n"
        
        "━━━━━━━━━━━━━━━\n"
        "🏢 <b>Виконавець:</b>\n"        
        "👤 Фізична особа Желнов Павло,\n"
        "📍 м. Павлоград, Україна\n\n"

        "<i>⚠️ Сервіс надається «як є». "
        "Точність цін залежить від зовнішніх майданчиків. \n"
        "Повернення коштів — протягом 14 днів якщо послуга не надавалась.</i>\n\n"
        "/help - допомога по поточним командам\n",
        parse_mode="HTML",
    )
