"""
Outer-middleware диспетчера:
- BlockedUserMiddleware — оновлення від заблокованих адміном далі не йдуть (кнопку закриваємо мовчки, оплату — відмовою);
- UserRateLimitMiddleware — не більше USER_REQUESTS_PER_MINUTE апдейтів на користувача за хвилину.
"""
from __future__ import annotations

import time

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, PreCheckoutQuery, TelegramObject
from sqlalchemy import select

from botkit.context import ctx

logger = structlog.get_logger(__name__)


async def is_blocked(telegram_id: int) -> bool:
    User = ctx().models.User  # noqa: N806
    async with ctx().get_session() as s:
        return bool((await s.execute(
            select(User.is_blocked).where(User.telegram_id == telegram_id)
        )).scalar_one_or_none())


class BlockedUserMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is None or not await is_blocked(user.id):
            return await handler(event, data)
        logger.info("blocked_user_update_dropped", telegram_id=user.id)
        if isinstance(event, CallbackQuery):
            await event.answer()
        elif isinstance(event, PreCheckoutQuery):
            await event.answer(ok=False, error_message="—")
        return None


class UserRateLimitMiddleware(BaseMiddleware):
    """
    Не більше USER_REQUESTS_PER_MINUTE апдейтів (повідомлення, кнопки — будь-що) на користувача за хвилину.
    Понад ліміт апдейт відкидається; користувач отримує ОДНЕ повідомлення на вікно (не на кожен запит).
    Оплату (pre_checkout / successful_payment) і адміна не обмежуємо: Telegram чекає відповіді на оплату ≤10 с.
    """

    def __init__(self) -> None:
        self._warned: dict[int, float] = {}

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        from aiogram.types import Message

        from botkit import ratelimit
        settings = ctx().settings

        user = data.get("event_from_user")
        if (user is None or user.id == settings.ADMIN_TELEGRAM_ID or isinstance(event, PreCheckoutQuery)
                or (isinstance(event, Message) and event.successful_payment)):
            return await handler(event, data)
        try:
            ratelimit.hit(f"tg:{user.id}", settings.USER_REQUESTS_PER_MINUTE, 60)
        except ratelimit.RateLimited as e:
            logger.info("user_rate_limited", telegram_id=user.id)
            now = time.monotonic()
            if now - self._warned.get(user.id, 0) >= 60:
                self._warned[user.id] = now
                await _tell_slow_down(event, e.retry_after)
            elif isinstance(event, CallbackQuery):
                await event.answer()
            return None
        return await handler(event, data)


async def _tell_slow_down(event: TelegramObject, seconds: int) -> None:
    from aiogram.types import Message

    c = ctx()
    lang = c.resolve_lang(getattr(getattr(event, "from_user", None), "language_code", None)) if c.resolve_lang else "uk"
    text = c.t(lang, "rate_limited", seconds=seconds) if c.t else f"⏳ {seconds} s"
    try:
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)
    except Exception:  # noqa: BLE001 — відповісти не вдалось, апдейт і так відкинуто
        pass
