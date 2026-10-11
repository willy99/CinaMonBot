"""
Універсальний екстрактор даних товару зі сторінки магазину.

Чисті функції, без мережі і без CSS-селекторів під конкретний сайт:
беремо структуровані дані, які магазини й так віддають для Google/Facebook.

Порядок стратегій (від найнадійнішої до найслабшої):
    1. JSON-LD      — <script type="application/ld+json"> з schema.org Product
    2. Microdata    — елементи з itemtype="…schema.org/Product" і itemprop=…
    3. OpenGraph    — og:title, product:price:amount, twitter:data1 тощо
    4. Евристика    — <h1> + перший елемент, у class/id якого є "price"

Перша стратегія, що дала ціну, перемагає. Якщо ціни ніде немає, повертаємо
результат JSON-LD/microdata без ціни (це явно сторінка товару) — OpenGraph
без ціни не рахується, бо og:title є на будь-якій сторінці.

Публічні функції:
    extract_product(html, url)        → ProductData | None
    parse_price_text("1 299,99 грн")  → Decimal("1299.99") | None
    detect_currency(text, url)        → "UAH" | "USD" | …
    parse_availability(value)         → True | False | None
"""
from __future__ import annotations

import html as html_lib
import json
import re
from collections.abc import Iterable, Iterator
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

if TYPE_CHECKING:  # уникаємо циклічного імпорту scraper ↔ extract
    from app.services.scrapers.scraper import ProductData

STRATEGY_JSON_LD = "json-ld"
STRATEGY_MICRODATA = "microdata"
STRATEGY_OPENGRAPH = "opengraph"
STRATEGY_HEURISTIC = "heuristic"

MAX_TITLE_LEN = 512
DEFAULT_CURRENCY = "UAH"
# Евристика не дивиться більше ніж стільки "цінових" елементів — захист від велетенських сторінок.
MAX_PRICE_CANDIDATES = 300

# Типи schema.org, які вважаємо "товаром".
_PRODUCT_TYPES = {"product", "individualproduct", "productmodel", "productgroup", "someproducts"}

# Усі "просторові" символи Unicode, які трапляються в цінах, → звичайний пробіл.
_SPACE_CHARS = "        　​﻿"
_SPACE_TABLE = {ord(c): " " for c in _SPACE_CHARS}

# Перший грошовий токен: цифри з можливими пробілами/крапками/комами/апострофами всередині.
_MONEY_TOKEN_RE = re.compile(r"\d(?:[\d .,'’]*\d)?")

# Валюта: спочатку ISO-коди (окреме слово), потім символи/слова.
_CURRENCY_CODES = (
    "UAH", "USD", "EUR", "GBP", "PLN", "CZK", "TRY", "KZT", "RUB", "BYN", "GEL", "MDL", "RON", "HUF",
    "CHF", "SEK", "NOK", "DKK", "CAD", "AUD", "JPY", "CNY", "INR", "ILS", "AED",
)
_CURRENCY_CODE_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(_CURRENCY_CODES) + r")(?![A-Za-z])")
_CURRENCY_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"₴|грн|гривн", re.I), "UAH"),
    (re.compile(r"€|евро|євро", re.I), "EUR"),
    (re.compile(r"£", re.I), "GBP"),
    (re.compile(r"zł|złoty", re.I), "PLN"),
    (re.compile(r"Kč", re.I), "CZK"),
    (re.compile(r"₺|\bTL\b"), "TRY"),
    (re.compile(r"₸|тенге|\bтг\b", re.I), "KZT"),
    (re.compile(r"\$|долл|\bдол\b", re.I), "USD"),
]
_TLD_CURRENCY: list[tuple[str, str]] = [
    (".co.uk", "GBP"), (".org.uk", "GBP"), (".uk", "GBP"),
    (".com.ua", "UAH"), (".ua", "UAH"),
    (".pl", "PLN"), (".cz", "CZK"), (".tr", "TRY"), (".kz", "KZT"),
    (".de", "EUR"), (".fr", "EUR"), (".it", "EUR"), (".es", "EUR"), (".nl", "EUR"), (".at", "EUR"),
    (".be", "EUR"), (".ie", "EUR"), (".pt", "EUR"), (".fi", "EUR"), (".sk", "EUR"), (".lt", "EUR"),
    (".lv", "EUR"), (".ee", "EUR"),
    (".com", "USD"), (".us", "USD"),
]
# Символи, які інколи кладуть у поле priceCurrency замість ISO-коду.
_CURRENCY_ALIASES = {
    "₴": "UAH", "ГРН": "UAH", "ГРН.": "UAH", "$": "USD", "€": "EUR", "£": "GBP",
    "ZŁ": "PLN", "KČ": "CZK", "₺": "TRY", "₸": "KZT",
}

