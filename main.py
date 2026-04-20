"""
Точка входу. Один процес — бот + планувальник.

Запуск:
    python main.py

Або якщо є .env файл з різними налаштуваннями:
    cp .env.example .env
    # заповни BOT_TOKEN і DATABASE_URL
    python main.py
"""
import asyncio
import logging
import sys

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import settings
from app.database.session import init_db, close_engine
from app.bot.handlers.main import router
from app.scheduler.price_checker import setup_scheduler, scheduler

# ─── Logging setup ───────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="%H:%M:%S"),
        structlog.dev.ConsoleRenderer() if not settings.is_production
        else structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
)
logger = structlog.get_logger(__name__)


# ─── Lifecycle ───────────────────────────────────────────────────

async def on_startup(bot: Bot) -> None:
    logger.info("starting", db=settings.DATABASE_URL.split("///")[-1])

    # Ініціалізуємо БД (створює таблиці якщо не існують)
    await init_db()
    logger.info("database_ready")

    # Запускаємо планувальник перевірки цін
    setup_scheduler(bot)
    logger.info("scheduler_ready")

    # Видаляємо вебхук якщо був встановлений раніше
    await bot.delete_webhook(drop_pending_updates=True)

    me = await bot.get_me()
    logger.info("bot_ready", username=me.username)


async def on_shutdown(bot: Bot) -> None:
    logger.info("shutting_down")
    scheduler.shutdown(wait=False)
    await close_engine()


# ─── Main ────────────────────────────────────────────────────────

async def main() -> None:
    bot = Bot(
        token=settings.BOT_TOKEN.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # MemoryStorage — простіше ніж Redis, достатньо для старту
    # Мінус: стан FSM губиться при рестарті (юзер знову починає /add)
    # Це не критично — просто попросить посилання ще раз
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logger.info("starting_polling")
    await dp.start_polling(
        bot,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("stopped_by_user")
