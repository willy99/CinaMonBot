"""
Тести універсального екстрактора (app/services/scrapers/extract.py): без мережі.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.scrapers.extract import (
    clean_title,
    detect_currency,
    extract_product,
    parse_availability,
    parse_price_text,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
URL = "https://shop.example.ua/product/123"


def page(head: str = "", body: str = "") -> str:
    return f"<!doctype html><html><head><meta charset='utf-8'>{head}</head><body>{body}</body></html>"


def ld(data) -> str:
    return f'<script type="application/ld+json">{json.dumps(data, ensure_ascii=False)}</script>'


# ─── parse_price_text ────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("1 299,99 грн", "1299.99"),
    ("1,299.00", "1299.00"),
    ("12.99", "12.99"),
    ("1.299,00 €", "1299.00"),
    ("1 299", "1299.00"),
    ("12,99", "12.99"),
    ("€12.99 - €19.99", "12.99"),
    ("од 1 299 грн", "1299.00"),
    ("1 299 грн", "1299.00"),          # nbsp
    ("1 299,50 zł", "1299.50"),             # narrow nbsp
    ("₴ 999", "999.00"),
    ("12 999 ₴", "12999.00"),
    ("1 299 999", "1299999.00"),
    ("1.299.000", "1299000.00"),
    ("1,299,000.50", "1299000.50"),
    ("12.999", "12999.00"),
    ("12.5", "12.50"),
    ("$1,234.5", "1234.50"),
    ("1 299.99", "1299.99"),
    ("12 3", "12.00"),                           # друга група не з 3 цифр — не тисячі
    ("Ціна: 5 шт", "5.00"),
    ("від 2 499,00 грн до 3 000 грн", "2499.00"),
    ("999.", "999.00"),
    ("1'299.00 CHF", "1299.00"),
    ("1.0051", "1.01"),                           # 2 знаки, ROUND_HALF_UP
    ("1.005", "1005.00"),                         # 3 цифри після роздільника → тисячі
    (1299, "1299.00"),
    (12.5, "12.50"),
    (Decimal("7"), "7.00"),
])
def test_parse_price_text(text, expected):
    assert parse_price_text(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["", "   ", "немає в наявності", "0", "0,00", "-5", None, True])
def test_parse_price_text_rejects(text):
    assert parse_price_text(text) is None


# ─── detect_currency ─────────────────────────────────────────────

@pytest.mark.parametrize("text,url,expected", [
    ("1 299 грн", "", "UAH"),
    ("₴999", "", "UAH"),
    ("$12", "", "USD"),
    ("12 USD", "", "USD"),
    ("€12", "", "EUR"),
    ("£12", "", "GBP"),
    ("12 zł", "", "PLN"),
    ("120 Kč", "", "CZK"),
    ("₺120", "", "TRY"),
    ("₸120", "", "KZT"),
    ("12", "https://shop.ua/x", "UAH"),
    ("12", "https://shop.com.ua/x", "UAH"),
    ("12", "https://shop.pl/x", "PLN"),
    ("12", "https://shop.de/x", "EUR"),
    ("12", "https://shop.fr/x", "EUR"),
    ("12", "https://shop.cz/x", "CZK"),
    ("12", "https://shop.co.uk/x", "GBP"),
    ("12", "https://shop.com/x", "USD"),
    ("12", "https://shop.xyz/x", "UAH"),          # невідома зона → дефолт
    ("12 грн", "https://shop.com/x", "UAH"),      # символ важливіший за TLD
])
def test_detect_currency(text, url, expected):
    assert detect_currency(text, url) == expected


# ─── parse_availability ──────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("https://schema.org/InStock", True),
    ("http://schema.org/InStock", True),
    ("schema.org/InStock", True),
    ("InStock", True),
    ("instock", True),
    ("in stock", True),
    ("https://schema.org/LimitedAvailability", True),
    ("https://schema.org/PreOrder", True),
    ("https://schema.org/BackOrder", True),
    ("https://schema.org/OutOfStock", False),
    ("OutOfStock", False),
    ("out of stock", False),
    ("oos", False),
    ("https://schema.org/SoldOut", False),
    ("https://schema.org/Discontinued", False),
    ("Немає в наявності", False),
    ("В наявності", True),
    ({"@id": "https://schema.org/InStock"}, True),
    (["https://schema.org/OutOfStock"], False),
    (None, None),
    ("", None),
    ("something else", None),
])
def test_parse_availability(value, expected):
    assert parse_availability(value) is expected


# ─── JSON-LD ─────────────────────────────────────────────────────

def test_json_ld_basic_product():
    html = page(ld({
        "@context": "https://schema.org", "@type": "Product", "name": "  Телефон   Test  ",
        "image": "https://img/1.jpg",
        "offers": {"@type": "Offer", "price": "1299.00", "priceCurrency": "UAH",
                   "availability": "https://schema.org/InStock", "url": URL},
    }))
    p = extract_product(html, URL)
    assert p is not None
    assert p.strategy == "json-ld"
    assert p.title == "Телефон Test"
    assert p.price == Decimal("1299.00")
    assert p.currency == "UAH"
    assert p.is_available is True
    assert p.image_url == "https://img/1.jpg"
    assert p.canonical_url == URL


def test_json_ld_type_list_and_numeric_price():
    html = page(ld({
        "@type": ["Product", "IndividualProduct"], "name": "X",
        "offers": {"@type": "Offer", "price": 999, "priceCurrency": "usd"},
    }))
    p = extract_product(html, "https://shop.com/x")
    assert p and p.strategy == "json-ld" and p.price == Decimal("999.00") and p.currency == "USD"


def test_json_ld_graph_and_multiple_scripts():
    html = page(
        ld({"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": []})
        + ld({"@context": "https://schema.org", "@graph": [
            {"@type": "WebPage", "name": "page"},
            {"@type": "Product", "name": "Graph product",
             "offers": {"@type": "Offer", "price": "10.50", "priceCurrency": "EUR"}},
        ]})
    )
    p = extract_product(html, "https://shop.de/x")
    assert p and p.title == "Graph product" and p.price == Decimal("10.50") and p.currency == "EUR"


def test_json_ld_top_level_array():
    html = page(ld([
        {"@type": "Organization", "name": "Shop"},
        {"@type": "Product", "name": "Array product", "offers": [{"@type": "Offer", "price": "5"}]},
    ]))
    p = extract_product(html, URL)
    assert p and p.title == "Array product" and p.price == Decimal("5.00")


def test_json_ld_aggregate_offer_uses_low_price():
    html = page(ld({
        "@type": "Product", "name": "Agg",
        "offers": {"@type": "AggregateOffer", "lowPrice": "1 299,00", "highPrice": "1 599,00",
                   "priceCurrency": "UAH", "offerCount": 3},
    }))
    p = extract_product(html, URL)
    assert p and p.price == Decimal("1299.00")


def test_json_ld_aggregate_offer_with_nested_offers_prefers_page_url():
    html = page(ld({
        "@type": "Product", "name": "Agg nested",
        "offers": {"@type": "AggregateOffer", "lowPrice": 100, "offers": [
            {"@type": "Offer", "price": 150, "url": "https://shop.example.ua/product/999"},
            {"@type": "Offer", "price": 120, "url": "https://www.shop.example.ua/product/123/"},
        ]},
    }))
    p = extract_product(html, URL)
    assert p and p.price == Decimal("120.00")


def test_json_ld_offers_list_first_with_price():
    html = page(ld({
        "@type": "Product", "name": "List",
        "offers": [
            {"@type": "Offer", "availability": "https://schema.org/OutOfStock"},
            {"@type": "Offer", "price": "49.90", "priceCurrency": "PLN", "availability": "https://schema.org/InStock"},
        ],
    }))
    p = extract_product(html, "https://shop.pl/x")
    assert p and p.price == Decimal("49.90") and p.currency == "PLN" and p.is_available is True


def test_json_ld_price_specification():
    html = page(ld({
        "@type": "Product", "name": "Spec",
        "offers": {"@type": "Offer", "priceSpecification": [
            {"@type": "UnitPriceSpecification", "priceType": "https://schema.org/ListPrice", "price": 200, "priceCurrency": "EUR"},
            {"@type": "UnitPriceSpecification", "price": "150.00", "priceCurrency": "EUR"},
        ]},
    }))
    p = extract_product(html, "https://shop.de/x")
    assert p and p.price == Decimal("150.00") and p.old_price == Decimal("200.00") and p.currency == "EUR"


def test_json_ld_product_group_has_variant_matching_url():
    html = page(ld({
        "@type": "ProductGroup", "name": "Group", "hasVariant": [
            {"@type": "Product", "name": "Variant red", "url": "https://shop.example.ua/product/111",
             "offers": {"@type": "Offer", "price": 11, "priceCurrency": "UAH"}},
            {"@type": "Product", "name": "Variant blue", "url": URL,
             "offers": {"@type": "Offer", "price": 22, "priceCurrency": "UAH",
                        "availability": "http://schema.org/OutOfStock"}},
        ],
    }))
    p = extract_product(html, URL)
    assert p and p.title == "Variant blue" and p.price == Decimal("22.00") and p.is_available is False


def test_json_ld_product_group_falls_back_to_first_priced_variant():
    html = page(ld({
        "@type": "ProductGroup", "name": "Group", "hasVariant": [
            {"@type": "Product", "name": "No price"},
            {"@type": "Product", "name": "Priced", "offers": {"@type": "Offer", "price": "7.00"}},
        ],
    }))
    p = extract_product(html, URL)
    assert p and p.title == "Priced" and p.price == Decimal("7.00")


@pytest.mark.parametrize("availability,expected", [
    ("https://schema.org/InStock", True),
    ("http://schema.org/InStock", True),
    ("InStock", True),
    ("https://schema.org/OutOfStock", False),
    ("https://schema.org/SoldOut", False),
    ("https://schema.org/PreOrder", True),
    ("https://schema.org/Discontinued", False),
])
def test_json_ld_availability_variants(availability, expected):
    html = page(ld({"@type": "Product", "name": "A",
                    "offers": {"@type": "Offer", "price": 1, "availability": availability}}))
    p = extract_product(html, URL)
    assert p and p.is_available is expected


@pytest.mark.parametrize("image,expected", [
    ("https://img/a.jpg", "https://img/a.jpg"),
    (["https://img/b.jpg", "https://img/c.jpg"], "https://img/b.jpg"),
    ({"@type": "ImageObject", "url": "https://img/d.jpg"}, "https://img/d.jpg"),
    ([{"@type": "ImageObject", "contentUrl": "https://img/e.jpg"}], "https://img/e.jpg"),
    (None, None),
])
def test_json_ld_image_shapes(image, expected):
    data = {"@type": "Product", "name": "I", "offers": {"price": 1}}
    if image is not None:
        data["image"] = image
    p = extract_product(page(ld(data)), URL)
    assert p and p.image_url == expected


def test_json_ld_tolerates_trailing_commas_and_comments():
    raw = """<script type="application/ld+json">
    <!--
    {
      "@context": "https://schema.org",
      "@type": "Product",
      "name": "Broken JSON",
      "offers": {"@type": "Offer", "price": "1 299,99", "priceCurrency": "UAH",},
    }
    -->
    </script>"""
    p = extract_product(page(raw), URL)
    assert p and p.strategy == "json-ld" and p.title == "Broken JSON" and p.price == Decimal("1299.99")


def test_json_ld_invalid_json_falls_through():
    html = page(
        '<script type="application/ld+json">{{{not json</script>'
        '<meta property="og:title" content="OG title"><meta property="product:price:amount" content="5">',
    )
    p = extract_product(html, URL)
    assert p and p.strategy == "opengraph" and p.title == "OG title"


def test_json_ld_type_with_charset_attribute():
    html = page('<script type="application/ld+json; charset=utf-8">'
                '{"@type": "Product", "name": "Charset", "offers": {"price": 3}}</script>')
    p = extract_product(html, URL)
    assert p and p.strategy == "json-ld"


def test_json_ld_without_price_still_returns_product_when_nothing_else():
    html = page(ld({"@type": "Product", "name": "No price at all"}))
    p = extract_product(html, URL)
    assert p and p.strategy == "json-ld" and p.price is None and p.is_available is False


def test_json_ld_without_price_lets_opengraph_win():
    html = page(
        ld({"@type": "Product", "name": "LD name"})
        + '<meta property="og:title" content="OG name"><meta property="product:price:amount" content="42">'
    )
    p = extract_product(html, URL)
    assert p and p.strategy == "opengraph" and p.price == Decimal("42.00")


def test_json_ld_rejects_zero_price_and_uses_next_offer():
    html = page(ld({"@type": "Product", "name": "Z", "offers": [{"price": 0}, {"price": "-3"}, {"price": "2.5"}]}))
    p = extract_product(html, URL)
    assert p and p.price == Decimal("2.50")


def test_json_ld_currency_symbol_in_price_currency_field():
    html = page(ld({"@type": "Product", "name": "S", "offers": {"price": 1, "priceCurrency": "грн"}}))
    p = extract_product(html, URL)
    assert p and p.currency == "UAH"


# ─── Microdata ───────────────────────────────────────────────────

MICRODATA = """
<div itemscope itemtype="http://schema.org/Product">
  <div itemprop="brand" itemscope itemtype="http://schema.org/Brand"><meta itemprop="name" content="BrandCo"></div>
  <h1 itemprop="name">Microdata   phone</h1>
  <img itemprop="image" src="https://img/m.jpg">
  <div itemprop="offers" itemscope itemtype="http://schema.org/Offer">
    <meta itemprop="priceCurrency" content="UAH">
    <span itemprop="price" content="2499.00">2 499 грн</span>
    <link itemprop="availability" href="http://schema.org/InStock">
  </div>