_AVAILABLE_TOKENS = {
    "instock", "limitedavailability", "onlineonly", "instoreonly", "preorder", "presale",
    "backorder", "available", "availablefororder", "true", "yes", "1",
}
_UNAVAILABLE_TOKENS = {"outofstock", "soldout", "discontinued", "oos", "unavailable", "notavailable", "false", "no", "0"}

_PRICE_LABEL_RE = re.compile(r"price|ціна|цена|cena|preis|prix|prezzo|precio|prijs|fiyat|бағасы", re.I)
_PRICE_CLASS_RE = re.compile(r"price|ціна|цена|cena", re.I)
_OLD_PRICE_CLASS_RE = re.compile(r"old|strike|cross|was[-_]|prev|compare", re.I)

_SKIP_TAGS = {"script", "style", "template", "noscript", "svg", "head", "meta", "link", "input", "option", "select"}


# ─── Ціна ────────────────────────────────────────────────────────

def parse_price_text(text: Any) -> Decimal | None:
    """
    Витягує перше число-ціну з довільного тексту.

        "1 299,99 грн" → 1299.99     "1,299.00" → 1299.00     "12.99" → 12.99
        "1.299,00 €"   → 1299.00     "1 299"    → 1299        "12,99" → 12.99
        "€12.99 - €19.99" → 12.99    "од 1 299 грн" → 1299    "₴ 999" → 999

    Правило: якщо є обидва роздільники — останній десятковий. Якщо один:
    2 цифри після нього → десятковий, 3 → тисячний, повторюється → тисячний.
    Повертає None, якщо числа немає або ціна ≤ 0. Приймає також int/float/Decimal.
    """
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float, Decimal)):
        return _finalize_price(_decimal_from_number(text))

    s = str(text).translate(_SPACE_TABLE)
    for m in _MONEY_TOKEN_RE.finditer(s):
        if m.start() > 0 and s[m.start() - 1] in "-−":  # "-5" — від'ємне, не ціна
            continue
        value = _parse_money_token(m.group(0))
        if value is not None:
            return _finalize_price(value)
    return None


def _decimal_from_number(n: Any) -> Decimal | None:
    try:
        return Decimal(str(n))
    except (InvalidOperation, ValueError):
        return None


def _merge_space_groups(token: str) -> str:
    """
    "1 299" → "1299", "1 299,99" → "1299,99", "12 999 000" → "12999000",
    "12 3" → "12" (друга група не з 3 цифр — це вже не тисячі).
    """
    groups = token.split(" ")
    merged = groups[0]
    if not re.fullmatch(r"\d{1,3}", merged):
        return merged
    for g in groups[1:]:
        m = re.fullmatch(r"\d{3}([.,]\d+)?", g)
        if not m:
            break
        merged += g
        if m.group(1):  # після десяткової частини тисяч уже не буває
            break
    return merged


