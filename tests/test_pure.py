"""Модулі без БД: логи, ліміт частоти, запобіжник, SSRF-захист, заголовки медіа."""
from __future__ import annotations

import logging
import sys

import httpx
import pytest

from botkit import netguard, ratelimit
from botkit.admin import telegram as tg
from botkit.breaker import Breaker
from botkit.logs import RedactingFormatter, redact, setup_logging

TOKEN = "123456789:AAH-abcdefghijklmnopqrstuvwxyz_0123"


def test_redact_and_formatter():
    assert TOKEN not in redact(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage")
    try:
        raise RuntimeError(f"url https://api.telegram.org/file/bot{TOKEN}/x")
    except RuntimeError:
        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, exc_info=sys.exc_info())
    assert TOKEN not in RedactingFormatter().format(rec)
    setup_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_ratelimit_sliding_window():
    ratelimit.reset()
    for i in range(3):
        ratelimit.hit("k", 3, 60, now=100.0 + i)
    with pytest.raises(ratelimit.RateLimited) as e:
        ratelimit.hit("k", 3, 60, now=110.0)
    assert e.value.retry_after == 51
    ratelimit.hit("k", 3, 60, now=160.0)


def test_breaker_backoff_success_snapshot():
    b = Breaker(base_seconds=60, max_seconds=300)
    assert [b.trip("x", "net", now=0.0)[1] for _ in range(5)] == [60, 120, 240, 300, 300]
    assert b.is_open("x", now=10.0) and b.snapshot(now=0.0)["x"]["failures"] == 5
    assert b.trip("y", "captcha", now=0.0, base_seconds=500) == (True, 300)
    assert b.success("x") and not b.success("x") and not b.is_open("x")


def test_breaker_restore_continues_series():
    """Після рестарту процесу серія триває: trip() не «перший» (без повторного алерту), пауза росте далі."""
    b = Breaker(base_seconds=60, max_seconds=1800)
    b.restore("uz", failures=5, paused_s=200, reason="ProviderUnavailable", now=0.0)
    assert b.is_open("uz", now=100.0) and not b.is_open("uz", now=201.0)
    assert b.snapshot(now=0.0)["uz"] == {"paused_s": 200, "failures": 5, "reason": "ProviderUnavailable"}
    assert b.trip("uz", "ProviderUnavailable", now=300.0) == (False, 1800)
    b.restore("x", failures=0, paused_s=100)                         # порожній запис ігноруємо
    assert "x" not in b.snapshot()


@pytest.mark.parametrize("url", ["http://127.0.0.1:8088/", "http://localhost/", "http://10.0.0.5/", "http://[::1]/",
                                 "http://169.254.169.254/", "http://[::ffff:127.0.0.1]/", "file:///etc/passwd",
                                 "http://printer.local/", "http://100.64.0.1/"])
async def test_netguard_rejects_internal(url):
    netguard.clear_cache()
    with pytest.raises(netguard.ForbiddenTarget):
        await netguard.check_url(url)


async def test_netguard_dns_and_redirects(monkeypatch):
    async def resolve(host):
        return ["93.184.216.34", "10.1.2.3"] if host == "evil.example" else ["93.184.216.34"]
    monkeypatch.setattr(netguard, "_resolve", resolve)
    netguard.clear_cache()
    with pytest.raises(netguard.ForbiddenTarget):
        await netguard.check_url("https://evil.example/x")
    await netguard.check_url("https://shop.example/x")

    def handler(req):
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"}) if req.url.host == "shop.example" \
            else httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True,
                                 event_hooks={"request": [netguard.httpx_request_hook]}) as c:
        with pytest.raises(netguard.ForbiddenTarget):
            await c.get("https://shop.example/x")


@pytest.mark.parametrize("path,inline", [("photos/f.jpg", True), ("documents/f.svg", False),
                                         ("documents/f.html", False)])
def test_media_headers(path, inline):
    h = tg.media_headers(tg.guess_content_type(path), path.rsplit("/", 1)[-1])
    assert (h["Content-Disposition"] == "inline") is inline
    assert h["X-Content-Type-Options"] == "nosniff" and "sandbox" in h["Content-Security-Policy"]
    bad = tg.media_headers("application/pdf", 'a"b\r\nSet-Cookie: x.pdf')
    assert "\r" not in bad["Content-Disposition"] and "\n" not in bad["Content-Disposition"]


async def test_media_download_error_hides_token(kit, monkeypatch):
    async def boom(_path):
        req = httpx.Request("GET", f"https://api.telegram.org/file/bot{TOKEN}/x.pdf")
        raise httpx.HTTPStatusError("500", request=req, response=httpx.Response(500, request=req))
    monkeypatch.setattr(tg, "_download", boom)
    with pytest.raises(tg.TelegramFileError) as e:
        await tg.download_file("documents/x.pdf")
    assert TOKEN not in str(e.value)
