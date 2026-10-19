"""
Повідомлення поза командами й діалогами: посилання на товар без /add, кілька посилань, файл зі списком.

Роутер підключається ОСТАННІМ (bot.py) і лише для стану «немає діалогу» (StateFilter(None)),
тож команди, фідбек, введення цілі та інші кроки мають пріоритет і цей обробник їх не перехоплює.

- 1 посилання → звичайний шлях /add (картка товару + питання про ціль);
- 2+ посилань → масове додавання (app/bot/bulk.py);
- файл .txt/.csv → посилання з файлу, далі так само;
- текст без посилань → коротка підказка, що надіслати.
"""
from __future__ import annotations

import io

import structlog
from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.links import extract_urls, is_link_list_file, urls_from_message
from app.i18n import t

logger = structlog.get_logger(__name__)
router = Router(name="inbox")


@router.message(StateFilter(None), F.document)
async def inbox_document(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    from app.bot.handlers.main import get_or_create_user
    user = await get_or_create_user(message.from_user)
    if user.is_blocked:
        return
    if not is_link_list_file(message.document):
        urls = urls_from_message(message)            # посилання в підписі до файлу
        if urls:
            await _route(message, state, user, urls)
        return
    try:
        buf = io.BytesIO()
        await message.bot.download(message.document, destination=buf)
        text = buf.getvalue().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        logger.warning("link_file_download_failed", error=str(e))
        await message.answer(t(user.lang, "bulk_file_failed"))
        return
    urls = extract_urls(text, limit=200)
    if not urls:
        await message.answer(t(user.lang, "bulk_file_empty"))
        return
    await _route(message, state, user, urls, from_file=True)


@router.message(StateFilter(None), F.text | F.caption)
async def inbox_text(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    text = message.text or message.caption or ""
    if text.startswith("/"):
        return                                       # невідома команда — не наша справа
    from app.bot.handlers.main import get_or_create_user
    user = await get_or_create_user(message.from_user)
    if user.is_blocked:
        return
    urls = urls_from_message(message)
    if not urls:
        if message.text:                             # підпис до фото без посилання — мовчимо
            await message.answer(t(user.lang, "inbox_hint"), parse_mode="HTML")
        return
    await _route(message, state, user, urls)


async def _route(message: Message, state: FSMContext, user, urls: list[str], from_file: bool = False) -> None:
    # Файл — завжди масово (навіть одне посилання: тексту повідомлення, з якого читає /add, немає)
    if len(urls) > 1 or from_file:
        from app.bot.bulk import bulk_add
        await bulk_add(message, user, urls)
        return
    # Одне посилання — той самий шлях, що й після /add (картка товару, питання про ціль)
    from app.bot.handlers.main import AddTrackerStates, process_url
    await state.set_state(AddTrackerStates.waiting_for_url)
    await process_url(message, state)