def _parse_money_token(token: str) -> Decimal | None:
    token = token.strip(" .,'’")
    if not token:
        return None
    if " " in token:
        token = _merge_space_groups(token)
    token = token.replace("'", "").replace("’", "")

    has_dot = "." in token
    has_comma = "," in token
    if has_dot and has_comma:
        last = max(token.rfind("."), token.rfind(","))
        int_part = re.sub(r"[.,]", "", token[:last])
        frac_part = token[last + 1:]
        normalized = f"{int_part}.{frac_part}" if frac_part.isdigit() else int_part
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        if token.count(sep) > 1:
            normalized = token.replace(sep, "")          # "1.299.000"
        else:
            head, tail = token.split(sep)
            if len(tail) == 3:
                normalized = head + tail                  # "1,299" / "12.999"
            else:
                normalized = f"{head}.{tail}"             # "12,99" / "12.5"
    else:
        normalized = token

    if not re.fullmatch(r"\d+(\.\d+)?", normalized):
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _finalize_price(value: Decimal | None) -> Decimal | None:
    if value is None or not value.is_finite():
        return None
    try:
        value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    return value if value > 0 else None


def coerce_price(value: Any) -> Decimal | None:
    """Ціна з JSON (число, рядок, список або об'єкт із price/value) → Decimal або None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return _finalize_price(_decimal_from_number(value))
    if isinstance(value, (list, tuple)):
        for item in value:
            price = coerce_price(item)
            if price is not None:
                return price
        return None
    if isinstance(value, dict):
        for key in ("price", "value", "lowPrice", "@value"):
            if key in value:
                price = coerce_price(value[key])
                if price is not None:
                    return price
        return None
    return parse_price_text(str(value))


# ─── Валюта ──────────────────────────────────────────────────────

def _currency_from_text(text: str | None) -> str | None:
    if not text:
        return None
    s = str(text)
    m = _CURRENCY_CODE_RE.search(s)
    if m:
        return m.group(1)
    for pattern, code in _CURRENCY_HINTS:
        if pattern.search(s):
            return code
    return None


def currency_for_url(url: str) -> str:
    """Валюта за доменною зоною: .ua→UAH, .pl→PLN, .de→EUR, .co.uk→GBP, .com→USD…"""
    host = (urlparse(url).hostname or "").lower() if url else ""
    for suffix, code in _TLD_CURRENCY:
        if host.endswith(suffix):
            return code
    return DEFAULT_CURRENCY


def detect_currency(text: str | None, url: str = "") -> str:
    """
    Валюта за символом/словом у тексті (₴/грн→UAH, $→USD, €→EUR, £→GBP, zł→PLN,
    Kč→CZK, ₺→TRY, ₸→KZT або ISO-код), інакше за доменною зоною URL,
    інакше DEFAULT_CURRENCY. Завжди повертає ISO-код у верхньому регістрі.
    """
    return _currency_from_text(text) or currency_for_url(url)


def normalize_currency(value: Any, url: str = "", fallback_text: str = "") -> str:
    """ISO-код із будь-якого значення priceCurrency (може бути "₴", "грн", "uah", dict)."""
    if isinstance(value, dict):
        value = value.get("@value") or value.get("name") or value.get("value")
    if isinstance(value, list):
        value = value[0] if value else None
    if value:
        s = str(value).strip()
        if s.upper() in _CURRENCY_ALIASES:
            return _CURRENCY_ALIASES[s.upper()]
        if re.fullmatch(r"[A-Za-z]{3}", s):
            return s.upper()
        found = _currency_from_text(s)
        if found:
            return found
    return detect_currency(fallback_text, url)


# ─── Наявність ───────────────────────────────────────────────────

def parse_availability(value: Any) -> bool | None:
    """
    schema.org / OpenGraph значення наявності → True/False, None якщо незрозуміло.
    Розуміє "https://schema.org/InStock", "http://schema.org/InStock", "schema.org/InStock",
    "InStock", "in stock", "out of stock", "oos", "PreOrder", "SoldOut" тощо.
    PreOrder/BackOrder/LimitedAvailability вважаємо доступними (товар можна замовити).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        value = value.get("@id") or value.get("name") or value.get("@value")
    if isinstance(value, list):
        for item in value:
            parsed = parse_availability(item)
            if parsed is not None:
                return parsed
        return None
    raw = str(value).strip()
    if not raw:
        return None
    s = raw.lower().rstrip("/").rsplit("/", 1)[-1].split(":")[-1]
    token = re.sub(r"[^a-z0-9]", "", s)
    if token in _AVAILABLE_TOKENS:
        return True
    if token in _UNAVAILABLE_TOKENS:
        return False
    # Людські тексти: "В наявності", "Немає в наявності", "Нет в наличии"
    low = raw.lower()
    if re.search(r"нема[єе]|закінчив|відсутн|нет в налич|not available|out of stock|sold out", low):
        return False
    if re.search(r"в наявності|в наличии|in stock|available", low):
        return True
    return None


