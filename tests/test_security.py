"""Безпека (аудит 2026-10-12): токен бота в логах, медіа з фідбеку в адмінці."""
from __future__ import annotations

import logging
import sys

import httpx
import pytest

from app.admin import telegram as tg
from app.logs import RedactingFormatter, redact, setup_logging

TOKEN = "123456789:AAH-abcdefghijklmnopqrstuvwxyz_0123"


# ─── S3: токен бота не потрапляє в логи ──────────────────────────

def test_redact_masks_bot_token_in_urls():
    line = f'HTTP Request: POST https://api.telegram.org/bot{TOKEN}/sendMessage "HTTP/1.1 200 OK"'
    assert TOKEN not in redact(line) and "bot<BOT_TOKEN>/sendMessage" in redact(line)


def test_formatter_masks_token_in_tracebacks():
    try:
        raise RuntimeError(f"failed for url https://api.telegram.org/file/bot{TOKEN}/x.jpg")
    except RuntimeError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, exc_info=sys.exc_info())
    out = RedactingFormatter().format(record)
    assert TOKEN not in out and "<BOT_TOKEN>" in out


def test_setup_logging_quiets_httpx():
    setup_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


async def test_download_error_does_not_leak_url(monkeypatch):
    async def boom(_path):
        req = httpx.Request("GET", f"https://api.telegram.org/file/bot{TOKEN}/documents/x.pdf")
        raise httpx.HTTPStatusError("500", request=req, response=httpx.Response(500, request=req))
    monkeypatch.setattr(tg, "_download", boom)
    with pytest.raises(tg.TelegramFileError) as e:
        await tg.download_file("documents/x.pdf")
    assert TOKEN not in str(e.value) and e.value.__cause__ is None


# ─── S2: SVG і інше не показуються inline в адмінці ──────────────

@pytest.mark.parametrize("path", ["documents/file_1.svg", "documents/file_2.html", "documents/file_3.xml"])
def test_active_content_is_attachment(path):
    h = tg.media_headers(tg.guess_content_type(path), path.rsplit("/", 1)[-1])
    assert h["Content-Disposition"].startswith("attachment;")
    assert h["X-Content-Type-Options"] == "nosniff" and "sandbox" in h["Content-Security-Policy"]


def test_raster_images_inline():
    h = tg.media_headers(tg.guess_content_type("photos/file_7.jpg"), "file_7.jpg")
    assert h["Content-Disposition"] == "inline"


def test_filename_header_injection_stripped():
    h = tg.media_headers("application/pdf", 'a"b\r\nSet-Cookie: x.pdf')
    assert "\r" not in h["Content-Disposition"] and 'filename="abSet-Cookie: x.pdf"' in h["Content-Disposition"]


# ─── SSRF: посилання користувача не ведуть у внутрішню мережу ────

from app.services.scrapers import netguard  # noqa: E402


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8088/", "http://localhost/", "http://10.0.0.5/x", "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/",
    "http://printer.local/", "file:///etc/passwd", "ftp://shop.example/x", "http://100.64.0.1/",
])
async def test_internal_targets_rejected(url):
    with pytest.raises(netguard.ForbiddenTarget):
        await netguard.check_url(url)


async def test_domain_resolving_to_private_ip_rejected(monkeypatch):
    async def resolve(host):
        return ["93.184.216.34", "10.1.2.3"] if host == "evil.example" else ["93.184.216.34"]
    monkeypatch.setattr(netguard, "_resolve", resolve)
    with pytest.raises(netguard.ForbiddenTarget):
        await netguard.check_url("https://evil.example/item")
    await netguard.check_url("https://rozetka.com.ua/item")


async def test_redirect_to_internal_address_blocked():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "shop.example":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8088/admins"})
        return httpx.Response(200, text="secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True,
                                 event_hooks={"request": [netguard.httpx_request_hook]}) as c:
        with pytest.raises(netguard.ForbiddenTarget):
            await c.get("https://shop.example/item")


async def test_scrape_returns_forbidden_without_fetching(monkeypatch):
    from app.services.scrapers import scraper as sc
    called = []
    monkeypatch.setattr(sc, "get_http_client", lambda: called.append(1))
    result = await sc.GenericScraper("http://127.0.0.1:8088/").scrape("http://127.0.0.1:8088/")
    assert isinstance(result, sc.ScrapeError) and result.reason == "forbidden_target" and not called


async def test_browser_requests_to_internal_hosts_aborted():
    from unittest.mock import AsyncMock, MagicMock

    from app.services.scrapers.render import BrowserRenderer
    route = MagicMock()
    route.request.resource_type = "fetch"
    route.request.url = "http://192.168.0.1/router"
    route.abort, route.continue_ = AsyncMock(), AsyncMock()
    await BrowserRenderer._route(route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()


# ─── S10: заблоковані користувачі не доходять до хендлерів ───────

async def test_blocked_user_middleware(db):
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import CallbackQuery

    from app.bot.middlewares import BlockedUserMiddleware
    from app.database.models import User as DbUser
    from app.database.session import get_session
    from tests.helpers import make_callback, tg_user

    async with get_session() as s:
        s.add(DbUser(telegram_id=11, is_blocked=True))
        s.add(DbUser(telegram_id=12))
    mw, handler = BlockedUserMiddleware(), AsyncMock(return_value="handled")

    assert await mw(handler, MagicMock(), {"event_from_user": tg_user(12)}) == "handled"
    assert await mw(handler, MagicMock(), {"event_from_user": tg_user(99)}) == "handled"      # ще не в базі
    cb = make_callback(11, "list")
    answered = AsyncMock()
    object.__setattr__(cb, "answer", answered)
    assert isinstance(cb, CallbackQuery)
    assert await mw(handler, cb, {"event_from_user": tg_user(11)}) is None
    assert handler.await_count == 2 and answered.await_count == 1


# ─── N5: ≤ 20 запитів на хвилину на користувача ──────────────────

async def test_bot_user_rate_limit_warns_once_and_drops(db):
    from unittest.mock import AsyncMock

    from app.bot.middlewares import UserRateLimitMiddleware
    from tests.helpers import make_message, tg_user

    mw, handler = UserRateLimitMiddleware(), AsyncMock(return_value="ok")
    msgs = [make_message(77, f"text {i}") for i in range(23)]
    answers = []
    for m in msgs:
        object.__setattr__(m, "answer", AsyncMock(side_effect=lambda text, **kw: answers.append(text)))
        await mw(handler, m, {"event_from_user": tg_user(77)})
    assert handler.await_count == 20                                   # 21-ше і далі — відкинуто
    assert len(answers) == 1 and "Забагато запитів" in answers[0]       # попередження — одне на вікно
    other = make_message(78, "hi")
    assert await mw(handler, other, {"event_from_user": tg_user(78)}) == "ok"   # інших не зачіпає
