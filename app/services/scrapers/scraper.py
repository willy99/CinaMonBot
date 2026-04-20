"""
Self-healing scraper.

Архітектура:
1. Селектори зберігаються в JSON файлі (selector_store.json) — не в коді
2. При парсингу використовуємо селектори з файлу
3. Якщо парсинг падає — викликаємо Claude API щоб знайти нові
4. Нові селектори зберігаємо і використовуємо далі
5. Тобі приходить алерт в Telegram

selector_store.json формат:
{
  "rozetka.com.ua": {
    "title": ["h1.title__font", "h1[class*=title]"],
    "price": ["p.product-price__big"],
    "old_price": ["p.product-price__small"],
    "availability_negative": ["немає в наявності"],
    "updated_at": "2026-04-20T18:00:00",
    "updated_by": "manual"
  }
}
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
import structlog
from bs4 import BeautifulSoup, Tag

from app.config import settings

logger = structlog.get_logger(__name__)

SELECTOR_STORE_PATH = Path("selector_store.json")
HTML_SAMPLE_SIZE = 15_000


# ─── Result types ────────────────────────────────────────────────

@dataclass(frozen=True)
class ProductData:
    title: str
    price: Decimal | None
    old_price: Decimal | None
    currency: str
    is_available: bool
    image_url: str | None
    canonical_url: str


@dataclass(frozen=True)
class ScrapeError:
    url: str
    reason: str


ScrapeResult = ProductData | ScrapeError


# ─── Selector Store ──────────────────────────────────────────────

class SelectorStore:
    """
    Зберігає CSS-селектори в JSON файлі.
    Завантажує дефолти при першому старті.
    """

    DEFAULTS: dict[str, dict] = {
        "rozetka.com.ua": {
            "title": [
                "h1.title__font",
                "h1[class*=title]",
                "h1",
            ],
            "price": [
                "p.product-price__big",
                "p[class*=product-price__big]",
                "[itemprop=price]",
            ],
            "old_price": [
                "p.product-price__small",
                "p[class*=product-price__small]",
            ],
            "availability_negative": [
                "немає в наявності",
                "закінчився",
                "недоступний",
            ],
            "updated_by": "default",
        },
        "olx.ua": {
            "json_ld": True,
            "title_meta": "og:title",
            "price_meta": "product:price:amount",
            "updated_by": "default",
        },
        "prom.ua": {
            "title": ["h1[data-qaid=product_name]"],
            "price": ["[data-qaid=product_price]"],
            "updated_by": "default",
        },
    }

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if SELECTOR_STORE_PATH.exists():
            try:
                self._store = json.loads(SELECTOR_STORE_PATH.read_text())
                logger.info("selectors_loaded", path=str(SELECTOR_STORE_PATH))
                return
            except Exception as e:
                logger.warning("selectors_load_failed", error=str(e))

        self._store = {
            k: {**v, "updated_at": datetime.now(timezone.utc).isoformat()}
            for k, v in self.DEFAULTS.items()
        }
        self._save()

    def _save(self) -> None:
        SELECTOR_STORE_PATH.write_text(
            json.dumps(self._store, ensure_ascii=False, indent=2)
        )

    def get(self, domain: str) -> dict:
        return self._store.get(domain, {})

    def update(self, domain: str, selectors: dict, updated_by: str = "claude-auto") -> None:
        selectors["updated_at"] = datetime.now(timezone.utc).isoformat()
        selectors["updated_by"] = updated_by
        self._store[domain] = selectors
        self._save()
        logger.info("selectors_updated", domain=domain, updated_by=updated_by)


_selector_store = SelectorStore()


# ─── Claude Auto-Healer ──────────────────────────────────────────

class ClaudeAutoHealer:
    """
    Коли парсер не знаходить дані — надсилає HTML в Claude
    і отримує нові CSS-селектори.
    """

    SYSTEM_PROMPT = (
        "Ти експерт з веб-скрапінгу. Тобі дають HTML фрагмент сторінки товару. "
        "Знайди CSS-селектори для назви, ціни і старої ціни. "
        "Відповідай ТІЛЬКИ валідним JSON без жодних пояснень і markdown."
    )

    USER_PROMPT = """\
Домен: {domain}

HTML:
{html}

Поверни JSON:
{{
  "title": ["css-селектор-1", "css-селектор-2"],
  "price": ["css-селектор-1"],
  "old_price": ["css-селектор-1"],
  "availability_negative": ["текст що означає відсутність товару"],
  "confidence": 0.95
}}