# ─── Назва, картинка ─────────────────────────────────────────────

def clean_title(value: Any) -> str:
    """Рядок без HTML-сутностей, зайвих пробілів, не довший за MAX_TITLE_LEN."""
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("@value") or value.get("name") or ""
    if isinstance(value, list):
        value = value[0] if value else ""
    s = html_lib.unescape(str(value))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:MAX_TITLE_LEN]


def _image_url(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        v = value.strip()
        return v or None
    if isinstance(value, list):
        for item in value:
            u = _image_url(item)
            if u:
                return u
        return None
    if isinstance(value, dict):
        return _image_url(value.get("url") or value.get("contentUrl") or value.get("@id"))
    return None


def _attr_text(tag: Tag) -> str:
    """class + id елемента одним рядком (bs4 віддає class списком)."""
    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = [classes]
    return " ".join([*classes, str(tag.get("id") or "")])


# ─── JSON-LD ─────────────────────────────────────────────────────

def load_json_ld_blocks(soup: BeautifulSoup) -> list[Any]:
    """Усі розпарсені JSON-LD блоки сторінки (толерантно до сміття в JSON)."""
    blocks: list[Any] = []
    for script in soup.find_all("script"):
        stype = str(script.get("type") or "").lower()
        if "ld+json" not in stype:
            continue
        raw = script.string if script.string is not None else script.get_text()
        if not raw or not raw.strip():
            continue
        data = _tolerant_json_loads(raw)
        if data is not None:
            blocks.append(data)
    return blocks


def _tolerant_json_loads(raw: str) -> Any:
    try:
        return json.loads(raw, strict=False)
    except (ValueError, TypeError):
        pass
    cleaned = raw.strip()
    cleaned = re.sub(r"^\s*<!--|-->\s*$", "", cleaned)               # обгортка HTML-коментарем
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.S)         # коментарі всередині
    cleaned = re.sub(r"^\s*//<!\[CDATA\[|//\]\]>\s*$", "", cleaned)
    cleaned = re.sub(r"<!\[CDATA\[|\]\]>", "", cleaned)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.S)          # /* … */
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)                 # trailing commas
    cleaned = cleaned.strip().rstrip(";")
    try:
        return json.loads(cleaned, strict=False)
    except (ValueError, TypeError):
        pass
    # Останній шанс: беремо від першої { до останньої }
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(cleaned[start:end + 1], strict=False)
        except (ValueError, TypeError):
            return None
    return None


def _types_of(node: dict) -> set[str]:
    t = node.get("@type") or node.get("type")
    if t is None:
        return set()
    items = t if isinstance(t, list) else [t]
    out: set[str] = set()
    for item in items:
        if isinstance(item, str):
            out.add(item.rsplit("/", 1)[-1].rsplit(":", 1)[-1].lower())
    return out


def _is_product(node: dict) -> bool:
    return bool(_types_of(node) & _PRODUCT_TYPES)


