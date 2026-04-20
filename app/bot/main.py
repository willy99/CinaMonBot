"""
Bot entrypoint.

Використовуємо webhook замість polling:
- Webhook: Telegram надсилає нам POST запит — ефективніше, менше навантаження
- Polling: ми постійно питаємо Telegram "є нові повідомлення?" — зайве навантаження

В development можна запускати з polling (--polling прапор).
"""
import asyncio
import logging
import sys

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.config import settings
from app.bot.handlers.main import router
from app.database.session import close_engine

# ─── Logging ─────────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer() if settings.is_production
        else structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = structlog.get_logger(__name__)


# ─── Bot & Dispatcher ─────────────────────────────────────────────────────────

def create_bot() -> Bot:
    return Bot(
        token=settings.BOT_TOKEN.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    """
    RedisStorage для FSM — стан зберігається в Redis.
    Це дозволяє масштабувати бот на кілька інстансів без втрати стану.
    """
    storage = RedisStorage.from_url(settings.REDIS_URL)
    dp = Dispatcher(storage=storage)
    dp.include_router(router)
    return dp


# ─── Startup / Shutdown ──────────────────────────────────────────────────────

async def on_startup(bot: Bot) -> None:
    logger.info("bot_starting", environment=settings.ENVIRONMENT)

    # Ініціалізуємо БД
    from app.database.models import Base
    from app.database.session import engine
    async with engine.begin() as conn:
        # В production міграції через Alembic, тут тільки для dev
        if not settings.is_production:
            await conn.run_sync(Base.metadata.create_all)

    if settings.WEBHOOK_URL:
        await bot.set_webhook(
            url=f"{settings.WEBHOOK_URL}/webhook",
            secret_token=settings.WEBHOOK_SECRET.get_secret_value(),
            drop_pending_updates=True,
        )
        logger.info("webhook_set", url=settings.WEBHOOK_URL)
    else:
        logger.info("running_in_polling_mode")

    if settings.SENTRY_DSN:
        import sentry_sdk
        sentry_sdk.init(dsn=settings.SENTRY_DSN, environment=settings.ENVIRONMENT)


async def on_shutdown(bot: Bot) -> None:
    logger.info("bot_stopping")
    await bot.delete_webhook()
    await close_engine()


# ─── Webhook mode (production) ────────────────────────────────────────────────

def run_webhook() -> None:
    bot = create_bot()
    dp = create_dispatcher()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.WEBHOOK_SECRET.get_secret_value(),
    )
    handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    web.run_app(app, host="0.0.0.0", port=8080)


# ─── Polling mode (development) ──────────────────────────────────────────────

async def run_polling() -> None:
    bot = create_bot()
    dp = create_dispatcher()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logger.info("starting_polling")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


# ─── Entrypoint ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "webhook"

    if mode == "polling":
        asyncio.run(run_polling())
    else:
        run_webhook()
