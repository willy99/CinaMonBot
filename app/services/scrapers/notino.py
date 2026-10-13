"""
Notino (notino.ua, .pl, .de, …): ціна конкретного варіанта (об'єму/відтінку).

Проблема з універсальним парсером: JSON-LD на сторінці завжди описує варіант
за замовчуванням, навіть коли в посиланні вибрано інший (`…/p-541146/`). Для
парфумів 30 / 50 / 100 мл це різні ціни — стежили б не за тим товаром.

Notino вбудовує стан застосунку в `<script id="__APOLLO_STATE__">`, де кожен
`CatalogVariant:<id>` має власні `price`, `originalPrice`, `availability`:

    "CatalogVariant:541146": {"webId": "541146", "name": "Original",
        "additionalInfo": "0,8 mm 8 кс", "price": {"value": 180, "currency": "UAH"},
        "originalPrice": {"value": 250, ...}, "availability": {"state": "CanBeBought"}, ...}

Вибір варіанта: id з `/p-<id>/` у посиланні → інакше варіант із URL або SKU
пропозиції в JSON-LD → інакше перший варіант зі стану.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import structlog

logger = structlog.get_logger(__name__)

STRATEGY = "notino-state"
NOTINO_HOST_RE = re.compile(r"(^|\.)notino\.(ua|pl|de|cz|sk|hu|ro|bg|it|fr|es|at|ch|gr|hr|si|nl|be|dk|se|fi|pt|ie|co\.uk|lt|lv|ee)$")
_VARIANT_ID_RE = re.compile(r"/p-(\d+)/?")
_APOLLO_RE = re.compile(r'<script[^>]*id="__APOLLO_STATE__"[^>]*>(.*?)</script>', re.S)
_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def is_notino_host(host: str) -> bool:
    return bool(NOTINO_HOST_RE.search((host or "").lower()))


def variant_id_from_url(url: str) -> str | None:
    m = _VARIANT_ID_RE.search(urlparse(url).path or "")
    return m.group(1) if m else None


def load_state(html: str) -> dict[str, Any] | None:
    m = _APOLLO_RE.search(html)
    if not m:
        return None
    try:
        state = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None
    return state if isinstance(state, dict) else None


def _ld_offer_hint(html: str) -> tuple[str | None, str | None]:
    """(variant id з url пропозиції, sku) з JSON-LD Product — це варіант за замовчуванням."""
    for m in _LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except (ValueError, TypeError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict) or node.get("@type") != "Product":
                continue
            offers = node.get("offers")
            offer = (offers[0] if isinstance(offers, list) and offers else offers) or {}
            if not isinstance(offer, dict):
                continue
            vid = variant_id_from_url(str(offer.get("url") or ""))
            return vid, (offer.get("sku") or node.get("sku"))
    return None, None


def _variants(state: dict[str, Any]) -> dict[str, dict]:
    return {
        str(v.get("webId") or k.split(":", 1)[1]): v
        for k, v in state.items()
        if k.startswith("CatalogVariant:") and isinstance(v, dict)
    }


def pick_variant(state: dict[str, Any], html: str, url: str) -> dict | None:
    variants = _variants(state)
    if not variants:
        return None
    wanted = variant_id_from_url(url)
    if wanted:
        # Явно вибраний у посиланні варіант. Якщо його немає в стані — краще помилка,
        # ніж мовчки стежити за іншим об'ємом.
        return variants.get(wanted)
    ld_vid, ld_sku = _ld_offer_hint(html)
    if ld_vid and ld_vid in variants:
        return variants[ld_vid]
    if ld_sku:
        for v in variants.values():
            if v.get("productCode") == ld_sku:
                return v
    return next(iter(variants.values()))


def _money(node: Any) -> tuple[Decimal | None, str | None]:
    if not isinstance(node, dict) or node.get("value") is None:
        return None, None
    try:
        value = Decimal(str(node["value"])).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None, None
    return (value if value > 0 else None), (node.get("currency") or None)


def _resolve(state: dict[str, Any], ref: Any) -> dict:
    if isinstance(ref, dict) and "__ref" in ref:
        return state.get(ref["__ref"]) or {}
    return ref if isinstance(ref, dict) else {}


def _title(state: dict[str, Any], v: dict) -> str:
    brand = _resolve(state, v.get("brand")).get("name") or ""
    name = (v.get("name") or "").strip()
    head = name if not brand or name.lower().startswith(brand.lower()) else f"{brand} {name}"
    extra = (v.get("additionalInfo") or "").strip()
    title = f"{head} · {extra}" if extra else head
    return re.sub(r"\s+", " ", title).strip()[:512]


IMAGE_CDN = "https://cdn.notinoimg.com/order_2k/"


def _image(state: dict[str, Any], v: dict) -> str | None:
    """media.mainImage.src — відносний шлях на CDN ("tepe/…/original___140422.jpg")."""
    media = _resolve(state, v.get("media"))
    for key in ("mainImage", "previewImage", "shadeImage"):
        src = _resolve(state, media.get(key)).get("src")
        if isinstance(src, str) and src:
            return src if src.startswith("http") else IMAGE_CDN + src.lstrip("/")
    return None


def extract_notino(html: str, url: str):
    """ProductData для вибраного варіанта або None (немає стану / варіанта / ціни)."""
    from app.services.scrapers.scraper import ProductData   # уникаємо циклічного імпорту

    state = load_state(html)
    if not state:
        return None
    v = pick_variant(state, html, url)
    if not v:
        logger.warning("notino_variant_not_found", url=url, wanted=variant_id_from_url(url))
        return None
    price, currency = _money(v.get("price"))
    if price is None:
        return None
    original, _ = _money(v.get("originalPrice"))
    availability = v.get("availability") if isinstance(v.get("availability"), dict) else {}
    return ProductData(
        title=_title(state, v),
        price=price,
        old_price=original if original and original > price else None,
        currency=(currency or "UAH").upper(),
        is_available=availability.get("state") == "CanBeBought",
        image_url=_image(state, v),
        canonical_url=url,
        strategy=STRATEGY,
    )