def _walk(node: Any, depth: int = 0) -> Iterator[dict]:
    """Обходить JSON-LD (разом із @graph, списками, вкладеними об'єктами) і віддає всі dict."""
    if depth > 8:
        return
    if isinstance(node, list):
        for item in node:
            yield from _walk(item, depth + 1)
    elif isinstance(node, dict):
        yield node
        for key, value in node.items():
            # Варіанти й оффери обробляє _product_from_node, решту "декоративних" гілок не чіпаємо
            if key in ("offers", "hasVariant", "isVariantOf", "review", "aggregateRating", "brand", "image"):
                continue
            if isinstance(value, (dict, list)):
                yield from _walk(value, depth + 1)


def _product_nodes(blocks: Iterable[Any]) -> list[dict]:
    seen: list[dict] = []
    for block in blocks:
        for node in _walk(block):
            if _is_product(node) and not any(node is s for s in seen):
                seen.append(node)
    return seen


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _flatten_offers(value: Any, depth: int = 0) -> list[dict]:
    """offers може бути dict, list, AggregateOffer з вкладеним offers — повертаємо плоский список."""
    if depth > 4 or not value:
        return []
    if isinstance(value, list):
        out: list[dict] = []
        for item in value:
            out.extend(_flatten_offers(item, depth + 1))
        return out
    if isinstance(value, dict):
        out = [value]
        if value.get("offers"):
            out.extend(_flatten_offers(value["offers"], depth + 1))
        return out
    return []


def _offer_price(offer: dict) -> tuple[Decimal | None, Any]:
    """(ціна, сире значення priceCurrency) з Offer/AggregateOffer/priceSpecification."""
    currency = offer.get("priceCurrency")
    price = coerce_price(offer.get("price"))
    if price is None:
        price = coerce_price(offer.get("lowPrice"))
    if price is None:
        for spec in _as_list(offer.get("priceSpecification")):
            if not isinstance(spec, dict):
                continue
            if "listprice" in str(spec.get("priceType", "")).lower():
                continue
            price = coerce_price(spec.get("price"))
            if price is None:
                price = coerce_price(spec.get("minPrice"))
            if price is not None:
                currency = currency or spec.get("priceCurrency")
                break
    if currency is None:
        for spec in _as_list(offer.get("priceSpecification")):
            if isinstance(spec, dict) and spec.get("priceCurrency"):
                currency = spec["priceCurrency"]
                break
    return price, currency


def _offer_old_price(offer: dict, price: Decimal | None) -> Decimal | None:
    """Стара ціна, якщо магазин поклав її в priceSpecification з priceType=ListPrice."""
    for spec in _as_list(offer.get("priceSpecification")):
        if not isinstance(spec, dict):
            continue
        ptype = str(spec.get("priceType", "")).lower()
        if "listprice" in ptype or "strikethrough" in ptype:
            old = coerce_price(spec.get("price"))
            if old is not None and (price is None or old > price):
                return old
    return None


def _url_key(u: str) -> str:
    p = urlparse(u.strip())
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host + p.path.rstrip("/")


def _same_page(candidate: Any, url: str) -> bool:
    if not isinstance(candidate, str) or not candidate or not url:
        return False
    return _url_key(candidate) == _url_key(url)


def _pick_offer(offers: list[dict], url: str) -> dict | None:
    """Оффер, чий url збігається зі сторінкою; інакше перший із ціною; інакше перший."""
    priced = [(o, _offer_price(o)[0]) for o in offers]
    for offer, price in priced:
        if price is not None and _same_page(offer.get("url"), url):
            return offer
    for offer, price in priced:
        if price is not None:
            return offer
    return offers[0] if offers else None


def _matches_page(node: dict, url: str) -> bool:
    if _same_page(node.get("url"), url) or _same_page(node.get("@id"), url):
        return True
    return any(_same_page(o.get("url"), url) for o in _flatten_offers(node.get("offers")))


