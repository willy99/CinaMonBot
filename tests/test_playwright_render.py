"""
Рендеринг через headless Chromium (app/services/scrapers/render.py) та його інтеграція в scraper.py.

Усі тести офлайн і без браузера: BrowserRenderer.render підмінюється. Єдиний виняток —
test_real_chromium_renders_local_page (RUN_BROWSER_TESTS=1), який запускає справжній Chromium
на локальному HTTP-сервері.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import respx

from app.services.scrapers import registry
from app.services.scrapers import render as render_mod
from app.services.scrapers import scraper as scraper_mod
from app.services.scrapers.extract import extract_product
from app.services.scrapers.registry import Platform
from app.services.scrapers.render import (
    BrowserRenderer, RenderError, RenderResult, RenderUnavailable, locale_for, setting,
)
from app.services.scrapers.scraper import GenericScraper, ProductData, ScrapeError

PRODUCT_LD = json.dumps({
    "@context": "https://schema.org", "@type": "Product", "name": "Rendered product",
    "offers": {"@type": "Offer", "price": "2499.00", "priceCurrency": "UAH", "availability": "https://schema.org/InStock"},
})
PRODUCT_HTML = f'<html><head><title>Shop</title><script type="application/ld+json">{PRODUCT_LD}</script></head><body><h1>x</h1></body></html>'
UNPRICED_HTML = ('<html><head><title>Shop</title><script type="application/ld+json">'
                 + json.dumps({"@context": "https://schema.org", "@type": "Product", "name": "SSR without price"})
                 + '</script></head><body></body></html>')
BOT_WALL_HTML = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
NOTHING_HTML = "<html><head><title>Hi</title></head><body><p>No product here</p></body></html>"
HTML = {"content-type": "text/html; charset=utf-8"}

FLAGGED = "flagged.example"
PLAIN = "plain.example"
PLATFORMS = [
    Platform(FLAGGED, "Flagged", "UA", "verified", "json-ld", render_js=True),
    Platform(PLAIN, "Plain", "UA", "verified", "json-ld"),
]


def html_response(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=text, headers=HTML)


def rendered(html: str = PRODUCT_HTML, url: str = "", status: int | None = 200, **kw) -> RenderResult:
    return RenderResult(html=html, final_url=url or f"https://{FLAGGED}/p", status=status, elapsed=1.5, **kw)


@pytest.fixture
def fast(monkeypatch):
    """Без пауз/rate limiter/healer; record_scrape — заглушка з журналом; реєстр — два тестові домени."""
    calls: list[dict] = []

    async def no_sleep(_s):
        return None

    async def no_wait(_d):
        return None

    async def fake_record(domain, ok, *, strategy=None, reason=None, healed=False, duration_ms=0):
        calls.append({"domain": domain, "ok": ok, "strategy": strategy, "reason": reason, "healed": healed})

    async def no_heal(domain, html):
        raise AssertionError("Claude healer must not be called")

    monkeypatch.setattr(scraper_mod, "_sleep", no_sleep)
    monkeypatch.setattr(scraper_mod._rate_limiter, "acquire", no_wait)
    monkeypatch.setattr(scraper_mod, "record_scrape", fake_record)
    monkeypatch.setattr(scraper_mod._auto_healer, "find_selectors", no_heal)
    monkeypatch.setattr(scraper_mod.settings, "SCRAPE_MAX_RETRIES", 2)
    monkeypatch.setattr(registry, "PLATFORMS", PLATFORMS)
    monkeypatch.setenv("RENDER_FALLBACK", "1")
    monkeypatch.setattr(render_mod, "_renderer", None)
    return calls


@pytest.fixture
def fake_render(monkeypatch):
    """Підміна BrowserRenderer.render: повертає заготовлений результат або кидає виключення; рахує виклики."""
    state: dict = {"calls": [], "result": rendered(), "error": None}

    async def _render(self, url, *, timeout=None):
        state["calls"].append(url)
        if state["error"] is not None:
            raise state["error"]
        return state["result"]

    monkeypatch.setattr(BrowserRenderer, "render", _render)
    return state


def platform_url(domain: str, path: str = "/p") -> str:
    return f"https://{domain}{path}"


# ─── Інтеграція в scraper.py ─────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_flagged_domain_uses_renderer_not_httpx(fast, fake_render):
    url = platform_url(FLAGGED)
    route = respx.get(url).mock(return_value=html_response(NOTHING_HTML))
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ProductData)
    assert result.title == "Rendered product" and result.price == Decimal("2499.00")
    assert result.strategy == "json-ld+render"
    assert fake_render["calls"] == [url]
    assert route.call_count == 0
    assert fast == [{"domain": FLAGGED, "ok": True, "strategy": "json-ld+render", "reason": None, "healed": False}]


@pytest.mark.asyncio
@respx.mock
async def test_unflagged_domain_uses_httpx(fast, fake_render):
    url = platform_url(PLAIN)
    route = respx.get(url).mock(return_value=html_response(PRODUCT_HTML))
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ProductData) and result.strategy == "json-ld"
    assert route.call_count == 1 and fake_render["calls"] == []
    assert len(fast) == 1 and fast[0]["strategy"] == "json-ld"


@pytest.mark.asyncio
@respx.mock
async def test_unknown_domain_without_render_flag_uses_httpx(fast, fake_render):
    url = "https://unknown-shop.example/p"
    respx.get(url).mock(return_value=html_response(PRODUCT_HTML))
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ProductData) and result.strategy == "json-ld"
    assert fake_render["calls"] == []


@pytest.mark.asyncio
@respx.mock
async def test_fallback_on_bot_wall_when_enabled(fast, fake_render):
    url = platform_url(PLAIN)
    respx.get(url).mock(return_value=html_response(BOT_WALL_HTML))
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ProductData) and result.strategy == "json-ld+render"
    assert fake_render["calls"] == [url]
    assert len(fast) == 1 and fast[0]["ok"] is True and fast[0]["strategy"] == "json-ld+render"


@pytest.mark.asyncio
@respx.mock
async def test_fallback_on_403_after_retries(fast, fake_render):
    url = platform_url(PLAIN)
    route = respx.get(url).mock(return_value=httpx.Response(403, text="forbidden"))
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ProductData) and result.strategy == "json-ld+render"
    assert route.call_count == 2            # httpx-повтори відбулись до фолбеку
    assert fake_render["calls"] == [url]    # рендер — рівно один


@pytest.mark.asyncio
@respx.mock
async def test_fallback_on_parse_failed(fast, fake_render):
    url = platform_url(PLAIN)
    respx.get(url).mock(return_value=html_response(NOTHING_HTML))
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ProductData) and result.strategy == "json-ld+render"
    assert fake_render["calls"] == [url]


@pytest.mark.asyncio
@respx.mock
async def test_fallback_when_httpx_product_has_no_price(fast, fake_render):
    """needs_js: SSR віддає Product без ціни — пробуємо рендер; з ціною він перемагає."""
    url = platform_url(PLAIN)
    respx.get(url).mock(return_value=html_response(UNPRICED_HTML))
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ProductData) and result.price == Decimal("2499.00")
    assert result.strategy == "json-ld+render"


@pytest.mark.asyncio
@respx.mock
async def test_unpriced_http_result_kept_when_render_also_unpriced(fast, fake_render):
    url = platform_url(PLAIN)
    respx.get(url).mock(return_value=html_response(UNPRICED_HTML))
    fake_render["result"] = rendered(UNPRICED_HTML, url)
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ProductData) and result.price is None
    assert result.strategy == "json-ld"      # httpx-результат без суфікса
    assert fake_render["calls"] == [url]


@pytest.mark.asyncio
@respx.mock
async def test_no_fallback_when_disabled(fast, fake_render, monkeypatch):
    # RENDER_* тепер оголошені в Settings — вимикаємо через налаштування, а не env
    monkeypatch.setattr(render_mod.settings, "RENDER_FALLBACK", False)
    url = platform_url(PLAIN)
    respx.get(url).mock(return_value=html_response(BOT_WALL_HTML))
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ScrapeError) and result.reason == "blocked"
    assert fake_render["calls"] == []
    assert len(fast) == 1 and fast[0]["reason"] == "blocked"


@pytest.mark.asyncio
@respx.mock
async def test_no_fallback_for_not_found_or_network(fast, fake_render):
    url = platform_url(PLAIN, "/gone")
    respx.get(url).mock(return_value=httpx.Response(404))
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ScrapeError) and result.reason == "not_found"

    url2 = platform_url(PLAIN, "/net")
    respx.get(url2).mock(side_effect=httpx.ConnectTimeout("timeout"))
    result = await GenericScraper(url2).scrape(url2)
    assert isinstance(result, ScrapeError) and result.reason == "network"
    assert fake_render["calls"] == []


@pytest.mark.asyncio
@respx.mock
async def test_fallback_failure_keeps_http_reason(fast, fake_render):
    url = platform_url(PLAIN)
    respx.get(url).mock(return_value=html_response(BOT_WALL_HTML))
    fake_render["error"] = RenderError("render_timeout", "slow")
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ScrapeError) and result.reason == "blocked"
    assert len(fast) == 1 and fast[0]["reason"] == "blocked"


@pytest.mark.asyncio
@respx.mock
async def test_fallback_still_blocked_after_render(fast, fake_render):
    url = platform_url(PLAIN)
    respx.get(url).mock(return_value=html_response(BOT_WALL_HTML))
    fake_render["result"] = rendered(BOT_WALL_HTML, url, status=403)
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ScrapeError) and result.reason == "blocked"
    assert len(fast) == 1


@pytest.mark.parametrize("error,reason", [
    (RenderError("render_failed", "crash"), "render_failed"),
    (RenderError("render_timeout", "slow"), "render_timeout"),
    (RenderError("blocked", "challenge"), "blocked"),
    (RuntimeError("unexpected"), "render_failed"),
])
@pytest.mark.asyncio
@respx.mock
async def test_renderer_errors_become_scrape_errors_logged_once(fast, fake_render, error, reason):
    url = platform_url(FLAGGED)
    fake_render["error"] = error
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ScrapeError) and result.reason == reason
    assert fast == [{"domain": FLAGGED, "ok": False, "strategy": None, "reason": reason, "healed": False}]
    assert len(fake_render["calls"]) == 1   # без повторів


@pytest.mark.asyncio
@respx.mock
async def test_rendered_bot_wall_404_and_403_without_data(fast, fake_render):
    url = platform_url(FLAGGED)
    fake_render["result"] = rendered(BOT_WALL_HTML, url, status=200)
    assert (await GenericScraper(url).scrape(url)).reason == "blocked"

    fake_render["result"] = rendered(NOTHING_HTML, url, status=404)
    assert (await GenericScraper(url).scrape(url)).reason == "not_found"

    fake_render["result"] = rendered(NOTHING_HTML, url, status=403)
    assert (await GenericScraper(url).scrape(url)).reason == "blocked"

    fake_render["result"] = rendered(NOTHING_HTML, url, status=200)
    assert (await GenericScraper(url).scrape(url)).reason == "parse_failed"
    assert [c["reason"] for c in fast] == ["blocked", "not_found", "blocked", "parse_failed"]


@pytest.mark.asyncio
@respx.mock
async def test_flagged_domain_degrades_to_httpx_when_playwright_missing(fast, fake_render):
    url = platform_url(FLAGGED)
    route = respx.get(url).mock(return_value=html_response(PRODUCT_HTML))
    fake_render["error"] = RenderUnavailable("no playwright")
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ProductData) and result.strategy == "json-ld"   # без «+render»
    assert route.call_count == 1
    assert len(fast) == 1 and fast[0]["ok"] is True and fast[0]["strategy"] == "json-ld"


@pytest.mark.asyncio
@respx.mock
async def test_fallback_silently_skipped_when_playwright_missing(fast, fake_render):
    url = platform_url(PLAIN)
    respx.get(url).mock(return_value=html_response(BOT_WALL_HTML))
    fake_render["error"] = RenderUnavailable("no playwright")
    result = await GenericScraper(url).scrape(url)
    assert isinstance(result, ScrapeError) and result.reason == "blocked"
    assert len(fast) == 1


@pytest.mark.asyncio
@respx.mock
async def test_rendering_counts_as_domain_request(fast, fake_render, monkeypatch):
    acquired: list[str] = []

    async def record_acquire(domain):
        acquired.append(domain)

    monkeypatch.setattr(scraper_mod._rate_limiter, "acquire", record_acquire)
    url = platform_url(PLAIN)
    respx.get(url).mock(return_value=html_response(BOT_WALL_HTML))
    await GenericScraper(url).scrape(url)
    assert acquired == [PLAIN, PLAIN]       # httpx + рендер


# ─── BrowserRenderer без браузера ────────────────────────────────

@pytest.mark.asyncio
async def test_renderer_unavailable_when_playwright_import_fails(monkeypatch):
    monkeypatch.setenv("RUN_BROWSER_TESTS", "1")                 # знімаємо pytest-запобіжник
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)  # import → ImportError
    monkeypatch.setattr(BrowserRenderer, "_unavailable_logged", False)
    r = BrowserRenderer()
    with pytest.raises(RenderUnavailable):
        await r.render("https://shop.example/p")
    # Повторний виклик — cooldown, браузер не запускається, знову RenderUnavailable
    with pytest.raises(RenderUnavailable):
        await r.render("https://shop.example/p")
    await r.close()     # безпечно без браузера


@pytest.mark.asyncio
async def test_renderer_refuses_to_launch_under_pytest_by_default(monkeypatch):
    monkeypatch.delenv("RUN_BROWSER_TESTS", raising=False)
    r = BrowserRenderer()
    with pytest.raises(RenderUnavailable):
        await r.render("https://shop.example/p")


@pytest.mark.asyncio
async def test_renderer_rejects_url_without_host():
    with pytest.raises(RenderError) as exc:
        await BrowserRenderer().render("not a url")
    assert exc.value.reason == "render_failed"


def test_locale_for():
    assert locale_for("comfy.ua")[0] == "uk-UA" and locale_for("comfy.ua")[1] == "Europe/Kyiv"
    assert locale_for("allegro.pl")[0] == "pl-PL"
    assert locale_for("otto.de")[0] == "de-DE"
    assert locale_for("argos.co.uk")[0] == "en-GB"
    assert locale_for("walmart.com")[0] == "en-US"


def test_setting_prefers_settings_then_env_then_defaults(monkeypatch):
    # 1) значення з app.config.Settings мають пріоритет
    assert setting("RENDER_MAX_CONCURRENCY") == render_mod.settings.RENDER_MAX_CONCURRENCY == 2
    monkeypatch.setattr(render_mod.settings, "RENDER_MAX_CONCURRENCY", 5)
    monkeypatch.setenv("RENDER_MAX_CONCURRENCY", "9")
    assert setting("RENDER_MAX_CONCURRENCY") == 5
    # 2) якщо поля в Settings немає (None) — env, а сміття в env → дефолт
    monkeypatch.setattr(render_mod.settings, "RENDER_MAX_CONCURRENCY", None)
    assert setting("RENDER_MAX_CONCURRENCY") == 9
    monkeypatch.setenv("RENDER_MAX_CONCURRENCY", "garbage")
    assert setting("RENDER_MAX_CONCURRENCY") == 2
    monkeypatch.setattr(render_mod.settings, "RENDER_TIMEOUT_SECONDS", None)
    monkeypatch.setenv("RENDER_TIMEOUT_SECONDS", "12.5")
    assert setting("RENDER_TIMEOUT_SECONDS") == 12.5
    # 3) булеве через Settings
    for value, expected in ((False, False), (True, True)):
        monkeypatch.setattr(render_mod.settings, "RENDER_FALLBACK", value)
        assert render_mod.render_fallback_enabled() is expected


def test_registry_render_flag_default_and_lookup():
    assert Platform("x.example", "X", "UA").render_js is False
    flagged = [p.domain for p in registry.PLATFORMS if p.render_js]
    for p in registry.PLATFORMS:
        if p.render_js:
            assert p.status == "verified" and p.strategy, f"{p.domain}: render_js без verified/strategy"
    assert all("." in d for d in flagged)


# ─── Реальні відрендерені сторінки: tests/fixtures/*.render.html ──

_FIX = Path(__file__).parent / "fixtures"


def _render_fixtures() -> list[Path]:
    return sorted(_FIX.glob("*.render.html")) if _FIX.is_dir() else []


@pytest.mark.parametrize("path", _render_fixtures() or [None], ids=lambda p: p.name if p else "no-render-fixtures")
def test_rendered_fixture_extracts_title_and_price(path: Path | None):
    if path is None:
        pytest.skip("немає tests/fixtures/*.render.html — запусти scripts/probe_render.py")
    sidecar = path.with_suffix(".json")
    meta = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
    assert meta.get("rendered") is True, f"{path.name}: sidecar без rendered: true"
    product = extract_product(path.read_text(encoding="utf-8", errors="replace"), meta.get("url") or f"https://{path.name.split('.render')[0]}/")
    assert product is not None and product.title
    assert product.price is not None and product.price > 0
    if meta.get("expected_strategy"):
        assert product.strategy == meta["expected_strategy"]
    if meta.get("expected_currency"):
        assert product.currency == meta["expected_currency"]
    # Домен фікстури має бути в реєстрі з render_js=True
    domain = path.name.split(".render.html")[0]
    p = registry.platform_for(domain)
    assert p is not None and p.render_js, f"{domain}: є фікстура рендеру, але в реєстрі render_js=False"


# ─── Справжній Chromium (RUN_BROWSER_TESTS=1) ────────────────────

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = PRODUCT_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # тихо
        return


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="RUN_BROWSER_TESTS=1 вмикає тест зі справжнім Chromium")
@pytest.mark.asyncio
async def test_real_chromium_renders_local_page():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    r = BrowserRenderer()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/product.html"
        result = await r.render(url, timeout=30)
        assert result.status == 200 and result.final_url == url
        assert "Rendered product" in result.html
        product = extract_product(result.html, url)
        assert product is not None and product.price == Decimal("2499.00") and product.strategy == "json-ld"
        assert r.pages_rendered == 1
    finally:
        await r.close()
        server.shutdown()
