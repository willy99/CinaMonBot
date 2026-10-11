"""
Помічники для тестів бота: фейкові Telegram-об'єкти та перехоплення відповідей.

    from tests.helpers import make_message, make_callback, fsm_for, capture_bot

    async def test_x(db):
        with capture_bot() as sent:
            await h.cmd_start(make_message(1, "/start", lang="en"))
        assert "Hi" in sent[-1].text

Контракт:
- make_message(uid, text, lang) → aiogram Message, прив'язаний до MagicMock-бота
  (msg.bot.send_message — AsyncMock, можна перевіряти виклики адміну).
- make_callback(uid, data, lang) → CallbackQuery з тим самим ботом.
- capture_bot() патчить Message.answer / Message.edit_text / Message.edit_reply_markup /
  CallbackQuery.answer і збирає Sent(text, reply_markup, kind) у список.
- fsm_for(uid) → FSMContext на спільному MemoryStorage (новий на кожен виклик fsm_storage()).
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Document, User as TgUser

ADMIN_ID = 431742835


@dataclass
class Sent:
    text: str
    reply_markup: Any
    kind: str  # "answer" | "edit" | "markup" | "callback"

    @property
    def buttons(self) -> list[str]:
        if not self.reply_markup or not getattr(self.reply_markup, "inline_keyboard", None):
            return []
        return [b.text for row in self.reply_markup.inline_keyboard for b in row]

    @property
    def callbacks(self) -> list[str]:
        if not self.reply_markup or not getattr(self.reply_markup, "inline_keyboard", None):
            return []
        return [b.callback_data for row in self.reply_markup.inline_keyboard for b in row]


def make_bot() -> MagicMock:
    bot = MagicMock(name="bot")
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=777))
    bot.copy_message = AsyncMock(return_value=MagicMock(message_id=778))
    bot.forward_message = AsyncMock(return_value=MagicMock(message_id=779))
    bot.set_my_commands = AsyncMock()
    return bot


def tg_user(uid: int, lang: str = "uk", username: str | None = "tester", first_name: str = "Tester") -> TgUser:
    return TgUser(id=uid, is_bot=False, first_name=first_name, username=username, language_code=lang)


def make_message(uid: int, text: str | None = "/start", lang: str = "uk", *, bot: MagicMock | None = None,
                 photo: bool = False, document: bool = False, caption: str | None = None,
                 username: str | None = "tester") -> Message:
    kwargs: dict[str, Any] = {}
    if photo:
        kwargs["photo"] = [PhotoSize(file_id="photo-file-id", file_unique_id="u1", width=10, height=10, file_size=100)]
        kwargs["caption"] = caption
        text = None
    if document:
        kwargs["document"] = Document(file_id="doc-file-id", file_unique_id="u2", file_name="report.pdf",
                                      mime_type="application/pdf", file_size=1000)
        kwargs["caption"] = caption
        text = None
    msg = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=uid, type="private"),
        from_user=tg_user(uid, lang, username=username), text=text, **kwargs,
    )
    return msg.as_(bot or make_bot())


def make_callback(uid: int, data: str, lang: str = "uk", *, bot: MagicMock | None = None) -> CallbackQuery:
    bot = bot or make_bot()
    cb = CallbackQuery(
        id="cb1", from_user=tg_user(uid, lang), chat_instance="ci", data=data,
        message=make_message(uid, "menu", lang, bot=bot),
    )
    return cb.as_(bot)


_storage = MemoryStorage()


def fsm_storage() -> MemoryStorage:
    """Новий порожній сховок FSM (виклич на початку тесту, якщо потрібна ізоляція)."""
    global _storage
    _storage = MemoryStorage()
    return _storage


def fsm_for(uid: int) -> FSMContext:
    return FSMContext(storage=_storage, key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


@contextmanager
def capture_bot() -> Iterator[list[Sent]]:
    sent: list[Sent] = []

    async def answer(self, text, **kw):
        sent.append(Sent(text, kw.get("reply_markup"), "answer"))
        m = MagicMock(name="sent_message")
        m.edit_text = AsyncMock(side_effect=lambda t, **k: sent.append(Sent(t, k.get("reply_markup"), "edit")))
        return m

    async def edit_text(self, text, **kw):
        sent.append(Sent(text, kw.get("reply_markup"), "edit"))

    async def cb_answer(self, text=None, **kw):
        sent.append(Sent(text or "", None, "callback"))

    async def edit_reply_markup(self, reply_markup=None, **kw):
        # Лише клавіатура змінюється (mute/unmute під сповіщенням) — текст лишається
        sent.append(Sent("", reply_markup, "markup"))

    with patch.object(Message, "answer", answer), patch.object(Message, "edit_text", edit_text), \
         patch.object(Message, "edit_reply_markup", edit_reply_markup), \
         patch.object(CallbackQuery, "answer", cb_answer):
        yield sent
