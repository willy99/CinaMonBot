"""
«Мій список бажань» посиланням: /share → t.me/<бот>?start=wl_<token>.

Хто відкриє посилання, бачить назви товарів власника, поточні ціни й магазини і може
одним дотиком почати стежити за товаром сам. Не показуємо: цілі власника, його
username чи Telegram ID (лише ім'я з профілю). Власнику ніхто нічого не надсилає.
Посилання можна перевипустити (старе перестає працювати) або вимкнути.
"""
from __future__ import annotations

import re
import secrets
from html import escape as html_escape
from urllib.parse import quote

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.database.models import PriceTracker, TrackerStatus, User
from app.database.session import get_session
from app.i18n import fmt_money, t
from app.services.limits import sync_free_limit

logger = structlog.get_logger(__name__)
router = Router(name="wishlist")

SHARE_MAX_ITEMS = 20
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{8,24}")
_SHOWN = (TrackerStatus.ACTIVE, TrackerStatus.PAUSED, TrackerStatus.PAUSED_LIMIT, TrackerStatus.PAUSED_DOMAIN)
FALLBACK_BOT_USERNAME = "cina_mon_bot"


def new_token() -> str:
    return secrets.token_urlsafe(9)            # 12 символів з [A-Za-z0-9_-]


async def bot_username(bot) -> str:
    try:
        me = await bot.me()
        if isinstance(getattr(me, "username", None), str) and me.username:
            return me.username
    except Exception:  # noqa: BLE001
        pass
    return FALLBACK_BOT_USERNAME


async def _shared_items(owner_id: str) -> list[PriceTracker]:
    async with get_session() as s:
        return (await s.execute(
            select(PriceTracker)
            .where(PriceTracker.user_id == owner_id, PriceTracker.status.in_(_SHOWN))
            .order_by(PriceTracker.created_at.asc())
            .limit(SHARE_MAX_ITEMS)
        )).scalars().all()


async def _owner_by_token(token: str) -> User | None:
    if not TOKEN_RE.fullmatch(token or ""):
        return None
    async with get_session() as s:
        return (await s.execute(select(User).where(User.share_token == token))).scalar_one_or_none()


# ─── Власник: /share ─────────────────────────────────────────────

def _share_keyboard(lang: str, link: str) -> InlineKeyboardMarkup:
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(t(lang, 'share_send_text'), safe='')}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_share_send"), url=share_url)],
        [InlineKeyboardButton(text=t(lang, "btn_share_rotate"), callback_data="share:new"),
         InlineKeyboardButton(text=t(lang, "btn_share_off"), callback_data="share:off")],
    ])


async def _set_token(user_id: str, token: str | None) -> None:
    async with get_session() as s:
        u = await s.get(User, user_id)
        if u:
            u.share_token = token


async def _show_share(msg: Message, user: User, *, rotate: bool = False, edit: bool = False) -> None:
    lang = user.lang
    items = await _shared_items(user.id)
    if not items:
        await msg.answer(t(lang, "share_empty"), parse_mode="HTML")
        return
    token = user.share_token
    if rotate or not token:
        token = new_token()
        await _set_token(user.id, token)
        logger.info("wishlist_token_issued", user=user.telegram_id, rotated=rotate)
    link = f"https://t.me/{await bot_username(msg.bot)}?start=wl_{token}"
    text = t(lang, "share_intro", link=link, n=len(items))
    if edit:
        await msg.edit_text(text, reply_markup=_share_keyboard(lang, link), parse_mode="HTML", disable_web_page_preview=True)
    else:
        await msg.answer(text, reply_markup=_share_keyboard(lang, link), parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("share"))
async def cmd_share(message: Message) -> None:
    if not message.from_user:
        return
    from app.bot.handlers.main import get_or_create_user
    user = await get_or_create_user(message.from_user)
    await _show_share(message, user)


