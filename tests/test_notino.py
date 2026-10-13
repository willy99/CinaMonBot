"""
Notino: ціна вибраного варіанта зі стану сторінки (__APOLLO_STATE__), а не з JSON-LD.

Фікстури — реальні сторінки notino.ua (13.10.2026), стиснуті до JSON-LD + варіантів у стані:
- biorepair.html — товар з одним варіантом (75 мл, 250 грн, стара ціна 260);
- tepe.html — 9 варіантів (розміри щіточок); JSON-LD описує варіант за замовчуванням (1,1 mm).
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.scrapers import notino
from app.services.scrapers.registry import platform_for
from app.services.scrapers.scraper import GenericScraper, NotinoScraper, ScrapeError, ScraperFactory

FIX = Path(__file__).parent / "fixtures" / "notino"
BIOREPAIR = "https://www.notino.ua/biorepair/total-protective-repair-zmicnjujucha-zubna-pasta/"
TEPE = "https://www.notino.ua/tepe/original-mizhzubni-sshitochki-8-sht/"


def html(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ─── Розпізнавання сайту і фабрика ───────────────────────────────

@pytest.mark.parametrize("url, expected", [
    (BIOREPAIR, True),
    ("https://notino.ua/x/y/", True),
    ("https://www.notino.pl/dior/sauvage/p-123/", True),
    ("https://www.notino.co.uk/x/", True),
    ("https://notino.ua.evil.com/x/", False),
    ("https://fakenotino.ua/x/", False),
    ("ftp://www.notino.ua/x/", False),
    ("not a url", False),
])
def test_supports(url, expected):
    assert NotinoScraper.supports(url) is expected


def test_factory_returns_notino_with_host_domain():
    sc = ScraperFactory.get("https://www.notino.pl/x/y/p-1/")
    assert isinstance(sc, NotinoScraper) and sc.domain == "notino.pl"
    assert ScraperFactory.get(BIOREPAIR).domain == "notino.ua"
    assert ScraperFactory.is_specific(BIOREPAIR)
    assert isinstance(ScraperFactory.get("https://shop.example.com/p"), GenericScraper)


def test_canonicalize_drops_query_keeps_variant_path():
    sc = NotinoScraper(TEPE)
    assert sc.canonicalize_url(TEPE + "p-541146/?utm_source=x&gclid=1#reviews") == TEPE + "p-541146/"
    assert sc.canonicalize_url("HTTPS://WWW.Notino.UA/tepe/a/") == "https://www.notino.ua/tepe/a/"


def test_registry_has_notino():
    p = platform_for("www.notino.ua")
    assert p and p.name == "Notino" and p.status == "verified" and p.strategy == "notino-state"


@pytest.mark.parametrize("url, expected", [
    (TEPE + "p-541146/", "541146"),
    (TEPE + "p-541146", "541146"),
    (TEPE, None),
    ("https://www.notino.ua/x/?p-123/", None),        # лише шлях, не query
])
def test_variant_id_from_url(url, expected):
    assert notino.variant_id_from_url(url) == expected


# ─── Вибір варіанта на реальних сторінках ────────────────────────

def test_single_variant_product():
    p = notino.extract_notino(html("biorepair.html"), BIOREPAIR)
    assert p.title == "Biorepair Total Protective Repair · 75 мл"
    assert p.price == Decimal("250.00") and p.old_price == Decimal("260.00")
    assert p.currency == "UAH" and p.is_available is True and p.strategy == "notino-state"
    assert p.image_url.startswith("https://cdn.notinoimg.com/order_2k/biorepair/")


def test_base_url_takes_json_ld_default_variant():
    p = notino.extract_notino(html("tepe.html"), TEPE)
    assert p.title == "TePe Original · 1,1 mm 8 кс"


@pytest.mark.parametrize("vid, size, price, old", [
    ("541146", "0,8 mm", "180.00", "250.00"),
    ("541156", "0,7 mm", "180.00", "250.00"),
    ("541149", "0,6 mm", "190.00", "250.00"),     # реально інша ціна, ніж у варіанта за замовчуванням
    ("541142", "1,3 mm", "190.00", "240.00"),     # і своя «стара» ціна
])
def test_variant_url_takes_that_variant(vid, size, price, old):
    """JSON-LD на всіх цих сторінках однаковий — ціну/назву має дати саме варіант із посилання."""
    p = notino.extract_notino(html("tepe.html"), f"{TEPE}p-{vid}/")
    assert p.title == f"TePe Original · {size} 8 кс"
    assert p.price == Decimal(price) and p.old_price == Decimal(old)
    assert p.image_url.startswith("https://cdn.notinoimg.com/order_2k/tepe/")


def test_unknown_variant_returns_none_not_another_variant():
    assert notino.extract_notino(html("tepe.html"), f"{TEPE}p-999999/") is None


def _state_page(variants: dict, ld_sku: str | None = None, ld_vid: str | None = None) -> str:
    state = {f"CatalogVariant:{vid}": {"webId": vid, **v} for vid, v in variants.items()}
    state["Brand:1"] = {"name": "Dior"}
    ld = ""
    if ld_sku or ld_vid:
        offer = {"@type": "Offer", "price": 9999, "priceCurrency": "UAH", "sku": ld_sku,
                 "url": f"https://www.notino.ua/dior/sauvage/p-{ld_vid}/" if ld_vid else None}
        ld = '<script type="application/ld+json">' + json.dumps({"@type": "Product", "name": "X", "offers": offer}) + "</script>"
    return (f"<html><head>{ld}<script id=\"__APOLLO_STATE__\" type=\"application/json\">"
            f"{json.dumps(state, ensure_ascii=False)}</script></head></html>")


PERFUME = {
    "100": {"name": "Sauvage", "additionalInfo": "60 мл", "productCode": "S60", "brand": {"__ref": "Brand:1"},
            "price": {"value": 2970, "currency": "UAH"}, "availability": {"state": "CanBeBought"}},
    "200": {"name": "Sauvage", "additionalInfo": "100 мл", "productCode": "S100", "brand": {"__ref": "Brand:1"},
            "price": {"value": 4150, "currency": "UAH"}, "originalPrice": {"value": 4000, "currency": "UAH"},
            "availability": {"state": "CanBeBought"}},
    "300": {"name": "Sauvage", "additionalInfo": "200 мл", "productCode": "S200", "brand": {"__ref": "Brand:1"},
            "price": {"value": 6100.5, "currency": "UAH"}, "availability": {"state": "OutOfStock"}},
}


def test_different_volumes_have_different_prices():
    page = _state_page(PERFUME, ld_sku="S60", ld_vid="100")
    base = notino.extract_notino(page, "https://www.notino.ua/dior/sauvage/")
    v100 = notino.extract_notino(page, "https://www.notino.ua/dior/sauvage/p-200/")
    v200 = notino.extract_notino(page, "https://www.notino.ua/dior/sauvage/p-300/")
    assert (base.title, base.price) == ("Dior Sauvage · 60 мл", Decimal("2970.00"))
    assert (v100.title, v100.price) == ("Dior Sauvage · 100 мл", Decimal("4150.00"))
    assert v100.old_price is None                                       # "стара" нижча за поточну — не знижка
    assert v200.price == Decimal("6100.50") and v200.is_available is False


def test_default_variant_by_sku_when_offer_url_missing():
    page = _state_page(PERFUME, ld_sku="S100")
    assert notino.extract_notino(page, "https://www.notino.ua/dior/sauvage/").title.endswith("100 мл")


def test_no_state_or_no_price():
    assert notino.extract_notino("<html></html>", TEPE) is None
    broken = '<script id="__APOLLO_STATE__" type="application/json">{not json</script>'
    assert notino.extract_notino(broken, TEPE) is None
    no_price = _state_page({"1": {"name": "X", "price": {"value": 0, "currency": "UAH"}}})
    assert notino.extract_notino(no_price, "https://www.notino.ua/x/") is None


# ─── NotinoScraper._parse: fallback ──────────────────────────────

def test_parse_falls_back_to_json_ld_without_state_on_base_url():
    page = ('<script type="application/ld+json">{"@type":"Product","name":"Paste","offers":'
            '{"@type":"Offer","price":"250","priceCurrency":"UAH","availability":"https://schema.org/InStock"}}</script>')
    r = NotinoScraper(BIOREPAIR)._parse(page, BIOREPAIR)
    assert r.price == Decimal("250.00") and r.strategy == "json-ld"


def test_parse_refuses_json_ld_for_explicit_variant_without_state():
    """Посилання на конкретний об'єм, а стану немає: JSON-LD дав би ціну іншого об'єму — краще помилка."""
    page = ('<script type="application/ld+json">{"@type":"Product","name":"Paste","offers":'
            '{"@type":"Offer","price":"250","priceCurrency":"UAH"}}</script>')
    r = NotinoScraper(TEPE)._parse(page, TEPE + "p-541146/")
    assert isinstance(r, ScrapeError) and r.reason == "parse_failed"


def test_parse_real_fixture():
    r = NotinoScraper(TEPE)._parse(html("tepe.html"), TEPE + "p-541146/")
    assert r.strategy == "notino-state" and "0,8 mm" in r.title
