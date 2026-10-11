#!/usr/bin/env python
"""
Проба магазинів: що бачить звичайний httpx-клієнт бота на сторінці товару.

Для кожного магazину зі списку TARGETS робимо ОДИН запит до сторінки товару
(тими ж заголовками, що й бот у scraper.get_http_client()), і дивимось:
  * чи є JSON-LD з Product (у т.ч. всередині @graph / масивів / ProductGroup);
  * чи є microdata itemtype=schema.org/Product;
  * чи є OpenGraph product:price:amount / og:price:amount;
  * чи це бот-стіна (403/429, Cloudflare "Just a moment", DataDome, Akamai
    "Access Denied", капча, JS-челендж Horoshop тощо).

Результат кожної проби класифікується:
  verified     — витягнули адекватну назву І додатну ціну без JS
  needs_js     — 200, але структурованих даних/ціни немає (рендер на клієнті)
  blocked      — бот-стіна / 403 / 429 / капча
  experimental — щось є (наприклад, Product без ціни), але не все
  error        — мережева помилка (DNS, таймаут) після повторної спроби

Фікстури:
  tests/fixtures/<domain>.html + <domain>.json      — для verified
  tests/fixtures/<domain>.blocked.html / .needs_js.html — лише якщо < 200 KB
Якщо успішна сторінка > 1.5 MB, зберігаємо мінімальний HTML: <head> + всі
<script type="application/ld+json"> + елементи з itemtype*=Product.

Запуск:
  venv/bin/python scripts/probe_platforms.py                # усі магазини
  venv/bin/python scripts/probe_platforms.py --only comfy.ua,allo.ua
  venv/bin/python scripts/probe_platforms.py --accept-language "pl-PL,pl;q=0.9" --only allegro.pl
  venv/bin/python scripts/probe_platforms.py --no-fixtures --out /tmp/probe.json

Ввічливість: один запит на магазин (плюс один на сторінку-каталог, якщо треба
знайти URL товару), пауза 2 с між магазинами, одна повторна спроба через 10 с
лише при мережевих помилках.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

PROBE_DATE = "2026-10-10"

# Ті самі заголовки, що й у app/services/scrapers/scraper.py::get_http_client()
BOT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
}

TIMEOUT = 20.0
PAUSE_BETWEEN_SHOPS = 2.0
RETRY_PAUSE = 10.0
MAX_FULL_FIXTURE = 1_500_000      # байт; більше — зберігаємо скорочену версію
MAX_FAIL_FIXTURE = 200_000        # байт; блоковані/JS сторінки лише якщо менші


# ─── Цілі ────────────────────────────────────────────────────────────────────

@dataclass
class Target:
    domain: str                      # базовий домен без www (як у registry.Platform)
    name: str
    country: str                     # ISO-2 або GLOBAL
    url: str = ""                    # сторінка товару (якщо відома)
    discover: tuple[str, str] | None = None  # (сторінка каталогу, regex посилання на товар)
    aliases: tuple[str, ...] = ()
    kind: str = "shop"               # shop | platform (приклад SaaS-платформи)
    platform: str = ""               # Shopify / WooCommerce / Horoshop / PrestaShop / Magento
    comment: str = ""                # для звіту


TARGETS: list[Target] = [
    # ── Україна ──────────────────────────────────────────────────────────────
    Target("comfy.ua", "Comfy", "UA", "https://comfy.ua/ua/smartfon-apple-iphone-16-128gb-black.html"),
    Target("foxtrot.com.ua", "Foxtrot", "UA",
           "https://www.foxtrot.com.ua/uk/shop/smartfoniy-i-mobilniye-telefoniy-apple-smartfon-iphone-16-plus-128gb-blue.html"),
    Target("allo.ua", "Allo", "UA", "https://allo.ua/ua/televizory/televizor-xiaomi-tv-a-pro-43-2026.html"),
    Target("eldorado.ua", "Eldorado", "UA", "https://eldorado.ua/uk/smartfon-apple-iphone-14-pro-128gb-silver/p71419390/"),
    Target("moyo.ua", "MOYO", "UA", "https://www.moyo.ua/ua/noutbuk_acer_extensa_exo15-41_nx_elheu_002/853624.html"),
    Target("epicentrk.ua", "Епіцентр", "UA",
           "https://epicentrk.ua/ua/shop/smartfon-samsung-galaxy-a56-5g-8-256gb-awesome-graphite-sm-a566bzkceuc.html"),
    Target("citrus.ua", "Citrus", "UA",
           "https://citrus.ua/zmishuvach-dlya-bide/zmishuvach-dlya-bide-deante-silia-z-gigenichnim-dushem-titan-bqs-d34m-760004.html",
           aliases=("ctrs.com.ua",), comment="ctrs.com.ua → 301 на citrus.ua"),
    Target("brain.com.ua", "Brain", "UA",
           "https://brain.com.ua/ukr/Mobilniy_telefon_Samsung_Galaxy_A27_5G_6_128Gb_Black_SM-A276BZKBEUC-p1353675.html"),
    Target("stls.store", "Stylus", "UA",
           "https://stls.store/apple-iphone-16-pro-max-256gb-natural-titanium-mywy3-approved-vitrinnyy-obrazec-p1428052c13877.html",
           aliases=("stylus.ua",), comment="stylus.ua → 301 на stls.store"),
    Target("telemart.ua", "Telemart", "UA", "https://telemart.ua/ua/products/asus-rt-ax52/"),
    Target("kasta.ua", "Kasta", "UA", "https://kasta.ua/uk/product/18797508:33126731/"),
    Target("makeup.com.ua", "MAKEUP", "UA", "https://makeup.com.ua/ua/product/919818/"),
    Target("zakupka.com", "Zakupka", "UA",
           "https://zakupka.com/uk/p/1646193193-detskiy-muzykalnyy-telefon-elza/",
           comment="товари: zakupka.com/uk/p/<id>-slug/ (є й дзеркала на піддоменах магазинів, вони часто 404)"),
    Target("bigl.ua", "Bigl", "UA", "https://bigl.ua/ua/p2787356605-udarnyj-akkumulyatornyj-shurupovert"),
    Target("intertop.ua", "Intertop", "UA", "https://intertop.ua/uk-ua/product/sneakers-nike-9662581"),
    Target("apteka911.ua", "Аптека 911", "UA", "https://apteka911.ua/ua/shop/mukaltin-tabl-50mg-30-p12645"),
    Target("tabletki.ua", "Tabletki.ua", "UA", "https://tabletki.ua/uk/Nurofen/13347/"),
    Target("jysk.ua", "JYSK", "UA",
           "https://jysk.ua/spalnya/matraci/pruzhinni-matraci/matrats-pruzhynnyy-80x200sm-tenna-vysokyy-rp",
           comment="зниклий товар редіректить на категорію з ItemList із 12 Product — треба перевіряти, що canonical ≠ категорія"),
    Target("sportmaster.ua", "Sportmaster", "UA", "https://sportmaster.ua/",
           comment="домен не резолвиться (NXDOMAIN)"),
    Target("answear.ua", "Answear", "UA", "https://answear.ua/p/krosivky-adidas-originals-ozmillen-kolir-sribnyj-1083247"),
    Target("mobilluck.com.ua", "Mobilluck", "UA",
           "https://www.mobilluck.com.ua/katalog/mobila/samsung/samsung-G973FD_Galaxy_S10_Duos_128GB_Green-1853535.html"),
    Target("yakaboo.ua", "Yakaboo", "UA", "https://www.yakaboo.ua/ua/alhimizovani-3452388.html"),
    Target("aromateque.com.ua", "Aromateque", "UA",
           "https://aromateque.com.ua/maison-francis-kurkdjian-oud-velvet-mood-ra141681-comb-70-ml"),
    Target("deshevshe.net.ua", "Deshevshe", "UA", "https://deshevshe.net.ua/laptop-apple/apple_a1342_macbook_mc516rsa.html"),
    # ── Польща / ЄС ──────────────────────────────────────────────────────────
    Target("allegro.pl", "Allegro", "PL",
           "https://allegro.pl/produkt/smartfon-apple-iphone-15-6-gb-128-gb-5g-czarny-2c471a7b-e083-42cc-b811-faffef64ef77"),
    Target("ceneo.pl", "Ceneo", "PL", "https://www.ceneo.pl/157123293"),
    Target("x-kom.pl", "x-kom", "PL",
           "https://www.x-kom.pl/p/1203206-laptop-15-16-lenovo-ideapad-slim-3-15-i5-12450h-16gb-512-win11.html"),
    Target("mediaexpert.pl", "Media Expert", "PL",
           "https://www.mediaexpert.pl/smartfony-i-zegarki/smartfony/smartfon-apple-iphone-16-5g-black-128gb"),
    Target("morele.net", "Morele", "PL",
           "https://www.morele.net/dysk-zewnetrzny-hdd-toshiba-canvio-basics-exclusive-1tb-czarny-hdtb510mk3aa-600016107/"),
    Target("zalando.de", "Zalando", "DE",
           "https://www.zalando.de/nike-sportswear-air-force-1-07-unisex-sneaker-low-blackwhite-ni115o070-q11.html",
           aliases=("zalando.pl", "zalando.co.uk", "zalando.fr", "zalando.it", "zalando.es", "zalando.nl")),
    Target("otto.de", "OTTO", "DE", "https://www.otto.de/p/bosch-waschmaschine-serie-6-wgg256z41-10-kg-1600-u-min-2043083677/"),
    Target("mediamarkt.de", "MediaMarkt", "DE",
           "https://www.mediamarkt.de/de/product/_samsung-galaxy-s25-5g-256-gb-navy-dual-sim-2972620.html"),
    Target("saturn.de", "Saturn", "DE", "https://www.saturn.de/de/product/_apple-iphone-16-5g-128-gb-schwarz-dual-sim-2954263.html"),
    Target("idealo.de", "idealo", "DE",
           "https://www.idealo.de/preisvergleich/OffersOfProduct/204004898_-iphone-16-128gb-schwarz-apple.html",
           comment="URL товару не підтверджено (idealo блокує і пошуковий краулер)"),
    Target("alza.cz", "Alza", "CZ", "https://www.alza.cz/iphone-16-128gb-cerna-d12541617.htm", aliases=("alza.sk", "alza.de")),
    Target("bol.com", "bol", "NL", "https://www.bol.com/nl/nl/p/apple-iphone-16-128gb-wit/9300000189439845/"),
    Target("cdiscount.com", "Cdiscount", "FR",
           "https://www.cdiscount.com/telephonie/telephone-mobile/apple-iphone-16-128gb-white/f-14404-ip16128white.html"),
    Target("fnac.com", "Fnac", "FR", "https://www.fnac.com/Apple-iPhone-16-6-1-5G-128-Go-Double-SIM-Noir/a19813594/w-4"),
    Target("elgiganten.se", "Elgiganten", "SE",
           "https://www.elgiganten.se/product/mobiler-tablets-smartklockor/mobiltelefon/iphone-16-5g-smartphone-128gb-black/825094"),
    # ── США / Британія / глобальні ───────────────────────────────────────────
    Target("walmart.com", "Walmart", "US",
           "https://www.walmart.com/ip/Apple-AirPods-Pro-with-MagSafe-USB-C-Charging-Case-2nd-Gen/15191113204"),
    Target("target.com", "Target", "US",
           "https://www.target.com/p/airpods-pro-2nd-generation-with-magsafe-case-usb-c/-/A-85978622"),
    Target("bestbuy.com", "Best Buy", "US",
           "https://www.bestbuy.com/site/apple-airpods-pro-2nd-generation-with-magsafe-case-usbc-white/6447382.p?skuId=6447382"),
    Target("ebay.com", "eBay", "US", "https://www.ebay.com/itm/296110312917",
           aliases=("ebay.co.uk", "ebay.de", "ebay.pl")),
    Target("etsy.com", "Etsy", "US", "https://www.etsy.com/listing/1756455865/handmade-ceramic-mug-with-woman-line-art"),
    Target("newegg.com", "Newegg", "US",
           "https://www.newegg.com/samsung-2tb-990-pro-w-heatsink-normal-package-nvme-2-0/p/N82E16820147863"),
    Target("bhphotovideo.com", "B&H Photo", "US",
           "https://www.bhphotovideo.com/c/product/1667800-REG/sony_ilce_7m4_b_alpha_a7_iv_mirrorless.html"),
    Target("argos.co.uk", "Argos", "GB", "https://www.argos.co.uk/product/4190356"),
    Target("currys.co.uk", "Currys", "GB",
           "https://www.currys.co.uk/products/apple-refurbished-airpods-pro-2nd-generation-with-magsafe-charging-case-usbc-white-excellent-condition-10287140.html"),
    Target("johnlewis.com", "John Lewis", "GB",
           "https://www.johnlewis.com/apple-airpods-pro-3-with-magsafe-charging-case-usb-c-white/p114325329"),
    Target("asos.com", "ASOS", "GB", "https://www.asos.com/nike/nike-air-force-1-07-trainers-in-triple-black/prd/202329454"),
    Target("ikea.com", "IKEA", "GLOBAL", "https://www.ikea.com/us/en/p/raskog-utility-cart-white-30586783/"),
    Target("apple.com", "Apple", "GLOBAL", "https://www.apple.com/shop/buy-iphone/iphone-17"),
    Target("nike.com", "Nike", "GLOBAL", "https://www.nike.com/t/pegasus-premium-mens-road-running-shoes-kWXqW9yR/HQ2592-023"),
    Target("adidas.com", "adidas", "GLOBAL", "https://www.adidas.com/us/samba-og-shoes/B75806.html"),
    Target("aliexpress.com", "AliExpress", "GLOBAL", "https://www.aliexpress.com/item/1005007432415523.html",
           aliases=("aliexpress.us", "aliexpress.ru")),
    Target("amazon.com", "Amazon", "US", "https://www.amazon.com/dp/B0D1XD1ZV3",
           aliases=("amazon.de", "amazon.co.uk", "amazon.pl")),
    # ── Платформи (приклади магазинів на SaaS/движках) ───────────────────────
    Target("gymshark.com", "Gymshark (Shopify)", "GLOBAL",
           "https://www.gymshark.com/products/gymshark-adapt-x-whitney-leggings-leggings-green-aw26",
           kind="platform", platform="Shopify"),
    Target("allbirds.com", "Allbirds (Shopify)", "US", discover=("https://www.allbirds.com/collections/mens", r"/products/[a-z0-9-]+"),
           kind="platform", platform="Shopify"),
    Target("novritsch.com", "Novritsch (WooCommerce)", "GLOBAL", "https://eu.novritsch.com/product/bbs-bags/",
           aliases=("eu.novritsch.com",), kind="platform", platform="WooCommerce",
           comment="novritsch.com → 302 на eu.novritsch.com; у HTML є 'woocommerce'"),
    Target("galaxystore.com.ua", "Galaxy Store (Horoshop)", "UA",
           "https://galaxystore.com.ua/ua/wall-chargers/setevoe-zaryadnoe-ustroystvo-essager-grace-100w-2type-c-2usb-ect2ca-qyb01-z/",
           kind="platform", platform="Horoshop", comment="Horoshop без JS-челенджу (assets/cache/horoshop_default_*.js)"),
    Target("hillary.ua", "Hillary (Horoshop)", "UA", "https://hillary.ua/",
           kind="platform", platform="Horoshop", comment="Horoshop із JS-челенджем (518 байт, cookie challenge_passed=<hash зі сторінки>)"),
    Target("quality.in.ua", "quality.in.ua (Horoshop)", "UA", "https://quality.in.ua/",
           kind="platform", platform="Horoshop", comment="Horoshop із JS-челенджем; після cookie challenge_passed віддає звичайний HTML"),
    Target("bleu-de-chauffe.com", "Bleu de Chauffe (PrestaShop)", "FR",
           "https://www.bleu-de-chauffe.com/en/messenger-bag/1948-leon-messenger-bag-camel-3700716022378.html",
           kind="platform", platform="PrestaShop"),
    Target("oliverbonas.com", "Oliver Bonas (Magento 2)", "GB", "https://www.oliverbonas.com/homeware/polka-dot-bed-linen-410829",
           kind="platform", platform="Magento"),
]


# ─── Витягування структурованих даних (мінімальний самодостатній екстрактор) ─

PRODUCT_TYPES = {"product", "productgroup", "individualproduct", "productmodel", "vehicle", "car", "book", "drug"}


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    if isinstance(t, str):
        return {t.lower().rsplit("/", 1)[-1]}
    if isinstance(t, list):
        return {str(x).lower().rsplit("/", 1)[-1] for x in t}
    return set()


def _walk(obj: Any, depth: int = 0):
    """Усі dict-вузли в JSON-LD (включно з @graph, масивами, вкладеними)."""
    if depth > 12:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, depth + 1)


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    # "1 299,00" / "1,299.00" / "1299" / "12 999 грн"
    s = re.sub(r"[^\d,.  ]", "", s).replace(" ", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) <= 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _offer_price(offers: Any) -> tuple[float | None, str | None, str]:
    """Повертає (ціна, валюта, звідки) з offers (Offer / AggregateOffer / список)."""
    if offers is None:
        return None, None, ""
    if isinstance(offers, list):
        for o in offers:
            p, c, how = _offer_price(o)
            if p is not None:
                return p, c, how + "[list]"
        return None, None, ""
    if not isinstance(offers, dict):
        return None, None, ""
    cur = offers.get("priceCurrency")
    for key in ("price", "lowPrice", "highPrice"):
        p = _to_float(offers.get(key))
        if p is not None:
            return p, cur, f"offers.{key}"
    ps = offers.get("priceSpecification")
    if ps:
        if isinstance(ps, list):
            ps = ps[0] if ps else None
        if isinstance(ps, dict):
            p = _to_float(ps.get("price"))
            if p is not None:
                return p, ps.get("priceCurrency") or cur, "offers.priceSpecification.price"
    inner = offers.get("offers")
    if inner:
        return _offer_price(inner)
    return None, cur, ""


def _parse_ld_text(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    text = re.sub(r"^\s*<!--|-->\s*$", "", text).strip()
    text = text.replace("//<![CDATA[", "").replace("//]]>", "")
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass
    # Кілька JSON-об'єктів поспіль або трейлінг-коми
    try:
        return json.loads(re.sub(r",\s*([}\]])", r"\1", text), strict=False)
    except json.JSONDecodeError:
        return None


@dataclass
class Extracted:
    strategy: str = ""               # json-ld | microdata | opengraph | ""
    title: str | None = None
    price: float | None = None
    currency: str | None = None
    signals: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def extract(html: str) -> Extracted:
    soup = BeautifulSoup(html, "lxml")
    ex = Extracted()

    # ── JSON-LD ──
    ld_scripts = soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)})
    products: list[dict] = []
    parse_errors = 0
    all_types: set[str] = set()
    for s in ld_scripts:
        data = _parse_ld_text(s.string or s.get_text() or "")
        if data is None:
            parse_errors += 1
            continue
        for node in _walk(data):
            t = _types(node)
            all_types |= t
            if t & PRODUCT_TYPES:
                products.append(node)
    ex.signals["ld_scripts"] = len(ld_scripts)
    ex.signals["ld_parse_errors"] = parse_errors
    ex.signals["ld_products"] = len(products)
    ex.signals["ld_types"] = sorted(all_types)[:25]

    ld_title = ld_price = ld_cur = None
    ld_how = ""
    for i, p in enumerate(products):
        name = p.get("name")
        if isinstance(name, dict):
            name = name.get("@value")
        price, cur, how = _offer_price(p.get("offers"))
        if price is None and "productgroup" in _types(p):
            variants = p.get("hasVariant") or []
            price, cur, how = _offer_price([v.get("offers") for v in variants if isinstance(v, dict)])
            how = how and ("hasVariant." + how)
        if price is None:
            for key in ("price", "lowPrice"):
                price = _to_float(p.get(key))
                if price is not None:
                    how = key
                    cur = p.get("priceCurrency")
                    break
        if ld_title is None and name:
            ld_title = str(name).strip()
        if price is not None and ld_price is None:
            ld_price, ld_cur, ld_how = price, cur, how
            if i > 0:
                ex.notes.append(f"ціна в {i + 1}-му Product з {len(products)}")
        if ld_title and ld_price is not None:
            break
    if ld_price is not None and not ld_cur:
        ex.notes.append("JSON-LD без priceCurrency")
    ex.signals["ld_price_from"] = ld_how

    # ── microdata ──
    md_nodes = soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/(Product|IndividualProduct|ProductModel)\b", re.I)})
    ex.signals["microdata_products"] = len(md_nodes)
    md_title = md_price = md_cur = None

    def _own_prop(scope, prop: str):
        """itemprop усередині scope, але НЕ всередині вкладеного itemscope (напр. Brand.name)."""
        for e in scope.find_all(attrs={"itemprop": re.compile(rf"^{prop}$")}):
            parent_scope = e.find_parent(attrs={"itemscope": True})
            if parent_scope is scope or (parent_scope is not None and parent_scope.get("itemtype") == scope.get("itemtype")):
                return e
        return None

    for n in md_nodes:
        nm = _own_prop(n, "name")
        if nm and md_title is None:
            md_title = (nm.get("content") or nm.get_text(" ", strip=True) or "").strip() or None
        pr = n.find(attrs={"itemprop": re.compile(r"^(price|lowPrice)$")})
        if pr and md_price is None:
            md_price = _to_float(pr.get("content") or pr.get_text(" ", strip=True))
            cu = n.find(attrs={"itemprop": "priceCurrency"})
            md_cur = (cu.get("content") or cu.get_text(strip=True)) if cu else None
        if md_title and md_price is not None:
            break
    if md_price == 0:
        ex.notes.append("microdata price=0 — ціна підвантажується JS")

    # ── OpenGraph ──
    def meta(*props: str) -> str | None:
        for p in props:
            m = soup.find("meta", attrs={"property": p}) or soup.find("meta", attrs={"name": p})
            if m and m.get("content"):
                return m["content"].strip()
        return None

    og_price = _to_float(meta("product:price:amount", "og:price:amount"))
    og_cur = meta("product:price:currency", "og:price:currency")
    og_title = meta("og:title")
    ex.signals["og_type"] = meta("og:type")
    ex.signals["og_price"] = og_price

    # ── вибір стратегії: перша, що дала і назву, і ціну ──
    html_title = (soup.title.get_text(strip=True) if soup.title else None) or None
    for strat, t, p, c in (
        ("json-ld", ld_title, ld_price, ld_cur),
        ("microdata", md_title, md_price, md_cur),
        ("opengraph", og_title or html_title, og_price, og_cur),
    ):
        if p is not None and p > 0 and t:
            ex.strategy, ex.title, ex.price, ex.currency = strat, t, p, c
            break
    else:
        # Хоч щось: назва без ціни або ціна без назви
        ex.title = ld_title or md_title or og_title or html_title
        ex.price = next((p for p in (ld_price, md_price, og_price) if p is not None), None)
        ex.currency = ld_cur or md_cur or og_cur
        if ex.price is not None:
            ex.strategy = "json-ld" if ld_price is not None else ("microdata" if md_price is not None else "opengraph")
    return ex


# ─── Бот-стіни ───────────────────────────────────────────────────────────────

# (назва, regex, strong) — strong-маркери означають бот-стіну завжди; weak — лише коли
# сторінка маленька або статус не 200 (інакше слово "captcha" у формі відгуків дасть хибне спрацювання)
BOTWALL_MARKERS = [
    ("cloudflare-challenge", r"<title>just a moment|cf-chl|challenge-platform|_cf_chl_opt|enable javascript and cookies to continue", True),
    ("datadome", r"please enable js and disable any ad blocker|geo\.captcha-delivery\.com|<title>datadome", True),
    ("akamai", r"access denied.{0,80}you don't have permission to access|errors\.edgesuite\.net", True),
    ("kasada", r"window\.kpsdk\s*=|x-kpsdk-", True),
    ("f5-request-rejected", r"<title>request rejected</title>|the requested url was rejected", True),
    ("vercel-checkpoint", r"vercel security checkpoint", True),
    ("horoshop-js-challenge", r"defaultHash\s*=\s*\"[0-9a-f]{64}\"", True),
    ("maintenance-deny", r"site maintenance|loading deny page content|notre site n'est actuellement pas disponible", True),
    ("js-required", r"javascript n'est pas activ|javascript is disabled|enable javascript and then reload", False),
    ("captcha", r"<title>[^<]*captcha|verify that you'?re not a robot|robot or human|are you a human|confirm you are human|перевірка.{0,40}робот", False),
    ("perimeterx", r"px-captcha|_pxhd|human challenge", False),
]

SMALL_PAGE = 120_000  # байт; менше — ймовірно сторінка-заглушка, а не товар


def detect_botwall(status: int, html: str) -> str | None:
    head = html[:60_000].lower()
    small = len(html) < SMALL_PAGE
    for name, pat, strong in BOTWALL_MARKERS:
        if (strong or small or status != 200) and re.search(pat, head, re.I | re.S):
            return name
    if status in (401, 403, 429, 444, 503):
        return f"http-{status}"
    if status in (200, 202) and len(html.strip()) < 2_000:
        return f"http-{status}-empty-or-stub"
    return None


# ─── Проба ───────────────────────────────────────────────────────────────────

@dataclass
class ProbeResult:
    domain: str
    name: str
    country: str
    kind: str
    platform: str
    url: str
    final_url: str = ""
    http_status: int | None = None
    status: str = "error"           # verified | needs_js | blocked | experimental | error
    strategy: str = ""
    title: str | None = None
    price: float | None = None
    currency: str | None = None
    botwall: str | None = None
    size: int = 0
    server: str | None = None
    signals: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    fixture: str | None = None
    fetched_at: str = ""
    elapsed: float = 0.0


def _get(client: httpx.Client, url: str) -> httpx.Response:
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            return client.get(url)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            last = e
            if attempt == 1:
                print(f"    ! {type(e).__name__}: {e} — повтор через {RETRY_PAUSE:.0f} с", flush=True)
                time.sleep(RETRY_PAUSE)
    assert last is not None
    raise last


def discover_url(client: httpx.Client, start: str, pattern: str) -> str | None:
    r = _get(client, start)
    soup = BeautifulSoup(r.text, "lxml")
    rx = re.compile(pattern)
    for a in soup.find_all("a", href=True):
        h = urljoin(str(r.url), a["href"]).split("#")[0]
        if rx.search(h):
            return h
    return None


def minimal_fixture(html: str) -> str:
    """<head> + усі ld+json + елементи з itemtype*=Product — мінімальний валідний HTML."""
    soup = BeautifulSoup(html, "lxml")
    parts = ["<!DOCTYPE html>", "<html>"]
    if soup.head:
        parts.append(str(soup.head))
    parts.append("<body>")
    seen: set[int] = set()
    for s in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        if soup.head and s in soup.head.find_all("script"):
            continue
        parts.append(str(s))
    for n in soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/.*Product", re.I)}):
        if id(n) in seen:
            continue
        seen.add(id(n))
        parts.append(str(n))
    parts += ["</body>", "</html>"]
    return "\n".join(parts)


def probe(client: httpx.Client, t: Target, save_fixtures: bool) -> ProbeResult:
    res = ProbeResult(t.domain, t.name, t.country, t.kind, t.platform, t.url,
                      fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    t0 = time.monotonic()
    try:
        url = t.url
        if t.discover:
            url = discover_url(client, *t.discover)
            if not url:
                res.status, res.error = "error", "не знайшов посилання на товар на сторінці каталогу"
                res.url = t.discover[0]
                return res
            res.url = url
            time.sleep(1.0)
        r = _get(client, url)
    except Exception as e:  # DNS / таймаут після повтору / SSL
        res.status, res.error = "error", f"{type(e).__name__}: {e}"
        res.elapsed = time.monotonic() - t0
        return res

    html = r.text
    res.final_url = str(r.url)
    res.http_status = r.status_code
    res.size = len(r.content)
    res.server = r.headers.get("server")
    res.elapsed = time.monotonic() - t0
    res.botwall = detect_botwall(r.status_code, html)

    ex = extract(html)
    res.strategy, res.title, res.price, res.currency = ex.strategy, ex.title, ex.price, ex.currency
    res.signals, res.notes = ex.signals, list(ex.notes)
    if r.history:
        res.notes.append(f"редірект {len(r.history)}x → {urlparse(res.final_url).netloc}")

    ok = bool(ex.strategy) and ex.price is not None and ex.price > 0 and bool(ex.title)
    if ok and not res.botwall:
        res.status = "verified"
    elif ok and res.botwall:
        # Дані є попри маркер — маркер хибний (напр. слово "captcha" у формі відгуку)
        res.status = "verified"
        res.notes.append(f"маркер '{res.botwall}' проігноровано: структуровані дані є")
        res.botwall = None
    elif res.botwall or r.status_code in (401, 403, 429, 444):
        res.status = "blocked"
    elif r.status_code in (404, 410):
        res.status = "error"
        res.error = f"{r.status_code} — сторінка товару зникла; потрібен інший URL"
    elif r.status_code >= 400:
        res.status = "blocked"
        res.notes.append(f"HTTP {r.status_code} без явної бот-стіни")
    elif ex.price is not None and ex.price <= 0:
        # Розмітка є, але ціна 0 — її підвантажує JS/API (eldorado, epicentrk)
        res.status = "needs_js"
        res.notes.append("розмітка є, але price=0 — ціну підвантажує JS/API")
    elif ex.signals.get("ld_products") or ex.signals.get("microdata_products"):
        res.status = "experimental"
        res.notes.append("є Product-розмітка, але без ціни/назви")
    else:
        res.status = "needs_js"
        if ex.title and len(html) > SMALL_PAGE:
            res.notes.append("HTML великий і з назвою, але без JSON-LD/microdata/OG-ціни — потрібні селектори або JS")

    if save_fixtures:
        res.fixture = save_fixture(t, res, html)
    return res


def save_fixture(t: Target, res: ProbeResult, html: str) -> str | None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    if res.status == "verified":
        body = html if res.size <= MAX_FULL_FIXTURE else minimal_fixture(html)
        if res.size > MAX_FULL_FIXTURE:
            res.notes.append(f"фікстура скорочена ({res.size // 1024} KB → head+ld+json+microdata)")
        p = FIXTURES / f"{t.domain}.html"
        p.write_text(body, encoding="utf-8")
        (FIXTURES / f"{t.domain}.json").write_text(json.dumps({
            "url": res.final_url or res.url,
            "expected_strategy": res.strategy,
            "expected_currency": res.currency,
            "title": res.title,
            "price": res.price,
            "fetched_at": res.fetched_at,
            "status": res.http_status,
            "trimmed": res.size > MAX_FULL_FIXTURE,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return p.name
    if res.status in ("blocked", "needs_js") and 0 < res.size < MAX_FAIL_FIXTURE:
        p = FIXTURES / f"{t.domain}.{res.status}.html"
        p.write_text(html, encoding="utf-8")
        return p.name
    return None


# ─── CLI ─────────────────────────────────────────────────────────────────────

def md_table(results: list[ProbeResult]) -> str:
    rows = ["| domain | country | status | strategy | HTTP | title | price | notes |", "|---|---|---|---|---|---|---|---|"]
    for r in results:
        title = (r.title or "").replace("|", "/")[:60]
        price = f"{r.price:g} {r.currency or ''}".strip() if r.price is not None else ""
        notes = "; ".join(filter(None, [r.botwall and f"бот-стіна: {r.botwall}", r.error, *r.notes]))
        rows.append(f"| {r.domain} | {r.country} | {r.status} | {r.strategy} | {r.http_status or ''} | {title} | {price} | {notes.replace('|', '/')} |")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="кома-список доменів")
    ap.add_argument("--accept-language", help="перевизначити Accept-Language (експеримент із локаллю)")
    ap.add_argument("--no-fixtures", action="store_true")
    ap.add_argument("--out", help="куди записати JSON з результатами")
    ap.add_argument("--merge", action="store_true", help="з --only: оновити лише ці домени в існуючому --out")
    ap.add_argument("--pause", type=float, default=PAUSE_BETWEEN_SHOPS)
    args = ap.parse_args()

    targets = TARGETS
    if args.only:
        wanted = {d.strip().lower() for d in args.only.split(",")}
        targets = [t for t in TARGETS if t.domain in wanted or set(t.aliases) & wanted]
    headers = dict(BOT_HEADERS)
    if args.accept_language:
        headers["Accept-Language"] = args.accept_language

    results: list[ProbeResult] = []
    with httpx.Client(http2=False, timeout=TIMEOUT, follow_redirects=True, headers=headers) as client:
        for i, t in enumerate(targets):
            print(f"[{i + 1}/{len(targets)}] {t.domain} …", flush=True)
            r = probe(client, t, save_fixtures=not args.no_fixtures)
            results.append(r)
            price = f"{r.price:g} {r.currency or ''}" if r.price is not None else "-"
            print(f"    → {r.status:12s} {r.strategy or '-':10s} http={r.http_status} {price:>14s}  {(r.title or '')[:50]!r}"
                  f"{'  botwall=' + r.botwall if r.botwall else ''}{'  err=' + r.error if r.error else ''}", flush=True)
            if i < len(targets) - 1:
                time.sleep(args.pause)

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("\n" + md_table(results))
    print("\nПідсумок:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if args.out:
        out = [asdict(r) for r in results]
        if args.merge and Path(args.out).exists():
            old = json.loads(Path(args.out).read_text(encoding="utf-8"))
            fresh = {r["domain"]: r for r in out}
            merged = [fresh.pop(o["domain"], o) for o in old]
            # нові домени — в порядку TARGETS
            order = {t.domain: i for i, t in enumerate(TARGETS)}
            out = sorted(merged + list(fresh.values()), key=lambda r: order.get(r["domain"], 10**6))
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
