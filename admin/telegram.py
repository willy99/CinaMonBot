"""
Доступ до файлів Telegram (фото/документи з фідбеку) для дашборду.

Браузер ніколи не бачить BOT_TOKEN: дашборд сам ходить у Bot API (getFile)
і віддає байти через /media/<file_id>. Токен не логується.
"""
from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx
import structlog

from botkit.context import ctx

logger = structlog.get_logger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024  # ліміт Bot API для getFile — 20 МБ

# Лише растрові картинки показуємо прямо в браузері. SVG (і будь-що інше) — тільки завантаженням:
# SVG може містити <script>, який виконався б на origin адмінки з сесією адміна.
INLINE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
# Файли від користувачів: заборонити sniffing, скрипти, фрейми й будь-які підзавантаження
MEDIA_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
    "Cache-Control": "private, max-age=3600",
}


class TelegramFileError(Exception):
    """Файл невідомий/недоступний або завеликий."""


def _api_base() -> str:
    return f"https://api.telegram.org/bot{ctx().settings.BOT_TOKEN.get_secret_value()}"


def _file_base() -> str:
    return f"https://api.telegram.org/file/bot{ctx().settings.BOT_TOKEN.get_secret_value()}"


async def get_file_path(file_id: str) -> str | None:
    """Bot API getFile → file_path на серверах Telegram (None, якщо файл невідомий)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{_api_base()}/getFile", params={"file_id": file_id})
    except httpx.HTTPError as e:
        logger.warning("telegram_getfile_error", error=type(e).__name__)
        return None
    if r.status_code != 200:
        logger.info("telegram_getfile_failed", status=r.status_code)
        return None
    data = r.json()
    if not data.get("ok"):
        return None
    result = data.get("result") or {}
    size = result.get("file_size") or 0
    if size and size > MAX_FILE_BYTES:
        raise TelegramFileError("file too large")
    return result.get("file_path") or None


def guess_content_type(file_path: str, fallback: str = "application/octet-stream") -> str:
    ctype, _ = mimetypes.guess_type(PurePosixPath(file_path).name)
    if not ctype:
        # Telegram кладе фото у photos/file_N.jpg; якщо розширення немає — вважаємо октети
        return "image/jpeg" if file_path.startswith("photos/") else fallback
    return ctype


def media_headers(ctype: str, filename: str) -> dict[str, str]:
    """Заголовки відповіді /media: inline лише для безпечних картинок, решта — attachment."""
    filename = filename.replace("\r", "").replace("\n", "") or "file"
    if ctype in INLINE_TYPES:
        disposition = "inline"
    else:
        ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "")
        disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return {"Content-Disposition": disposition, **MEDIA_SECURITY_HEADERS}


async def download_file(file_path: str) -> tuple[bytes, str]:
    """Повертає (байти, content-type). Кидає TelegramFileError при 404 / перевищенні ліміту."""
    try:
        data = await _download(file_path)
    except httpx.HTTPError as e:
        # текст винятку містить URL з токеном — назовні лише тип помилки
        logger.warning("telegram_download_error", error=type(e).__name__)
        raise TelegramFileError("file not found") from None
    return data, guess_content_type(file_path)


async def _download(file_path: str) -> bytes:
    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream("GET", f"{_file_base()}/{file_path}") as r:
            if r.status_code == 404:
                raise TelegramFileError("file not found")
            r.raise_for_status()
            declared = int(r.headers.get("content-length") or 0)
            if declared > MAX_FILE_BYTES:
                raise TelegramFileError("file too large")
            chunks: list[bytes] = []
            total = 0
            async for chunk in r.aiter_bytes():
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise TelegramFileError("file too large")
                chunks.append(chunk)
    return b"".join(chunks)
