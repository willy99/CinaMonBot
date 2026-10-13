"""
Тести GenericScraper, фабрики і мережевого шару AbstractScraper.scrape (мережа через respx).
"""
from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from pathlib import Path
import respx

from app.services.scrapers import scraper as scraper_mod
from app.services.scrapers.scraper import (
    GenericScraper,
    OLXScraper,
    ProductData,
    PromScraper,
    RozetkaScraper,
    ScrapeError,
    ScraperFactory,
)

PRODUCT_LD = json.dumps({
    "@context": "https://schema.org", "@type": "Product", "name": "Generic product",
    "offers": {"@type": "Offer", "price": "1299.00", "priceCurrency": "UAH",
               "availability": "https://schema.org/InStock"},
})
PRODUCT_HTML = f'<html><head><title>Shop</title><script type="application/ld+json">{PRODUCT_LD}</script></head><body><h1>x</h1></body></html>'
BOT_WALL_HTML = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
NOTHING_HTML = "<html><head><title>Hi</title></head><body><p>No product here</p></body></html>"
HTML = {"content-type": "text/html; charset=utf-8"}


def html_response(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=text, headers=HTML)


@pytest.fixture
def fast(monkeypatch):
    """Без пауз між повторами, без rate limiter, без Claude-healer; record_scrape — заглушка з журналом."""
    calls: list[dict] = []

    async def no_sleep(_seconds: float) -> None:
        return None

    async def no_wait(_domain: str) -> None:
        return None

    async def fake_record(domain, ok, *, strategy=None, reason=None, healed=False, duration_ms=0):
        calls.append({"domain": domain, "ok": ok, "strategy": strategy, "reason": reason,
                      "healed": healed, "duration_ms": duration_ms})

    async def no_heal(domain, html):
        raise AssertionError("Claude healer must not be called in these tests")

    monkeypatch.setattr(scraper_mod, "_sleep", no_sleep)
    monkeypatch.setattr(scraper_mod._rate_limiter, "acquire", no_wait)
    monkeypatch.setattr(scraper_mod, "record_scrape", fake_record)
    monkeypatch.setattr(scraper_mod._auto_healer, "find_selectors", no_heal)
    monkeypatch.setattr(scraper_mod.settings, "SCRAPE_MAX_RETRIES", 2)
    return calls


# ─── Фабрика ─────────────────────────────────────────────────────

@pytest.mark.parametrize("url,cls", [
    ("https://rozetka.com.ua/ua/apple-iphone/p12345/", RozetkaScraper),
    ("https://www.rozetka.com.ua/x/p1/", RozetkaScraper),
    ("https://m.olx.ua/d/uk/obyavlenie/x.html", OLXScraper),
    ("https://prom.ua/ua/p123-x.html", PromScraper),
    ("https://shop.prom.ua/p123-x.html", PromScraper),
    ("https://www.comfy.ua/ua/smartfon.html", GenericScraper),
    ("http://some-random-shop.de/produkt/1?x=1", GenericScraper),
    ("https://notrozetka.com.ua/x", GenericScraper),        # не піддомен rozetka
])
def test_factory_routing(url, cls):
    scraper = ScraperFactory.get(url)
    assert isinstance(scraper, cls)


@pytest.mark.parametrize("url", ["not a url", "", "rozetka.com.ua/x", "ftp://shop.ua/x", "https://", "mailto:a@b.c", "javascript:alert(1)"])
def test_factory_rejects_non_urls(url):
    assert ScraperFactory.get(url) is None


def test_factory_is_specific_and_supported_domains():
    assert ScraperFactory.is_specific("https://rozetka.com.ua/x") is True
    assert ScraperFactory.is_specific("https://comfy.ua/x") is False
    assert ScraperFactory.is_specific("nope") is False
    assert ScraperFactory.supported_domains() == ["rozetka.com.ua", "olx.ua", "prom.ua", "notino.ua"]
    assert ScraperFactory.is_specific("https://www.notino.ua/x/") is True


def test_generic_domain_from_host():
    assert ScraperFactory.get("https://WWW.Comfy.UA/x").domain == "comfy.ua"
    assert ScraperFactory.get("https://shop.example.com:8443/x").domain == "shop.example.com"