def _product_from_node(node: dict, url: str) -> dict | None:
    """Збирає сирі поля товару з JSON-LD-вузла (Product або ProductGroup з hasVariant)."""
    candidates: list[dict] = []
    variants = [v for v in _as_list(node.get("hasVariant")) if isinstance(v, dict)]
    if variants:
        # Варіант, що відповідає сторінці, — наперед; решта в початковому порядку
        variants.sort(key=lambda v: 0 if _matches_page(v, url) else 1)
        candidates.extend(variants)
    candidates.append(node)

    best: dict | None = None
    for cand in candidates:
        name = clean_title(cand.get("name") or node.get("name"))
        if not name:
            continue
        offers = _flatten_offers(cand.get("offers"))
        offer = _pick_offer(offers, url)
        price, currency_raw = _offer_price(offer) if offer else (None, None)
        data = {
            "title": name,
            "price": price,
            "old_price": _offer_old_price(offer, price) if offer else None,
            "currency_raw": currency_raw,
            "availability": parse_availability(offer.get("availability")) if offer else None,
            "image": _image_url(cand.get("image") or node.get("image")),
        }
        if price is not None:
            return data
        if best is None:
            best = data
    return best


def extract_json_ld(soup: BeautifulSoup, url: str) -> dict | None:
    """Сирі поля товару з JSON-LD або None. Вузол із ціною має пріоритет."""
    blocks = load_json_ld_blocks(soup)
    if not blocks:
        return None
    best: dict | None = None
    for node in _product_nodes(blocks):
        data = _product_from_node(node, url)
        if data is None:
            continue
        if data["price"] is not None:
            return data
        best = best or data
    return best


# ─── Microdata ───────────────────────────────────────────────────

_MICRODATA_PRODUCT_RE = re.compile(r"schema\.org/(Product|IndividualProduct|ProductGroup|ProductModel)\s*$", re.I)


def _itemprop_value(tag: Tag) -> str:
    for attr in ("content", "href", "src", "datetime", "value"):
        v = tag.get(attr)
        if v:
            return str(v).strip()
    return tag.get_text(" ", strip=True)


def _props(scope: Tag, prop: str, *, own_scope_only: bool = False) -> list[Tag]:
    """
    Елементи з itemprop=prop усередині scope. own_scope_only=True відкидає ті,
    що лежать у вкладеному itemscope (щоб name бренду не став назвою товару).
    """
    out: list[Tag] = []
    for tag in scope.find_all(attrs={"itemprop": True}):
        if prop not in str(tag.get("itemprop") or "").split():
            continue
        if own_scope_only:
            nested = False
            for parent in tag.parents:
                if parent is scope:
                    break
                if isinstance(parent, Tag) and parent.has_attr("itemscope"):
                    nested = True
                    break
            if nested:
                continue
        out.append(tag)
    return out


def extract_microdata(soup: BeautifulSoup, url: str) -> dict | None:
    """Сирі поля товару з microdata (itemtype=schema.org/Product) або None."""
    best: dict | None = None
    for scope in soup.find_all(attrs={"itemtype": _MICRODATA_PRODUCT_RE}):
        if not isinstance(scope, Tag):
            continue
        names = _props(scope, "name", own_scope_only=True) or _props(scope, "name")
        title = clean_title(_itemprop_value(names[0])) if names else ""
        if not title:
            continue

        price: Decimal | None = None
        price_text = ""
        for prop in ("price", "lowPrice"):
            for tag in _props(scope, prop):
                text = _itemprop_value(tag)
                price = parse_price_text(text)
                if price is not None:
                    price_text = text
                    break
            if price is not None:
                break

        cur = _props(scope, "priceCurrency")
        avail = _props(scope, "availability")
        img = _props(scope, "image")
        data = {
            "title": title,
            "price": price,
            "old_price": None,
            "currency_raw": _itemprop_value(cur[0]) if cur else None,
            "currency_text": price_text,
            "availability": parse_availability(_itemprop_value(avail[0])) if avail else None,
            "image": _image_url(_itemprop_value(img[0])) if img else None,
        }
        if price is not None:
            return data
        best = best or data
    return best


