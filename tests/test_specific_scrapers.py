"""
Спеціальні парсери (Rozetka / OLX / Prom): _parse на синтетичному HTML, canonicalize_url, supports(),
SelectorStore на тимчасовому файлі, ClaudeAutoHealer із замоканим API.
Мережа — лише через respx; selector_store.json репозиторію не чіпаємо.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from app.services.scrapers import scraper as scraper_mod
from app.services.scrapers.scraper import (
    HTML_SAMPLE_SIZE,
    ClaudeAutoHealer,
    OLXScraper,
    ProductData,
    PromScraper,
    RozetkaScraper,
    ScrapeError,
    SelectorStore,
)

ROZETKA_URL = "https://rozetka.com.ua/ua/apple-iphone/p123/"
OLX_URL = "https://www.olx.ua/d/uk/obyavlenie/iphone-IDabc.html"
PROM_URL = "https://prom.ua/ua/p123-iphone.html"
ROZETKA_DEBUG_PAGE = Path(__file__).resolve().parents[1] / "debug" / "rozetka_debug.html"


def ld_html(name: str, price: str, currency: str = "UAH", availability: str = "InStock") -> str:
    ld = json.dumps({
        "@context": "https://schema.org", "@type": "Product", "name": name,
        "offers": {"@type": "Offer", "price": price, "priceCurrency": currency,
                   "availability": f"https://schema.org/{availability}"},
    })
    return f'<html><head><title>{name}</title><script type="application/ld+json">{ld}</script></head><body></body></html>'


# ─── Rozetka ─────────────────────────────────────────────────────

def test_rozetka_selectors_out_of_stock_marker():
    html = ('<html><body><h1 class="title__font">Товар</h1><p class="product-price__big">999₴</p>'
            '<div>Немає в наявності</div></body></html>')
    result = RozetkaScraper()._parse(html, ROZETKA_URL)
    assert isinstance(result, ProductData)
    assert result.price == Decimal("999.00") and result.is_available is False
    assert result.canonical_url == ROZETKA_URL and result.strategy == "selectors"


def test_rozetka_selectors_without_price_is_unavailable():
    html = '<html><body><h1 class="title__font">Без ціни</h1></body></html>'
    result = RozetkaScraper()._parse(html, ROZETKA_URL)
    assert isinstance(result, ProductData)
    assert result.title == "Без ціни" and result.price is None and result.is_available is False
    assert result.old_price is None and result.currency == "UAH"


def test_rozetka_json_ld_wins_over_selectors():
    html = ld_html("Rozetka JSON-LD", "69999").replace(
        "<body></body>", '<body><h1 class="title__font">Інша назва</h1><p class="product-price__big">1₴</p></body>')
    result = RozetkaScraper()._parse(html, ROZETKA_URL)
    assert isinstance(result, ProductData)
    assert result.strategy == "json-ld" and result.title == "Rozetka JSON-LD" and result.price == Decimal("69999")


def test_rozetka_json_ld_out_of_stock():
    result = RozetkaScraper()._parse(ld_html("X", "100", availability="OutOfStock"), ROZETKA_URL)
    assert isinstance(result, ProductData) and result.is_available is False


@pytest.mark.parametrize("html", ["", "<html></html>", "<html><body><p>nothing</p></body></html>"])
def test_rozetka_parse_failed(html):
    result = RozetkaScraper()._parse(html, ROZETKA_URL)
    assert isinstance(result, ScrapeError) and result.reason == "parse_failed" and result.url == ROZETKA_URL


def test_rozetka_real_debug_page():
    if not ROZETKA_DEBUG_PAGE.exists():
        pytest.skip("debug/rozetka_debug.html відсутній")
    html = ROZETKA_DEBUG_PAGE.read_text(encoding="utf-8", errors="replace")
    result = RozetkaScraper()._parse(html, ROZETKA_URL)
    assert isinstance(result, ProductData), result
    assert result.title.strip()
    assert result.price == Decimal("69999")
    assert result.currency == "UAH"
    assert result.canonical_url == ROZETKA_URL


# ─── OLX ─────────────────────────────────────────────────────────

def test_olx_json_ld():
    result = OLXScraper()._parse(ld_html("OLX JSON-LD", "1500"), OLX_URL)
    assert isinstance(result, ProductData)
    assert result.strategy == "json-ld" and result.price == Decimal("1500") and result.currency == "UAH"


def test_olx_meta_title_only_falls_back_to_selectors_without_price():
    html = '<html><head><meta property="og:title" content="OLX title only"></head><body></body></html>'
    result = OLXScraper()._parse(html, OLX_URL)
    assert isinstance(result, ProductData)
    assert result.title == "OLX title only" and result.price is None and result.is_available is False
    assert result.strategy == "selectors" and result.canonical_url == OLX_URL


def test_olx_custom_meta_names_from_store(monkeypatch):
    store = SelectorStore.__new__(SelectorStore)
    store._store = {"olx.ua": {"title_meta": "custom:title", "price_meta": "custom:price"}}
    monkeypatch.setattr(scraper_mod, "_selector_store", store)
    html = ('<html><head><meta property="custom:title" content="Custom OLX">'
            '<meta property="custom:price" content="2 500"></head><body></body></html>')
    result = OLXScraper()._parse(html, OLX_URL)
    assert isinstance(result, ProductData)
    assert result.title == "Custom OLX" and result.price == Decimal("2500") and result.is_available is True


@pytest.mark.parametrize("html", ["", "<html><body><div>no meta</div></body></html>"])
def test_olx_parse_failed(html):
    result = OLXScraper()._parse(html, OLX_URL)
    assert isinstance(result, ScrapeError) and result.reason == "parse_failed"


# ─── Prom ────────────────────────────────────────────────────────

def test_prom_selectors():
    html = '<html><body><h1 data-qaid="product_name"> Prom item </h1><span data-qaid="product_price">12 499,50 ₴</span></body></html>'
    result = PromScraper()._parse(html, PROM_URL)
    assert isinstance(result, ProductData)
    assert result.title == "Prom item" and result.price == Decimal("12499.50")
    assert result.strategy == "selectors" and result.is_available is True and result.currency == "UAH"


def test_prom_title_without_price_uses_json_ld_when_present():
    html = ld_html("Prom LD", "777").replace("<body></body>", '<body><h1 data-qaid="product_name">Selectors</h1></body>')
    result = PromScraper()._parse(html, PROM_URL)
    assert isinstance(result, ProductData) and result.strategy == "json-ld" and result.price == Decimal("777")


def test_prom_title_without_price_and_no_structured_data():
    html = '<html><body><h1 data-qaid="product_name">Only title</h1></body></html>'
    result = PromScraper()._parse(html, PROM_URL)
    assert isinstance(result, ProductData)
    assert result.title == "Only title" and result.price is None and result.is_available is False


@pytest.mark.parametrize("html", ["", "<html><body><p>nothing</p></body></html>"])
def test_prom_parse_failed(html):
    result = PromScraper()._parse(html, PROM_URL)
    assert isinstance(result, ScrapeError) and result.reason == "parse_failed"


# ─── canonicalize_url / supports ─────────────────────────────────

@pytest.mark.parametrize("cls, url, expected", [
    (RozetkaScraper, "https://rozetka.com.ua/ua/p1/?utm_source=x&gclid=1#tab", "https://rozetka.com.ua/ua/p1/"),
    (RozetkaScraper, "https://www.rozetka.com.ua/ua/p1/", "https://www.rozetka.com.ua/ua/p1/"),   # www лишаємо
    (OLXScraper, "https://www.olx.ua/d/uk/obyavlenie/x-IDa.html?ref=1#top", "https://www.olx.ua/d/uk/obyavlenie/x-IDa.html"),
    (PromScraper, "https://prom.ua/ua/p1.html?a=b;c=d", "https://prom.ua/ua/p1.html"),
    (PromScraper, "http://shop.prom.ua/p1.html", "http://shop.prom.ua/p1.html"),
])
def test_canonicalize_url(cls, url, expected):
    assert cls().canonicalize_url(url) == expected


def test_canonicalize_is_idempotent():
    for cls, url in ((RozetkaScraper, ROZETKA_URL + "?x=1"), (OLXScraper, OLX_URL + "?x=1"), (PromScraper, PROM_URL + "?x=1")):
        once = cls().canonicalize_url(url)
        assert cls().canonicalize_url(once) == once


@pytest.mark.parametrize("cls, url, expected", [
    (RozetkaScraper, "https://rozetka.com.ua/ua/p1/", True),
    (RozetkaScraper, "https://www.rozetka.com.ua/ua/p1/", True),
    (RozetkaScraper, "https://m.rozetka.com.ua/ua/p1/", True),             # піддомен
    (RozetkaScraper, "HTTPS://ROZETKA.COM.UA/ua/p1/", True),               # регістр
    (RozetkaScraper, "https://rozetka.com.ua.evil.com/ua/p1/", False),     # схожий домен
    (RozetkaScraper, "https://notrozetka.com.ua/ua/p1/", False),
    (RozetkaScraper, "https://rozetka.com/ua/p1/", False),
    (RozetkaScraper, "rozetka.com.ua/ua/p1/", False),                      # без схеми — hostname відсутній
    (RozetkaScraper, "", False),
    (OLXScraper, "https://www.olx.ua/d/uk/x.html", True),
    (OLXScraper, "https://olx.pl/d/x.html", False),
    (OLXScraper, "https://olx.ua.example.com/x", False),
    (PromScraper, "https://prom.ua/p1.html", True),
    (PromScraper, "https://my-shop.prom.ua/p1.html", True),
    (PromScraper, "https://prom.ua.fake.io/p1.html", False),
    (PromScraper, "https://bigl.ua/p1", False),
])
def test_supports(cls, url, expected):
    assert cls.supports(url) is expected


# ─── SelectorStore ───────────────────────────────────────────────

@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    path = tmp_path / "selector_store.json"
    monkeypatch.setattr(scraper_mod, "SELECTOR_STORE_PATH", path)
    return path


def test_selector_store_creates_defaults(tmp_store):
    assert not tmp_store.exists()
    store = SelectorStore()
    assert tmp_store.exists()
    data = json.loads(tmp_store.read_text())
    assert set(data) == set(SelectorStore.DEFAULTS)
    assert data["rozetka.com.ua"]["updated_by"] == "default" and "updated_at" in data["rozetka.com.ua"]
    assert store.get("rozetka.com.ua")["title"] == SelectorStore.DEFAULTS["rozetka.com.ua"]["title"]
    assert store.get("unknown.example") == {}


def test_selector_store_update_round_trip(tmp_store):
    store = SelectorStore()
    store.update("newshop.ua", {"title": ["h1.x"], "price": ["span.p"]}, updated_by="test")
    saved = json.loads(tmp_store.read_text())["newshop.ua"]
    assert saved["title"] == ["h1.x"] and saved["updated_by"] == "test" and saved["updated_at"]

    reloaded = SelectorStore()                       # новий екземпляр читає файл, не дефолти
    assert reloaded.get("newshop.ua")["price"] == ["span.p"]
    assert reloaded.get("rozetka.com.ua")["title"]  # дефолти теж збережені


def test_selector_store_update_overwrites_domain(tmp_store):
    store = SelectorStore()
    store.update("rozetka.com.ua", {"title": ["h1.new"]})
    assert store.get("rozetka.com.ua") == {"title": ["h1.new"], "updated_at": store.get("rozetka.com.ua")["updated_at"],
                                           "updated_by": "claude-auto"}


def test_selector_store_corrupt_file_falls_back_to_defaults(tmp_store):
    tmp_store.write_text("{not json")
    store = SelectorStore()
    assert store.get("prom.ua")["title"] == SelectorStore.DEFAULTS["prom.ua"]["title"]
    assert json.loads(tmp_store.read_text())["prom.ua"]["updated_by"] == "default"   # файл перезаписано


def test_rozetka_uses_selectors_from_store(tmp_store, monkeypatch):
    store = SelectorStore()
    store.update("rozetka.com.ua", {"title": ["h1.custom-title"], "price": ["div.custom-price"],
                                    "old_price": [], "availability_negative": ["розпродано"]}, updated_by="test")
    monkeypatch.setattr(scraper_mod, "_selector_store", store)
    html = '<html><body><h1 class="custom-title">Custom</h1><div class="custom-price">5 000 грн</div></body></html>'
    result = RozetkaScraper()._parse(html, ROZETKA_URL)
    assert isinstance(result, ProductData) and result.title == "Custom" and result.price == Decimal("5000")
    assert result.is_available is True
    result = RozetkaScraper()._parse(html + "<p>Розпродано</p>", ROZETKA_URL)
    assert isinstance(result, ProductData) and result.is_available is False


def test_repo_selector_store_untouched_by_tests():
    """Тести не повинні переписувати selector_store.json репозиторію (store модуля читає його лише раз)."""
    assert scraper_mod.SELECTOR_STORE_PATH == Path("selector_store.json")


# ─── ClaudeAutoHealer ────────────────────────────────────────────

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GOOD_SELECTORS = {"title": ["h1.t"], "price": ["span.p"], "old_price": [], "availability_negative": ["немає"]}


def _claude_response(payload: dict | str, status: int = 200) -> httpx.Response:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(status, json={"content": [{"text": text}]})


@respx.mock
async def test_healer_returns_selectors_without_confidence(monkeypatch):
    monkeypatch.setattr(scraper_mod.settings, "ANTHROPIC_API_KEY", "sk-test")
    route = respx.post(ANTHROPIC_URL).mock(return_value=_claude_response({**GOOD_SELECTORS, "confidence": 0.95}))
    result = await ClaudeAutoHealer().find_selectors("shop.ua", "<html><body><main><h1 class='t'>x</h1></main></body></html>")
    assert result == GOOD_SELECTORS                       # confidence вирізано
    request = route.calls.last.request
    assert request.headers["x-api-key"] == "sk-test"
    body = json.loads(request.content)
    assert body["messages"][0]["content"].startswith("Домен: shop.ua")
    assert "<h1" in body["messages"][0]["content"]


@respx.mock
async def test_healer_strips_markdown_fences():
    respx.post(ANTHROPIC_URL).mock(return_value=_claude_response("```json\n" + json.dumps(GOOD_SELECTORS) + "\n```"))
    assert await ClaudeAutoHealer().find_selectors("shop.ua", "<html></html>") == GOOD_SELECTORS


@respx.mock
async def test_healer_low_confidence_returns_none():
    respx.post(ANTHROPIC_URL).mock(return_value=_claude_response({**GOOD_SELECTORS, "confidence": 0.4}))
    assert await ClaudeAutoHealer().find_selectors("shop.ua", "<html></html>") is None


@respx.mock
@pytest.mark.parametrize("response", [
    _claude_response("not json at all"),
    _claude_response({}, status=500),
    httpx.Response(200, json={"content": []}),
])
async def test_healer_bad_responses_return_none(response):
    respx.post(ANTHROPIC_URL).mock(return_value=response)
    assert await ClaudeAutoHealer().find_selectors("shop.ua", "<html></html>") is None


@respx.mock
async def test_healer_network_error_returns_none():
    respx.post(ANTHROPIC_URL).mock(side_effect=httpx.ConnectError("boom"))
    assert await ClaudeAutoHealer().find_selectors("shop.ua", "<html></html>") is None


@respx.mock
async def test_healer_with_empty_api_key_returns_none():
    """Без ключа API відповідає 401 → None. (Раннього виходу немає — запит усе одно йде, з порожнім ключем.)"""
    assert scraper_mod.settings.ANTHROPIC_API_KEY == ""
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(401, json={"error": "authentication_error"}))
    assert await ClaudeAutoHealer().find_selectors("shop.ua", "<html></html>") is None
    assert route.called and route.calls.last.request.headers["x-api-key"] == ""


def test_healer_extracts_relevant_html():
    html = ("<html><head><title>T</title><style>.x{}</style></head><body><nav>menu</nav>"
            "<script>var a=1</script><main><h1>Product</h1><p>" + "x" * (HTML_SAMPLE_SIZE * 2) + "</p></main>"
            "<footer>f</footer></body></html>")
    sample = ClaudeAutoHealer()._extract_relevant_html(html)
    assert sample.startswith("<main>") and "<h1>Product</h1>" in sample
    assert "var a=1" not in sample and "menu" not in sample
    assert len(sample) == HTML_SAMPLE_SIZE


def test_healer_extracts_product_block_when_no_main():
    html = '<html><body><div class="header">h</div><div class="product-card"><h1>P</h1></div></body></html>'
    sample = ClaudeAutoHealer()._extract_relevant_html(html)
    assert sample.startswith('<div class="product-card">')