# ─── Канонізація ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("HTTPS://WWW.Shop.UA/p/1?variant=5&utm_source=tg&utm_medium=x&gclid=1&fbclid=2#top",
     "https://www.shop.ua/p/1?variant=5"),
    ("https://shop.ua/p/1?sku=ABC&ref=friend&_ga=1.2&mc_cid=a&mc_eid=b&srsltid=c&igshid=d&ttclid=e&msclkid=f&dclid=g&yclid=h&sc_campaign=i",
     "https://shop.ua/p/1?sku=ABC"),
    ("https://shop.ua/p/1?utm_source=a", "https://shop.ua/p/1"),
    ("https://shop.ua/p/1?a=1&b=2", "https://shop.ua/p/1?a=1&b=2"),
    ("https://shop.ua", "https://shop.ua/"),
    ("https://shop.ua/p/1?q=%D1%82%D0%B5%D1%81%D1%82", "https://shop.ua/p/1?q=%D1%82%D0%B5%D1%81%D1%82"),
])
def test_generic_canonicalize(raw, expected):
    assert GenericScraper(raw).canonicalize_url(raw) == expected


def test_specific_scrapers_keep_their_canonicalization():
    assert RozetkaScraper().canonicalize_url("https://rozetka.com.ua/ua/p1/?utm=1#x") == "https://rozetka.com.ua/ua/p1/"


# ─── scrape(): мережа через respx ────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_generic_scrape_success_logs_once(fast):
    url = "https://shop.example.ua/product/1?utm_source=x"
    respx.get("https://shop.example.ua/product/1").mock(
        return_value=html_response(PRODUCT_HTML)
    )
    result = await ScraperFactory.get(url).scrape(url)
    assert isinstance(result, ProductData)
    assert result.title == "Generic product"
    assert result.price == Decimal("1299.00")
    assert result.strategy == "json-ld"
    assert result.canonical_url == "https://shop.example.ua/product/1"
    assert len(fast) == 1
    assert fast[0]["domain"] == "shop.example.ua"
    assert fast[0]["ok"] is True
    assert fast[0]["strategy"] == "json-ld"
    assert fast[0]["healed"] is False


@pytest.mark.asyncio
@respx.mock
async def test_generic_scrape_sends_browser_headers(fast):
    route = respx.get("https://shop.example.ua/h").mock(return_value=html_response(PRODUCT_HTML))
    await GenericScraper("https://shop.example.ua/h").scrape("https://shop.example.ua/h")
    headers = route.calls.last.request.headers
    assert "Chrome" in headers["user-agent"]
    assert headers["accept"].startswith("text/html")
    assert headers["accept-language"] == "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7"
    assert headers["upgrade-insecure-requests"] == "1"
    assert headers["sec-fetch-mode"] == "navigate"


@pytest.mark.asyncio
@respx.mock
async def test_bot_wall_is_blocked(fast):
    respx.get("https://shop.example.ua/p").mock(return_value=html_response(BOT_WALL_HTML))
    result = await GenericScraper("https://shop.example.ua/p").scrape("https://shop.example.ua/p")
    assert isinstance(result, ScrapeError) and result.reason == "blocked"
    assert len(fast) == 1 and fast[0]["ok"] is False and fast[0]["reason"] == "blocked"


@pytest.mark.parametrize("title", ["Access Denied", "Please solve the captcha", "Are you a human?", "Attention Required! | Cloudflare"])
def test_bot_wall_titles(title):
    assert scraper_mod._is_bot_wall(f"<html><head><title>{title}</title></head></html>") is True
    assert scraper_mod._is_bot_wall("<html><head><title>Normal shop</title></head></html>") is False


@pytest.mark.asyncio
@respx.mock
async def test_403_after_retries_is_blocked(fast):
    route = respx.get("https://shop.example.ua/p").mock(return_value=httpx.Response(403, text="forbidden"))
    result = await GenericScraper("https://shop.example.ua/p").scrape("https://shop.example.ua/p")
    assert isinstance(result, ScrapeError) and result.reason == "blocked"
    assert route.call_count == 2            # SCRAPE_MAX_RETRIES
    assert len(fast) == 1 and fast[0]["reason"] == "blocked"