</div>
"""


def test_microdata_basic():
    p = extract_product(page(body=MICRODATA), URL)
    assert p is not None
    assert p.strategy == "microdata"
    assert p.title == "Microdata phone"          # не "BrandCo" із вкладеного itemscope
    assert p.price == Decimal("2499.00")
    assert p.currency == "UAH"
    assert p.is_available is True
    assert p.image_url == "https://img/m.jpg"


def test_microdata_price_from_text_and_out_of_stock():
    body = """
    <div itemscope itemtype="https://schema.org/Product">
      <span itemprop="name">T</span>
      <span itemprop="price">1 099,50 zł</span>
      <meta itemprop="availability" content="https://schema.org/OutOfStock">
    </div>"""
    p = extract_product(page(body=body), "https://shop.pl/x")
    assert p and p.price == Decimal("1099.50") and p.currency == "PLN" and p.is_available is False


# ─── OpenGraph / meta ────────────────────────────────────────────

def test_opengraph_product_price():
    head = """
    <meta property="og:title" content="OG &amp; product">
    <meta property="og:image" content="https://img/og.jpg">
    <meta property="product:price:amount" content="1299.50">
    <meta property="product:price:currency" content="UAH">
    <meta property="product:availability" content="in stock">
    """
    p = extract_product(page(head=head), URL)
    assert p is not None
    assert p.strategy == "opengraph"
    assert p.title == "OG & product"
    assert p.price == Decimal("1299.50")
    assert p.currency == "UAH"
    assert p.is_available is True
    assert p.image_url == "https://img/og.jpg"


def test_opengraph_og_price_variant_and_oos():
    head = """
    <meta property="og:title" content="X">
    <meta property="og:price:amount" content="19.99">
    <meta property="og:price:currency" content="eur">
    <meta property="og:availability" content="oos">
    """
    p = extract_product(page(head=head), URL)
    assert p and p.price == Decimal("19.99") and p.currency == "EUR" and p.is_available is False


def test_opengraph_sale_price_becomes_price_and_regular_old_price():
    head = """
    <meta property="og:title" content="Sale">
    <meta property="product:price:amount" content="100">
    <meta property="product:sale_price:amount" content="80">
    <meta property="product:price:currency" content="USD">
    """
    p = extract_product(page(head=head), URL)
    assert p and p.price == Decimal("80.00") and p.old_price == Decimal("100.00") and p.currency == "USD"


def test_opengraph_twitter_label_data_pair():
    head = """
    <meta name="twitter:title" content="Twitter product">
    <meta name="twitter:label1" content="Ціна">
    <meta name="twitter:data1" content="3 499 грн">
    """
    p = extract_product(page(head=head), URL)
    assert p and p.strategy == "opengraph" and p.title == "Twitter product"
    assert p.price == Decimal("3499.00") and p.currency == "UAH"


def test_opengraph_twitter_label_not_price_is_ignored():
    head = """
    <meta name="twitter:title" content="T">
    <meta name="twitter:label1" content="Brand"><meta name="twitter:data1" content="12345">
    """
    assert extract_product(page(head=head), URL) is None


def test_meta_itemprop_price_and_name_price():
    head = '<meta property="og:title" content="A"><meta itemprop="price" content="55">'
    p = extract_product(page(head=head), "https://shop.com/x")
    assert p and p.strategy == "opengraph" and p.price == Decimal("55.00") and p.currency == "USD"

    head = '<meta property="og:title" content="B"><meta name="price" content="66 zł">'
    p = extract_product(page(head=head), URL)
    assert p and p.price == Decimal("66.00") and p.currency == "PLN"


def test_opengraph_title_only_is_not_a_product():
    assert extract_product(page(head='<meta property="og:title" content="Just a page">'), URL) is None


# ─── Heuristic ───────────────────────────────────────────────────

def test_heuristic_h1_and_price_class():
    body = """
    <h1>  Heuristic
       product </h1>
    <div class="product-price"><span class="price-old">1 599 грн</span><span class="price-current">1 299 грн</span></div>
    """
    p = extract_product(page(body=body), URL)
    assert p is not None
    assert p.strategy == "heuristic"
    assert p.title == "Heuristic product"
    assert p.price == Decimal("1299.00")
    assert p.currency == "UAH"


def test_heuristic_id_price_and_currency_from_parent():
    body = '<h1>H</h1><div class="buy">Ціна: <span id="productPrice">12 999</span> ₴</div>'
    p = extract_product(page(body=body), "https://shop.com/x")
    assert p and p.price == Decimal("12999.00") and p.currency == "UAH"


def test_heuristic_currency_by_tld():
    body = '<h1>H</h1><span class="price">49,99</span>'
    assert extract_product(page(body=body), "https://shop.pl/x").currency == "PLN"
    assert extract_product(page(body=body), "https://shop.de/x").currency == "EUR"
    assert extract_product(page(body=body), "https://shop.co.uk/x").currency == "GBP"


def test_heuristic_skips_script_and_needs_title():
    body = '<script class="price">{"price": 5}</script><span class="price">7</span>'
    assert extract_product(page(body=body), URL) is None           # ні h1, ні title
    html = f"<html><head><title>Titled</title></head><body>{body}</body></html>"
    p = extract_product(html, URL)
    assert p and p.title == "Titled" and p.price == Decimal("7.00")


def test_heuristic_only_when_structured_gave_no_price():
    body = ld({"@type": "Product", "name": "LD name", "image": "https://img/x.jpg"}) + \
        '<h1>H1 name</h1><span class="price">777 грн</span>'
    p = extract_product(page(body=body), URL)
    assert p and p.strategy == "heuristic" and p.title == "LD name" and p.price == Decimal("777.00")


def test_heuristic_not_used_when_json_ld_has_price():
    body = ld({"@type": "Product", "name": "LD", "offers": {"price": 1}}) + '<h1>H</h1><span class="price">999</span>'
    p = extract_product(page(body=body), URL)
    assert p and p.strategy == "json-ld" and p.price == Decimal("1.00")


def test_empty_and_garbage_html():
    assert extract_product("", URL) is None
    assert extract_product("<html><body><p>nothing</p></body></html>", URL) is None


def test_clean_title_limits_length():
    assert len(clean_title("x" * 2000)) == 512
    assert clean_title("  a \n\n b&amp;c ") == "a b&c"


# ─── Реальні сторінки з tests/fixtures/*.html ────────────────────

def _fixture_pages() -> list[Path]:
    if not FIXTURES_DIR.is_dir():
        return []
    # *.blocked.html / *.needs_js.html — сторінки-заглушки для тестів бот-воллів, тут не потрібні
    return sorted(
        p for p in FIXTURES_DIR.glob("*.html")
        if not p.name.endswith((".blocked.html", ".needs_js.html"))
    )


@pytest.mark.parametrize("path", _fixture_pages() or [None], ids=lambda p: p.name if p else "no-fixtures")
def test_real_fixture_pages(path: Path | None):
    if path is None:
        pytest.skip("tests/fixtures/ має ще жодної сторінки")
    domain = path.stem
    html = path.read_text(encoding="utf-8", errors="replace")
    sidecar = path.with_suffix(".json")
    expected = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
    product = extract_product(html, expected.get("url") or f"https://{domain}/")
    assert product is not None, f"{domain}: нічого не витягли"
    assert product.title, f"{domain}: порожня назва"
    assert product.price is not None and product.price > 0, f"{domain}: немає ціни"

    if expected:
        if expected.get("expected_strategy"):
            assert product.strategy == expected["expected_strategy"]
        if expected.get("expected_currency"):
            assert product.currency == expected["expected_currency"].upper()