# ─── OpenGraph / meta ────────────────────────────────────────────

def _meta_content(soup: BeautifulSoup, *keys: str) -> str | None:
    """content першого <meta>, у якого property/name/itemprop збігається з одним із ключів."""
    wanted = {k.lower() for k in keys}
    for meta in soup.find_all("meta"):
        for attr in ("property", "name", "itemprop"):
            v = meta.get(attr)
            if v and str(v).strip().lower() in wanted:
                content = meta.get("content")
                if content is not None and str(content).strip():
                    return str(content).strip()
    return None


def _twitter_price(soup: BeautifulSoup) -> str | None:
    """twitter:label1="Price" + twitter:data1="1 299 грн" (і так до label3)."""
    for i in (1, 2, 3):
        label = _meta_content(soup, f"twitter:label{i}")
        if label and _PRICE_LABEL_RE.search(label):
            data = _meta_content(soup, f"twitter:data{i}")
            if data and parse_price_text(data) is not None:
                return data
    return None


def extract_opengraph(soup: BeautifulSoup, url: str) -> dict | None:
    """Сирі поля товару з OpenGraph/meta. Без ціни — None (og:title є скрізь)."""
    sale_text = _meta_content(soup, "product:sale_price:amount", "og:sale_price:amount")
    regular_text = _meta_content(soup, "product:price:amount", "og:price:amount", "product:price", "og:price")
    price_text = (
        sale_text
        or regular_text
        or _twitter_price(soup)
        or _meta_content(soup, "price", "product_price", "product:price:value")
    )
    price = parse_price_text(price_text) if price_text else None
    if price is None:
        return None

    old_price: Decimal | None = None
    if sale_text and regular_text:
        regular = parse_price_text(regular_text)
        if regular is not None and regular > price:
            old_price = regular

    title = clean_title(_meta_content(soup, "og:title", "twitter:title"))
    if not title:
        h1 = soup.find("h1")
        title = clean_title(h1.get_text(" ", strip=True)) if h1 else clean_title(soup.title.string if soup.title else "")
    if not title:
        return None

    currency_raw = _meta_content(
        soup, "product:sale_price:currency", "product:price:currency", "og:price:currency", "pricecurrency", "currency",
    )
    return {
        "title": title,
        "price": price,
        "old_price": old_price,
        "currency_raw": currency_raw,
        "currency_text": price_text,
        "availability": parse_availability(_meta_content(soup, "product:availability", "og:availability", "availability")),
        "image": _image_url(_meta_content(soup, "og:image:secure_url", "og:image", "twitter:image")),
    }


# ─── Евристика ───────────────────────────────────────────────────

def _price_candidates(soup: BeautifulSoup) -> list[Tag]:
    """Листові елементи, у class/id яких є "price" (контейнери з такими ж дітьми відкидаємо)."""
    body = soup.body or soup
    candidates: list[Tag] = []
    for tag in body.find_all(True):
        if tag.name in _SKIP_TAGS:
            continue
        if _PRICE_CLASS_RE.search(_attr_text(tag)):
            candidates.append(tag)
            if len(candidates) >= MAX_PRICE_CANDIDATES:
                break
    ids = {id(t) for t in candidates}
    return [t for t in candidates if not any(id(d) in ids for d in t.find_all(True))]


def _is_old_price_tag(tag: Tag) -> bool:
    """Перекреслена/стара ціна: <s>, <del> або class/id з old/strike/compare… у елемента чи предків."""
    for t in (tag, *tag.parents):
        if not isinstance(t, Tag) or t.name in ("body", "html", "[document]"):
            break
        if t.name in ("s", "del", "strike") or _OLD_PRICE_CLASS_RE.search(_attr_text(t)):
            return True
    return False