@pytest.mark.asyncio
@respx.mock
async def test_429_then_success(fast):
    route = respx.get("https://shop.example.ua/p")
    route.side_effect = [httpx.Response(429), html_response(PRODUCT_HTML)]
    result = await GenericScraper("https://shop.example.ua/p").scrape("https://shop.example.ua/p")
    assert isinstance(result, ProductData)
    assert route.call_count == 2
    assert len(fast) == 1 and fast[0]["ok"] is True


@pytest.mark.asyncio
@respx.mock
async def test_404_is_not_found_without_retry(fast):
    route = respx.get("https://shop.example.ua/gone").mock(return_value=httpx.Response(404))
    result = await GenericScraper("https://shop.example.ua/gone").scrape("https://shop.example.ua/gone")
    assert isinstance(result, ScrapeError) and result.reason == "not_found"
    assert route.call_count == 1
    assert len(fast) == 1 and fast[0]["reason"] == "not_found"


@pytest.mark.asyncio
@respx.mock
async def test_network_error_after_retries(fast):
    route = respx.get("https://shop.example.ua/p").mock(side_effect=httpx.ConnectTimeout("timeout"))
    result = await GenericScraper("https://shop.example.ua/p").scrape("https://shop.example.ua/p")
    assert isinstance(result, ScrapeError) and result.reason == "network"
    assert route.call_count == 2
    assert len(fast) == 1 and fast[0]["reason"] == "network"


@pytest.mark.asyncio
@respx.mock
async def test_5xx_after_retries_is_max_retries(fast):
    respx.get("https://shop.example.ua/p").mock(return_value=httpx.Response(503))
    result = await GenericScraper("https://shop.example.ua/p").scrape("https://shop.example.ua/p")
    assert isinstance(result, ScrapeError) and result.reason == "max_retries_exceeded"
    assert len(fast) == 1 and fast[0]["reason"] == "max_retries_exceeded"


@pytest.mark.asyncio
@respx.mock
async def test_parse_failed_without_healer(fast):
    respx.get("https://shop.example.ua/p").mock(return_value=html_response(NOTHING_HTML))
    result = await GenericScraper("https://shop.example.ua/p").scrape("https://shop.example.ua/p")
    assert isinstance(result, ScrapeError) and result.reason == "parse_failed"
    assert len(fast) == 1 and fast[0]["reason"] == "parse_failed"


@pytest.mark.asyncio
@respx.mock
async def test_non_html_and_too_large_bodies_are_skipped(fast):
    respx.get("https://shop.example.ua/file.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})
    )
    result = await GenericScraper("https://shop.example.ua/file.pdf").scrape("https://shop.example.ua/file.pdf")
    assert isinstance(result, ScrapeError) and result.reason == "parse_failed"

    respx.get("https://shop.example.ua/huge").mock(
        return_value=httpx.Response(200, text=PRODUCT_HTML, headers={**HTML, "content-length": str(6 * 1024 * 1024)})
    )
    result = await GenericScraper("https://shop.example.ua/huge").scrape("https://shop.example.ua/huge")
    assert isinstance(result, ScrapeError) and result.reason == "parse_failed"

    big = b"<html><head><title>x</title></head><body>" + b"a" * (scraper_mod.MAX_BODY_BYTES + 10) + b"</body></html>"
    respx.get("https://shop.example.ua/stream").mock(return_value=httpx.Response(200, content=big, headers=HTML))
    result = await GenericScraper("https://shop.example.ua/stream").scrape("https://shop.example.ua/stream")
    assert isinstance(result, ScrapeError) and result.reason == "parse_failed"
    assert len(fast) == 3


@pytest.mark.asyncio
@respx.mock
async def test_windows_1251_page_is_decoded_via_meta_charset(fast):
    body = ('<html><head><meta http-equiv="Content-Type" content="text/html; charset=windows-1251">'
            '<title>Магазин</title></head><body><h1>Пилосос</h1><span class="price">2 999 грн</span></body></html>'
            ).encode("cp1251")
    respx.get("https://shop.example.ua/cp").mock(return_value=httpx.Response(200, content=body, headers={"content-type": "text/html"}))
    result = await GenericScraper("https://shop.example.ua/cp").scrape("https://shop.example.ua/cp")
    assert isinstance(result, ProductData) and result.title == "Пилосос" and result.price == Decimal("2999.00")


