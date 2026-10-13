"""
Адмін-дашборд ЦінаБот (NiceGUI). Окремий процес поруч із ботом, спільна база.

Запуск:
    python -m app.admin.cli create-admin pavel --telegram-id 431742835   # один раз
    python admin.py                                                     # http://127.0.0.1:8088

Безпека:
- логін + пароль (scrypt), другий фактор — код у Telegram через бота;
- усі сторінки, крім /login, закриті middleware;
- слухає 127.0.0.1 за замовчуванням: ззовні заходити через Tailscale/SSH-тунель,
  або поставити ADMIN_HOST=0.0.0.0 за reverse proxy з HTTPS.
"""
from __future__ import annotations

import secrets

import structlog
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from nicegui import app, ui
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from botkit.logs import setup_logging
from app.database.migrate import ensure_schema
from app.database.session import close_engine

setup_logging()
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="%H:%M:%S"),
        structlog.dev.ConsoleRenderer() if not settings.is_production else structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
)
logger = structlog.get_logger(__name__)

PUBLIC_PATHS = {"/login"}


class AuthMiddleware(BaseHTTPMiddleware):
    """Усе, крім /login і службових шляхів NiceGUI, вимагає активної сесії адміна."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/_nicegui") or path in PUBLIC_PATHS:
            return await call_next(request)
        from botkit.admin.auth import check_session
        if not await check_session():
            if request.method == "GET" and not path.startswith("/api"):
                app.storage.user["referrer_path"] = path
            return RedirectResponse("/login")
        return await call_next(request)


app.add_middleware(AuthMiddleware)


@app.on_startup
async def _startup() -> None:
    await ensure_schema()
    from botkit.admin import queries as aq
    promoted = await aq.ensure_owner()
    if promoted:
        logger.warning("admin_owner_assigned", username=promoted)
    if await aq.admins_count_active() == 0:
        logger.warning("no_admins", hint="python -m app.admin.cli create-admin <login> --telegram-id <id>")
    logger.info("admin_ready", host=settings.ADMIN_HOST, port=settings.ADMIN_PORT, twofa=settings.ADMIN_2FA_ENABLED)


@app.on_shutdown
async def _shutdown() -> None:
    await close_engine()


# ─── Медіа з фідбеку: проксі до файлів Telegram ──────────────────
# Браузер не має бачити BOT_TOKEN, тому байти тягне сервер. Шлях не в PUBLIC_PATHS —
# AuthMiddleware вимагає сесію адміна.

@app.get("/media/{file_id}")
async def media_file(file_id: str, name: str | None = None) -> Response:
    from botkit.admin import telegram as tg

    try:
        file_path = await tg.get_file_path(file_id)
        if not file_path:
            raise HTTPException(status_code=404, detail="Файл невідомий або недоступний")
        data, ctype = await tg.download_file(file_path)
    except tg.TelegramFileError as e:
        raise HTTPException(status_code=413 if "large" in str(e) else 404, detail=str(e))

    # inline — лише растрові картинки; SVG/HTML з фідбеку виконали б скрипт на origin адмінки
    filename = name or file_path.rsplit("/", 1)[-1]
    return Response(content=data, media_type=ctype, headers=tg.media_headers(ctype, filename))


# Реєструємо сторінки (importlib, щоб не перекрити `app` з NiceGUI нашим пакетом app/)
import importlib  # noqa: E402
importlib.import_module("app.admin.pages")

storage_secret = settings.ADMIN_STORAGE_SECRET or secrets.token_hex(32)
if not settings.ADMIN_STORAGE_SECRET:
    logger.warning("admin_storage_secret_missing", hint="ADMIN_STORAGE_SECRET у .env — інакше сесії злітають після рестарту")

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host=settings.ADMIN_HOST,
        port=settings.ADMIN_PORT,
        title="ЦінаБот Admin",
        dark=True,
        reload=False,
        show=False,
        favicon="🛡️",
        storage_secret=storage_secret,
        # Своя назва cookie: cookie прив'язані до хоста, не порту — обидві адмінки на 127.0.0.1 інакше
        # перезаписували б одна одній cookie «session» і викидали з сесії
        session_middleware_kwargs={"session_cookie": "cinabot_admin_session", "same_site": "strict"},
        show_welcome_message=False,
    )