def extract_heuristic(soup: BeautifulSoup, url: str) -> dict | None:
    """
    Остання надія: <h1> як назва + перший елемент із "price" у class/id, текст якого
    парситься як ціна. Перекреслені ціни пропускаємо (беремо, лише якщо інших немає).
    """
    chosen: tuple[Tag, Decimal, str] | None = None
    fallback: tuple[Tag, Decimal, str] | None = None
    for tag in _price_candidates(soup):
        text = str(tag.get("content") or tag.get("data-price") or tag.get_text(" ", strip=True))
        price = parse_price_text(text)
        if price is None:
            continue
        if _is_old_price_tag(tag):
            fallback = fallback or (tag, price, text)
            continue
        chosen = (tag, price, text)
        break
    chosen = chosen or fallback
    if chosen is None:
        return None
    tag, price, text = chosen

    h1 = soup.find("h1")
    title = clean_title(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        title = clean_title(_meta_content(soup, "og:title") or (soup.title.string if soup.title else ""))
    if not title:
        return None

    currency_text = text
    if _currency_from_text(text) is None and isinstance(tag.parent, Tag):
        # Символу валюти в самому елементі немає — підглядаємо в батьківський
        currency_text = text + " " + tag.parent.get_text(" ", strip=True)[:200]

    return {
        "title": title,
        "price": price,
        "old_price": None,
        "currency_raw": None,
        "currency_text": currency_text,
        "availability": None,
        "image": _image_url(_meta_content(soup, "og:image")),
    }


# ─── Збірка ──────────────────────────────────────────────────────

def _build(data: dict, strategy: str, url: str) -> ProductData:
    from app.services.scrapers.scraper import ProductData  # lazy: уникаємо циклічного імпорту

    price: Decimal | None = data.get("price")
    currency = normalize_currency(data.get("currency_raw"), url, fallback_text=data.get("currency_text") or "")
    availability = data.get("availability")
    if availability is None:
        availability = price is not None
    return ProductData(
        title=data["title"],
        price=price,
        old_price=data.get("old_price"),
        currency=currency,
        is_available=bool(availability),
        image_url=data.get("image"),
        canonical_url=url,
        strategy=strategy,
    )


def extract_product(html: str, url: str, soup: BeautifulSoup | None = None) -> ProductData | None:
    """
    Головна точка входу: товар зі сторінки або None, якщо нічого не знайшли.

    Перша стратегія, що дала назву і ціну, перемагає. Без ціни повертаємо
    лише результат JSON-LD/microdata (явні дані товару); якщо ж ціну знайшла
    тільки евристика, назву беремо зі структурованих даних, а ціну — з неї.
    `soup` можна передати, щоб не парсити HTML двічі.
    """
    if not html or not html.strip():
        return None
    if soup is None:
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001
            soup = BeautifulSoup(html, "html.parser")

    unpriced: tuple[dict, str] | None = None
    for strategy, fn in (
        (STRATEGY_JSON_LD, extract_json_ld),
        (STRATEGY_MICRODATA, extract_microdata),
        (STRATEGY_OPENGRAPH, extract_opengraph),
    ):
        try:
            data = fn(soup, url)
        except Exception:  # noqa: BLE001 — одна зламана стратегія не має валити інші
            data = None
        if not data or not data.get("title"):
            continue
        if data.get("price") is not None:
            return _build(data, strategy, url)
        if unpriced is None and strategy != STRATEGY_OPENGRAPH:
            unpriced = (data, strategy)

    try:
        data = extract_heuristic(soup, url)
    except Exception:  # noqa: BLE001
        data = None
    if data and data.get("price") is not None:
        if unpriced is not None:
            merged = {**unpriced[0], "price": data["price"], "currency_text": data.get("currency_text", "")}
            return _build(merged, STRATEGY_HEURISTIC, url)
        return _build(data, STRATEGY_HEURISTIC, url)

    if unpriced is not None:
        return _build(unpriced[0], unpriced[1], url)
    return None