@router.callback_query(F.data == "share_menu")
async def share_menu_cb(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    from app.bot.handlers.main import get_or_create_user
    user = await get_or_create_user(callback.from_user)
    await _show_share(callback.message, user)
    await callback.answer()


@router.callback_query(F.data.in_({"share:new", "share:off"}))
async def share_action_cb(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    from app.bot.handlers.main import get_or_create_user
    user = await get_or_create_user(callback.from_user)
    lang = user.lang
    if callback.data == "share:off":
        await _set_token(user.id, None)
        logger.info("wishlist_sharing_off", user=user.telegram_id)
        await callback.message.edit_text(t(lang, "share_off_done"), parse_mode="HTML")
        await callback.answer()
        return
    await _show_share(callback.message, user, rotate=True, edit=True)
    await callback.answer(t(lang, "share_rotated_toast"))


# ─── Гість: відкрив посилання ────────────────────────────────────

async def show_shared_list(message: Message, viewer: User, token: str) -> None:
    lang = viewer.lang
    owner = await _owner_by_token(token)
    if owner is None:
        await message.answer(t(lang, "shared_invalid"), parse_mode="HTML")
        return
    if owner.id == viewer.id:
        await message.answer(t(lang, "shared_own"), parse_mode="HTML")
        return
    items = await _shared_items(owner.id)
    if not items:
        await message.answer(t(lang, "shared_empty"), parse_mode="HTML")
        return
    name = html_escape(owner.first_name or t(lang, "shared_friend"), quote=False)
    lines = [t(lang, "shared_title", name=name), ""]
    for i, tr in enumerate(items, 1):
        title = html_escape((tr.title or t(lang, "item"))[:60], quote=False)
        price = fmt_money(tr.current_price, tr.currency, lang) if tr.current_price else t(lang, "price_unknown")
        url = html_escape(tr.canonical_url, quote=True)
        lines.append(f'{i}. <a href="{url}">{title}</a> — {price} · {html_escape(tr.source, quote=False)}')
    lines += ["", t(lang, "shared_hint")]
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, tr in enumerate(items, 1):
        row.append(InlineKeyboardButton(text=f"➕ {i}", callback_data=f"wla:{tr.id}"))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text=t(lang, "btn_shared_add_all"), callback_data=f"wlall:{token}")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                         parse_mode="HTML", disable_web_page_preview=True)
    logger.info("wishlist_viewed", viewer=viewer.telegram_id, items=len(items))


async def _copy_tracker(viewer: User, tr: PriceTracker) -> str | None:
    from datetime import datetime

    from app.bot.handlers.main import DEFAULT_REMIND_MINUTES, create_tracker
    live_auction = tr.ends_at is not None and tr.ends_at > datetime.utcnow()
    return await create_tracker(
        viewer.id, url=tr.canonical_url, canonical_url=tr.canonical_url, source=tr.source,
        title=tr.title, price=tr.current_price, currency=tr.currency,
        ends_at=tr.ends_at if live_auction else None,
        remind_before_minutes=DEFAULT_REMIND_MINUTES if live_auction else None,
    )


async def _shareable_tracker(tracker_id: str) -> PriceTracker | None:
    """Трекер, який можна скопіювати: власник ще ділиться списком і трекер не помилковий."""
    async with get_session() as s:
        tr = await s.get(PriceTracker, tracker_id)
        if tr is None or tr.status not in _SHOWN:
            return None
        owner = await s.get(User, tr.user_id)
        if owner is None or not owner.share_token:
            return None
        return tr


@router.callback_query(F.data.startswith("wla:"))
async def shared_add_one_cb(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    from app.bot.handlers.main import get_or_create_user
    viewer = await get_or_create_user(callback.from_user)
    lang = viewer.lang
    tr = await _shareable_tracker(callback.data.split(":", 1)[1])
    if tr is None:
        await callback.answer(t(lang, "shared_invalid_toast"), show_alert=True)
        return
    if tr.user_id == viewer.id:
        await callback.answer(t(lang, "already_tracked"))
        return
    new_id = await _copy_tracker(viewer, tr)
    if new_id is None:
        await callback.answer(t(lang, "already_tracked"))
        return
    _, paused = await sync_free_limit(viewer.id)
    key = "shared_added_paused_toast" if any(p.id == new_id for p in paused) else "shared_added_toast"
    await callback.answer(t(lang, key, title=(tr.title or t(lang, "item"))[:50]), show_alert=key.endswith("paused_toast"))


@router.callback_query(F.data.startswith("wlall:"))
async def shared_add_all_cb(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    from app.bot.handlers.main import get_or_create_user
    viewer = await get_or_create_user(callback.from_user)
    lang = viewer.lang
    owner = await _owner_by_token(callback.data.split(":", 1)[1])
    if owner is None:
        await callback.answer(t(lang, "shared_invalid_toast"), show_alert=True)
        return
    if owner.id == viewer.id:
        await callback.answer(t(lang, "already_tracked"))
        return
    added = dup = 0
    for tr in await _shared_items(owner.id):
        if await _copy_tracker(viewer, tr) is None:
            dup += 1
        else:
            added += 1
    paused = len((await sync_free_limit(viewer.id))[1]) if added else 0
    text = t(lang, "shared_added_all", added=added, dup=dup)
    if paused:
        from app.services import runtime_settings as rt
        text += "\n" + t(lang, "bulk_paused", n=paused, limit=rt.cached("FREE_TIER_MAX_ITEMS"))
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()