Правила:
- title зазвичай h1 з class що містить "title"
- price — поточна (нижча) ціна, old_price — перекреслена вища
- Використовуй конкретні класи: "h1.title__font", "[data-qaid=price]"
- confidence: 0.9+ = знайшов впевнено, <0.7 = не впевнений
- old_price може бути порожнім масивом якщо знижки немає\
"""

    async def find_selectors(self, domain: str, html: str) -> dict | None:
        try:
            html_sample = self._extract_relevant_html(html)

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": settings.ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "claude-sonnet-4-20250514",
                        "max_tokens": 800,
                        "system": self.SYSTEM_PROMPT,
                        "messages": [{
                            "role": "user",
                            "content": self.USER_PROMPT.format(
                                domain=domain,
                                html=html_sample,
                            ),
                        }],
                    },
                )
                resp.raise_for_status()

            raw = resp.json()["content"][0]["text"].strip()
            raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
            selectors = json.loads(raw)

            confidence = float(selectors.pop("confidence", 1.0))
            if confidence < 0.7:
                logger.warning("claude_low_confidence", domain=domain, confidence=confidence)
                return None

            logger.info("claude_found_selectors", domain=domain, confidence=confidence)
            return selectors

        except Exception as e:
            logger.error("claude_healer_failed", error=str(e), domain=domain)
            return None

    def _extract_relevant_html(self, html: str) -> str:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all(["script", "style", "nav", "footer", "head"]):
            tag.decompose()
        main = (
            soup.find("main")
            or soup.find(class_=re.compile(r"product|item|good", re.I))
            or soup.find("body")
        )
        return str(main or soup)[:HTML_SAMPLE_SIZE]


_auto_healer = ClaudeAutoHealer()


# ─── Helpers ─────────────────────────────────────────────────────

def _parse_price(text: str) -> Decimal | None:
    cleaned = re.sub(r"[^\d]", "", text)
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _find_by_selectors(soup: BeautifulSoup, selectors: list[str]) -> Tag | None:
    for selector in selectors:
        try:
            tag = soup.select_one(selector)
            if tag:
                return tag
        except Exception:
            continue
    return None


# ─── Rate limiter ────────────────────────────────────────────────

class DomainRateLimiter:
    def __init__(self, delay: float = settings.SCRAPE_DELAY_SECONDS):
        self._delay = delay
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, domain: str) -> asyncio.Lock:
        if domain not in self._locks:
            self._locks[domain] = asyncio.Lock()
        return self._locks[domain]

    async def acquire(self, domain: str) -> None:
        async with self._lock(domain):
            now = time.monotonic()
            wait = self._delay - (now - self._last.get(domain, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[domain] = time.monotonic()


_rate_limiter = DomainRateLimiter()

# ─── HTTP client ─────────────────────────────────────────────────

_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            http2=False,
            timeout=settings.REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
            },
        )
    return _http_client


# ─── Abstract scraper ────────────────────────────────────────────

class AbstractScraper(ABC):
    domain: str

    async def scrape(self, url: str) -> ScrapeResult:
        canonical = self.canonicalize_url(url)

        for attempt in range(1, settings.SCRAPE_MAX_RETRIES + 1):
            try:
                await _rate_limiter.acquire(self.domain)
                response = await get_http_client().get(canonical)
                response.raise_for_status()

                result = self._parse(response.text, canonical)

                # Якщо не вдалось розпарсити — запускаємо self-heal
                if isinstance(result, ScrapeError) and result.reason == "parse_failed":
                    logger.warning("parse_failed_healing", domain=self.domain, url=canonical)
                    healed = await self._try_heal(response.text, canonical)
                    if healed:
                        return healed

                if not isinstance(result, ScrapeError):
                    logger.info("scrape_success", domain=self.domain, attempt=attempt)
                return result

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return ScrapeError(url=canonical, reason="not_found")
                if e.response.status_code == 429:
                    await asyncio.sleep(30 * attempt)
                    continue
                logger.warning("http_error", status=e.response.status_code)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                logger.warning("network_error", error=str(e), attempt=attempt)
            except Exception as e:
                logger.error("scrape_error", error=str(e), attempt=attempt)

            if attempt < settings.SCRAPE_MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)

        return ScrapeError(url=canonical, reason="max_retries_exceeded")

    async def _try_heal(self, html: str, url: str) -> ScrapeResult | None:
        """Просить Claude знайти нові селектори і пробує парсити ще раз."""
        new_selectors = await _auto_healer.find_selectors(self.domain, html)
        if not new_selectors:
            return None
        _selector_store.update(self.domain, new_selectors, updated_by="claude-auto")
        result = self._parse(html, url)
        return result if not isinstance(result, ScrapeError) else None

    @abstractmethod
    def _parse(self, html: str, url: str) -> ScrapeResult:
        ...

    @abstractmethod
    def canonicalize_url(self, url: str) -> str:
        ...

    @classmethod
    def supports(cls, url: str) -> bool:
        return cls.domain in urlparse(url).netloc


# ─── Rozetka ─────────────────────────────────────────────────────

class RozetkaScraper(AbstractScraper):
    domain = "rozetka.com.ua"

    def canonicalize_url(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    def _parse(self, html: str, url: str) -> ScrapeResult:
        soup = BeautifulSoup(html, "lxml")

        # Спроба 1: JSON-LD — найстабільніший метод (стандарт schema.org)
        result = self._parse_json_ld(soup, url)
        if result and isinstance(result, ProductData) and result.title:
            return result

        # Спроба 2: CSS-селектори з динамічного store
        sel = _selector_store.get(self.domain)
        title_tag = _find_by_selectors(soup, sel.get("title", []))
        if not title_tag:
            return ScrapeError(url=url, reason="parse_failed")

        price_tag = _find_by_selectors(soup, sel.get("price", []))
        old_tag = _find_by_selectors(soup, sel.get("old_price", []))

        price = _parse_price(price_tag.get_text()) if price_tag else None
        old_price = _parse_price(old_tag.get_text()) if old_tag else None

        negative = sel.get("availability_negative", [])
        out_of_stock = any(w.lower() in html.lower() for w in negative)

        return ProductData(
            title=title_tag.get_text(strip=True),
            price=price,
            old_price=old_price,
            currency="UAH",
            is_available=not out_of_stock and price is not None,
            image_url=None,
            canonical_url=url,
        )

    def _parse_json_ld(self, soup: BeautifulSoup, url: str) -> ScrapeResult | None:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if data.get("@type") != "Product":
                    continue
                offers = data.get("offers", {})
                price_raw = offers.get("price") or offers.get("lowPrice")
                image = data.get("image")
                return ProductData(
                    title=data.get("name", ""),
                    price=Decimal(str(price_raw)) if price_raw else None,
                    old_price=None,
                    currency=offers.get("priceCurrency", "UAH"),
                    is_available="InStock" in offers.get("availability", ""),
                    image_url=image[0] if isinstance(image, list) else image,
                    canonical_url=url,
                )
            except Exception:
                continue
        return None


# ─── OLX ─────────────────────────────────────────────────────────

class OLXScraper(AbstractScraper):
    domain = "olx.ua"

    def canonicalize_url(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    def _parse(self, html: str, url: str) -> ScrapeResult:
        soup = BeautifulSoup(html, "lxml")

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if data.get("@type") != "Product":
                    continue
                offers = data.get("offers", {})
                price_raw = offers.get("price")
                return ProductData(
                    title=data.get("name", ""),
                    price=Decimal(str(price_raw)) if price_raw else None,
                    old_price=None,
                    currency=offers.get("priceCurrency", "UAH"),
                    is_available="InStock" in offers.get("availability", ""),
                    image_url=None,
                    canonical_url=url,
                )
            except Exception:
                continue

        sel = _selector_store.get(self.domain)
        title_meta = soup.find("meta", property=sel.get("title_meta", "og:title"))
        price_meta = soup.find("meta", property=sel.get("price_meta", "product:price:amount"))
        title = title_meta.get("content", "") if title_meta else ""
        price = _parse_price(str(price_meta.get("content", ""))) if price_meta else None

        if not title:
            return ScrapeError(url=url, reason="parse_failed")

        return ProductData(
            title=str(title),
            price=price,
            old_price=None,
            currency="UAH",
            is_available=price is not None,
            image_url=None,
            canonical_url=url,
        )


# ─── Prom ────────────────────────────────────────────────────────

class PromScraper(AbstractScraper):
    domain = "prom.ua"

    def canonicalize_url(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    def _parse(self, html: str, url: str) -> ScrapeResult:
        soup = BeautifulSoup(html, "lxml")
        sel = _selector_store.get(self.domain)
        title_tag = _find_by_selectors(soup, sel.get("title", []))
        if not title_tag:
            return ScrapeError(url=url, reason="parse_failed")
        price_tag = _find_by_selectors(soup, sel.get("price", []))
        return ProductData(
            title=title_tag.get_text(strip=True),
            price=_parse_price(price_tag.get_text()) if price_tag else None,
            old_price=None,
            currency="UAH",
            is_available=price_tag is not None,
            image_url=None,
            canonical_url=url,
        )


# ─── Factory ─────────────────────────────────────────────────────

_SCRAPERS: list[type[AbstractScraper]] = [
    RozetkaScraper,
    OLXScraper,
    PromScraper,
]


class ScraperFactory:
    @staticmethod
    def get(url: str) -> AbstractScraper | None:
        for cls in _SCRAPERS:
            if cls.supports(url):
                return cls()
        return None

    @staticmethod
    def supported_domains() -> list[str]:
        return [cls.domain for cls in _SCRAPERS]