@pytest.mark.asyncio
@respx.mock
async def test_record_scrape_failure_does_not_break_scrape(fast, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(scraper_mod, "record_scrape", boom)
    respx.get("https://shop.example.ua/p").mock(return_value=html_response(PRODUCT_HTML))
    result = await GenericScraper("https://shop.example.ua/p").scrape("https://shop.example.ua/p")
    assert isinstance(result, ProductData)


# ─── Спеціальні парсери: fallback на extract_product ─────────────

@pytest.mark.asyncio
@respx.mock
async def test_rozetka_falls_back_to_structured_data(fast):
    html = PRODUCT_HTML.replace("Generic product", "Rozetka via JSON-LD")
    respx.get("https://rozetka.com.ua/ua/p1/").mock(return_value=html_response(html))
    result = await ScraperFactory.get("https://rozetka.com.ua/ua/p1/?utm=1").scrape("https://rozetka.com.ua/ua/p1/?utm=1")
    assert isinstance(result, ProductData)
    assert result.title == "Rozetka via JSON-LD" and result.strategy == "json-ld"
    assert fast[0]["domain"] == "rozetka.com.ua" and fast[0]["strategy"] == "json-ld"


def test_rozetka_selectors_still_work():
    html = ('<html><body><h1 class="title__font">Rozetka selectors</h1>'
            '<p class="product-price__big">12 999₴</p><p class="product-price__small">14 999₴</p></body></html>')
    result = RozetkaScraper()._parse(html, "https://rozetka.com.ua/ua/p1/")
    assert isinstance(result, ProductData)
    assert result.strategy == "selectors"
    assert result.price == Decimal("12999.00") and result.old_price == Decimal("14999.00")
    assert result.currency == "UAH" and result.is_available is True


def test_rozetka_heuristic_as_last_resort():
    # Ні селекторів, ні назви (h1/title) → parse_failed
    html = '<html><body><h2>no h1 selector</h2><div class="some-price">999 грн</div></body></html>'
    result = RozetkaScraper()._parse(html, "https://rozetka.com.ua/ua/p1/")
    assert isinstance(result, ScrapeError) and result.reason == "parse_failed"
    # Є <title> і елемент із "price" у класі → евристика
    html = '<html><head><title>Heur title</title></head><body><div class="some-price">999 грн</div></body></html>'
    result = RozetkaScraper()._parse(html, "https://rozetka.com.ua/ua/p1/")
    assert isinstance(result, ProductData) and result.strategy == "heuristic" and result.price == Decimal("999.00")


def test_olx_meta_fallback_and_structured():
    html = ('<html><head><meta property="og:title" content="OLX item">'
            '<meta property="product:price:amount" content="1500"></head><body></body></html>')
    result = OLXScraper()._parse(html, "https://www.olx.ua/d/uk/obyavlenie/x.html")
    assert isinstance(result, ProductData) and result.price == Decimal("1500.00")
    assert result.strategy == "opengraph"       # універсальний екстрактор перехоплює раніше за meta-селектори

    html = '<html><head><meta property="og:title" content="OLX item"></head><body></body></html>'
    result = OLXScraper()._parse(html, "https://www.olx.ua/x.html")
    assert isinstance(result, ProductData) and result.strategy == "selectors" and result.price is None


def test_prom_selectors_then_structured():
    html = '<html><body><h1 data-qaid="product_name">Prom item</h1><div data-qaid="product_price">2 499 ₴</div></body></html>'
    result = PromScraper()._parse(html, "https://prom.ua/p1.html")
    assert isinstance(result, ProductData) and result.strategy == "selectors" and result.price == Decimal("2499.00")

    html = PRODUCT_HTML.replace("Generic product", "Prom via JSON-LD")
    result = PromScraper()._parse(html, "https://prom.ua/p1.html")
    assert isinstance(result, ProductData) and result.strategy == "json-ld" and result.title == "Prom via JSON-LD"

    assert isinstance(PromScraper()._parse(NOTHING_HTML, "https://prom.ua/p1.html"), ScrapeError)


@pytest.mark.asyncio
@respx.mock
async def test_specific_scraper_healer_result_is_logged_as_healed(fast, monkeypatch):
    """Якщо Claude знайшов селектори — рядок у журналі має healed=True і strategy=selectors."""
    async def fake_heal(domain, html):
        return {"title": ["h2.t"], "price": ["div.p"], "old_price": [], "availability_negative": []}

    monkeypatch.setattr(scraper_mod._auto_healer, "find_selectors", fake_heal)
    monkeypatch.setattr(scraper_mod._selector_store, "_save", lambda: None)
    saved = scraper_mod._selector_store.get("prom.ua")
    try:
        html = '<html><head><title>P</title></head><body><h2 class="t">Healed</h2><div class="p">10 грн</div></body></html>'
        respx.get("https://prom.ua/p1.html").mock(return_value=html_response(html))
        result = await PromScraper().scrape("https://prom.ua/p1.html")
        assert isinstance(result, ProductData) and result.title == "Healed" and result.strategy == "selectors"
        assert len(fast) == 1 and fast[0]["ok"] is True and fast[0]["healed"] is True
    finally:
        scraper_mod._selector_store._store["prom.ua"] = saved


# ─── Реальні заглушки антиботів з tests/fixtures/*.blocked.html ──

_FIX = Path(__file__).parent / "fixtures"


def _blocked_pages():
    return sorted(_FIX.glob("*.blocked.html")) if _FIX.is_dir() else []


def _verified_pages():
    if not _FIX.is_dir():
        return []
    return sorted(p for p in _FIX.glob("*.html") if not p.name.endswith((".blocked.html", ".needs_js.html")))


@pytest.mark.skipif(not _blocked_pages(), reason="немає фікстур заглушок")
def test_bot_wall_detector_catches_real_walls():
    from app.services.scrapers.scraper import _is_bot_wall
    caught = {p.name for p in _blocked_pages() if _is_bot_wall(p.read_text(errors="replace"))}
    # Ці заглушки приходять зі статусом 200 (або з явними маркерами) — їх детектор зобов'язаний ловити.
    must = {
        "comfy.ua.blocked.html", "intertop.ua.blocked.html", "tabletki.ua.blocked.html",
        "x-kom.pl.blocked.html", "saturn.de.blocked.html", "mediamarkt.de.blocked.html",
        "alza.cz.blocked.html", "bhphotovideo.com.blocked.html", "cdiscount.com.blocked.html",
        "hillary.ua.blocked.html", "quality.in.ua.blocked.html", "otto.de.blocked.html",
        "walmart.com.blocked.html", "elgiganten.se.blocked.html", "argos.co.uk.blocked.html",
        "asos.com.blocked.html", "bestbuy.com.blocked.html", "adidas.com.blocked.html",
    }
    present = {p.name for p in _blocked_pages()}
    assert must & present <= caught, sorted((must & present) - caught)


@pytest.mark.parametrize("path", _verified_pages() or [None], ids=lambda p: p.name if p else "no-fixtures")
def test_bot_wall_detector_no_false_positives(path):
    if path is None:
        pytest.skip("немає фікстур")
    from app.services.scrapers.scraper import _is_bot_wall
    assert not _is_bot_wall(path.read_text(errors="replace")), path.name


def test_bot_wall_detector_alibaba_punish_in_large_page():
    """AliExpress віддає 300 КБ «скелета» без ціни, а API перенаправлено на капчу Alibaba — це blocked."""
    from app.services.scrapers.scraper import _is_bot_wall
    filler = "<div>" + "x" * 200_000 + "</div>"
    html = ("<html><head><title></title></head><body>" + filler +
            '<script src="https://acs.aliexpress.com:443//h5/mtop.aliexpress.pdp.pc.query/1.0/_____tmd_____/punish?x5secdata=abc"></script>'
            "</body></html>")
    assert _is_bot_wall(html)
    assert not _is_bot_wall("<html><head><title>Товар</title></head><body>" + filler + "</body></html>")
