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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse

import httpx
import structlog
from bs4 import BeautifulSoup, Tag, UnicodeDammit

from app.config import settings
from botkit import netguard
from app.services.scrapers import registry
from app.services.scrapers.extract import extract_product, parse_price_text
from app.services.scrapers.render import RenderError, RenderUnavailable, get_renderer, render_fallback_enabled
from app.services.scrapers.scrape_log import record_scrape

logger = structlog.get_logger(__name__)

SELECTOR_STORE_PATH = Path("selector_store.json")
HTML_SAMPLE_SIZE = 15_000
# Тіла більші за це не читаємо (сторінка товару — це сотні КБ, не мегабайти)
MAX_BODY_BYTES = 5 * 1024 * 1024
# Заголовок сторінки-заглушки антибота (Cloudflare, Akamai, капчі)
_BOT_WALL_RE = re.compile(
    r"just a moment|access denied|captcha|are you a human|attention required|robot or human|verify you are human",
    re.I,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
# HTTP-статуси, після яких (із повторами) кажемо "blocked"
_BLOCKED_STATUSES = {401, 403, 429}
# Причини httpx-шляху, після яких варто один раз спробувати headless Chromium (RENDER_FALLBACK)
_RENDER_FALLBACK_REASONS = {"blocked", "parse_failed"}
# Суфікс стратегії в scrape_log, коли дані дістали через браузер: "json-ld+render"
RENDER_SUFFIX = "+render"


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
    # Яка стратегія дала результат: "json-ld" | "microdata" | "opengraph" | "selectors" | "heuristic"
    strategy: str = ""


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

# Розбір ціни живе в extract.parse_price_text; псевдонім лишаємо для старих викликів.
_parse_price = parse_price_text


def _host_of(url: str) -> str:
    """Хост URL у нижньому регістрі без "www." ("" якщо URL без хоста)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# Маркери в тілі маленьких сторінок-заглушок (Cloudflare challenge, Akamai, Kasada,
# DataDome, F5, Vercel checkpoint, Horoshop JS-challenge). Перевіряємо лише короткі
# відповіді: справжня сторінка товару — це сотні КБ і такі слова там трапляються хіба у формах відгуків.
_BOT_WALL_BODY_RE = re.compile(
    r"enable javascript and cookies|request rejected|errors\.edgesuite\.net|kpsdk|"
    r"defaulthash\s*=|datadome|captcha-delivery\.com|_incapsula_|px-captcha|"
    r"pardon our interruption|security checkpoint|verifying your browser|"
    r"challenge-platform|cf-chl|unable to give you access|temporarily blocked|"
    r"loading deny page|a security issue was automatically identified",
    re.I,
)
_BOT_WALL_MAX_BODY = 120_000


# Маркери, які не трапляються на справжніх сторінках товару взагалі — перевіряємо на всьому документі:
# Alibaba/AliExpress "punish" капча (_____tmd_____/punish?x5secdata=...).
_BOT_WALL_STRONG_RE = re.compile(r"_____tmd_____/punish|x5secdata=", re.I)


def _is_bot_wall(html: str) -> bool:
    """200 OK, але сторінка — заглушка антибота: за <title>, сильними маркерами, а для коротких тіл — ще й за слабшими."""
    m = _TITLE_RE.search(html[:20_000])
    if m and _BOT_WALL_RE.search(m.group(1)):
        return True
    if _BOT_WALL_STRONG_RE.search(html[:600_000]):
        return True
    if len(html) <= _BOT_WALL_MAX_BODY and _BOT_WALL_BODY_RE.search(html):
        return True
    return False


def _decode_body(body: bytes, charset_hint: str | None) -> str:
    """Байти → текст: charset із заголовка, інакше <meta charset>/BOM через UnicodeDammit."""
    if charset_hint:
        try:
            return body.decode(charset_hint)
        except (LookupError, UnicodeDecodeError):
            pass
    dammit = UnicodeDammit(body, is_html=True)
    return dammit.unicode_markup or body.decode("utf-8", errors="replace")


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
            headers=BROWSER_HEADERS,
            event_hooks={"request": [netguard.httpx_request_hook]},   # SSRF: кожен запит і редирект
        )
    return _http_client


# Заголовки як у звичайного десктопного Chrome — менше шансів упертися в антибот.
BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


class _BlockedStatus(Exception):
    """401/403/429 — сайт не пускає; повторюємо, а після повторів кажемо blocked."""

    def __init__(self, status: int) -> None:
        super().__init__(f"blocked: HTTP {status}")
        self.status = status


async def _sleep(seconds: float) -> None:
    """Пауза між повторами (винесена, щоб тести могли її підмінити)."""
    await asyncio.sleep(seconds)


# ─── Abstract scraper ────────────────────────────────────────────

class AbstractScraper(ABC):
    domain: str

    async def scrape(self, url: str) -> ScrapeResult:
        """
        Завантажує і парсить сторінку. Рівно один рядок у scrape_log на виклик:
        ok=True зі стратегією (і healed, якщо допоміг Claude) або ok=False з причиною
        not_found | blocked | network | parse_failed | max_retries_exceeded.
        """
        canonical = self.canonicalize_url(url)
        started = time.monotonic()
        try:
            await netguard.check_url(canonical)
        except netguard.ForbiddenTarget:
            logger.warning("forbidden_target", domain=self.domain)
            return ScrapeError(url=canonical, reason="forbidden_target")
        try:
            result, healed = await self._scrape_with_retries(canonical)
        except Exception as e:  # noqa: BLE001 — страховка: планувальник не має падати через парсер
            logger.error("scrape_crashed", domain=self.domain, error=str(e))
            result, healed = ScrapeError(url=canonical, reason="max_retries_exceeded"), False
        duration_ms = int((time.monotonic() - started) * 1000)

        try:
            if isinstance(result, ProductData):
                await record_scrape(
                    self.domain, True, strategy=result.strategy, healed=healed, duration_ms=duration_ms,
                )
            else:
                await record_scrape(self.domain, False, reason=result.reason, duration_ms=duration_ms)
        except Exception as e:  # noqa: BLE001 — журнал ніколи не має ламати парсинг
            logger.warning("scrape_log_failed", domain=self.domain, error=str(e))
        return result

    # ── вибір шляху: httpx чи headless Chromium ──

    def _render_flagged(self) -> bool:
        """Домен у реєстрі позначено render_js=True — одразу йдемо через браузер."""
        p = registry.platform_for(self.domain)
        return bool(p and p.render_js)

    async def _scrape_with_retries(self, canonical: str) -> tuple[ScrapeResult, bool]:
        """
        (результат, чи допоміг healer).
        Домени з render_js → браузер (при недоступному Playwright — httpx як завжди).
        Решта → httpx; якщо він дав blocked/parse_failed, ввімкнено RENDER_FALLBACK і домен є в реєстрі —
        одна спроба через браузер (успіх логуємо як render_fallback_success, щоб домен можна було позначити).
        """
        flagged = self._render_flagged()
        if flagged:
            result, healed = await self._scrape_rendered(canonical)
            if not (isinstance(result, ScrapeError) and result.reason == "render_unavailable"):
                return result, healed
            logger.info("render_unavailable_fallback_http", domain=self.domain)

        result, healed, render_eligible = await self._scrape_http(canonical)
        # Браузер (JS чужої сторінки на нашій машині) — лише для доменів із реєстру: сторінку з довільного
        # посилання користувача в Chromium не відкриваємо (аудит 2026-10-13, N4)
        known = registry.platform_for(self.domain) is not None
        if not flagged and known and render_eligible and render_fallback_enabled() and self._worth_rendering(result):
            http_reason = result.reason if isinstance(result, ScrapeError) else "no_price"
            rendered, r_healed = await self._scrape_rendered(canonical)
            if isinstance(rendered, ProductData) and (rendered.price is not None or isinstance(result, ScrapeError)):
                logger.warning(
                    "render_fallback_success", domain=self.domain, url=canonical, strategy=rendered.strategy,
                    has_price=rendered.price is not None, http_reason=http_reason,
                    hint="позначити домен render_js=True у registry.py",
                )
                return rendered, r_healed
            if isinstance(rendered, ScrapeError) and rendered.reason != "render_unavailable":
                logger.info("render_fallback_failed", domain=self.domain, http_reason=http_reason,
                            render_reason=rendered.reason)
        return result, healed

    @staticmethod
    def _worth_rendering(result: ScrapeResult) -> bool:
        """blocked / parse_failed, або товар знайдено, але без ціни (needs_js: ціну домальовує JS)."""
        if isinstance(result, ScrapeError):
            return result.reason in _RENDER_FALLBACK_REASONS
        return result.price is None

    async def _scrape_http(self, canonical: str) -> tuple[ScrapeResult, bool, bool]:
        """
        (результат, чи допоміг healer, чи є сенс пробувати рендер).
        Повтори — на мережеві помилки, 5xx і 401/403/429. Рендер не має сенсу, коли
        відповідь — не HTML або завелика (ScrapeError прямо з _fetch_html).
        """
        blocked_status: int | None = None
        last_reason = "max_retries_exceeded"

        for attempt in range(1, settings.SCRAPE_MAX_RETRIES + 1):
            try:
                await _rate_limiter.acquire(self.domain)
                fetched = await self._fetch_html(canonical)
                if isinstance(fetched, ScrapeError):
                    return fetched, False, False

                if _is_bot_wall(fetched):
                    logger.warning("bot_wall", domain=self.domain, url=canonical)
                    return ScrapeError(url=canonical, reason="blocked"), False, True

                result = self._parse(fetched, canonical)

                # Якщо не вдалось розпарсити — запускаємо self-heal
                if isinstance(result, ScrapeError) and result.reason == "parse_failed":
                    logger.warning("parse_failed_healing", domain=self.domain, url=canonical)
                    healed = await self._try_heal(fetched, canonical)
                    if healed:
                        return healed, True, False

                if not isinstance(result, ScrapeError):
                    logger.info("scrape_success", domain=self.domain, attempt=attempt, strategy=result.strategy)
                return result, False, True

            except _BlockedStatus as e:
                blocked_status = e.status
                logger.warning("http_blocked", domain=self.domain, status=e.status, attempt=attempt)
                if e.status == 429 and attempt < settings.SCRAPE_MAX_RETRIES:
                    await _sleep(30 * attempt)
                    continue
            except httpx.HTTPStatusError as e:
                last_reason = "max_retries_exceeded"
                logger.warning("http_error", domain=self.domain, status=e.response.status_code, attempt=attempt)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_reason = "network"
                logger.warning("network_error", domain=self.domain, error=str(e), attempt=attempt)
            except Exception as e:  # noqa: BLE001
                last_reason = "max_retries_exceeded"
                logger.error("scrape_error", domain=self.domain, error=str(e), attempt=attempt)

            if attempt < settings.SCRAPE_MAX_RETRIES:
                await _sleep(2 ** attempt)

        if blocked_status is not None:
            return ScrapeError(url=canonical, reason="blocked"), False, True
        return ScrapeError(url=canonical, reason=last_reason), False, False

    async def _scrape_rendered(self, canonical: str) -> tuple[ScrapeResult, bool]:
        """
        Одна спроба через headless Chromium (без повторів — рендер і так до 35 с).
        Рендер рахується як запит до домену (rate limiter). Ніколи не кидає виключень:
        помилки браузера → ScrapeError(render_timeout | render_failed | blocked),
        відсутній Playwright → ScrapeError(render_unavailable) — внутрішній сигнал піти через httpx.
        """
        try:
            await _rate_limiter.acquire(self.domain)
            try:
                rendered = await get_renderer().render(canonical)
            except RenderUnavailable:
                return ScrapeError(url=canonical, reason="render_unavailable"), False
            except RenderError as e:
                logger.warning("render_error", domain=self.domain, url=canonical, reason=e.reason, error=str(e))
                return ScrapeError(url=canonical, reason=e.reason), False

            html = rendered.html
            if rendered.status in (404, 410):
                return ScrapeError(url=canonical, reason="not_found"), False
            if _is_bot_wall(html):
                logger.warning("bot_wall_rendered", domain=self.domain, url=canonical, status=rendered.status)
                return ScrapeError(url=canonical, reason="blocked"), False
            if len(html) > MAX_BODY_BYTES:
                logger.warning("body_too_large", domain=self.domain, read=len(html), rendered=True)
                return ScrapeError(url=canonical, reason="parse_failed"), False

            result = self._parse(html, canonical)
            healed = False
            if isinstance(result, ScrapeError) and result.reason == "parse_failed":
                healed_result = await self._try_heal(html, canonical)
                if healed_result:
                    result, healed = healed_result, True
            if isinstance(result, ScrapeError):
                if result.reason == "parse_failed" and rendered.status in _BLOCKED_STATUSES:
                    return ScrapeError(url=canonical, reason="blocked"), False
                return result, False

            result = replace(result, strategy=f"{result.strategy}{RENDER_SUFFIX}")
            logger.info("scrape_success", domain=self.domain, strategy=result.strategy, rendered=True,
                        challenged=rendered.challenged, seconds=round(rendered.elapsed, 1))
            return result, healed
        except Exception as e:  # noqa: BLE001 — рендер ніколи не має валити планувальник
            logger.error("render_scrape_error", domain=self.domain, url=canonical, error=str(e))
            return ScrapeError(url=canonical, reason="render_failed"), False

    async def _fetch_html(self, url: str) -> str | ScrapeError:
        """
        GET сторінки потоком: обриваємо тіла > MAX_BODY_BYTES і не-HTML.
        404/410 → not_found; 401/403/429 → _BlockedStatus (повтор); інші 4xx/5xx → HTTPStatusError.
        """
        client = get_http_client()
        async with client.stream("GET", url) as response:
            status = response.status_code
            if status in (404, 410):
                return ScrapeError(url=url, reason="not_found")
            if status in _BLOCKED_STATUSES:
                raise _BlockedStatus(status)
            response.raise_for_status()

            # Парсимо лише текст: text/html, text/plain (бувають криві сервери), xhtml/xml.
            content_type = response.headers.get("content-type", "").lower()
            if content_type and not (content_type.startswith("text/") or "html" in content_type or "xml" in content_type):
                logger.warning("not_html", domain=self.domain, content_type=content_type)
                return ScrapeError(url=url, reason="parse_failed")

            length = response.headers.get("content-length")
            if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
                logger.warning("body_too_large", domain=self.domain, content_length=int(length))
                return ScrapeError(url=url, reason="parse_failed")

            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    logger.warning("body_too_large", domain=self.domain, read=size)
                    return ScrapeError(url=url, reason="parse_failed")
                chunks.append(chunk)

            return _decode_body(b"".join(chunks), response.charset_encoding)

    async def _try_heal(self, html: str, url: str) -> ScrapeResult | None:
        """Просить Claude знайти нові селектори і пробує парсити ще раз."""
        new_selectors = await _auto_healer.find_selectors(self.domain, html)
        if not new_selectors:
            return None
        _selector_store.update(self.domain, new_selectors, updated_by="claude-auto")
        result = self._parse(html, url)
        return result if not isinstance(result, ScrapeError) else None

    @staticmethod
    def _structured(html: str, url: str, soup: BeautifulSoup | None = None, *,
                    allow_heuristic: bool = True) -> ProductData | None:
        """Універсальний екстрактор (JSON-LD → microdata → OpenGraph → евристика), лише з ціною."""
        try:
            data = extract_product(html, url, soup=soup)
        except Exception as e:  # noqa: BLE001
            logger.warning("extract_failed", url=url, error=str(e))
            return None
        if data is None or data.price is None:
            return None
        if not allow_heuristic and data.strategy == "heuristic":
            return None
        return data

    @abstractmethod
    def _parse(self, html: str, url: str) -> ScrapeResult:
        ...

    @abstractmethod
    def canonicalize_url(self, url: str) -> str:
        ...

    @classmethod
    def supports(cls, url: str) -> bool:
        host = _host_of(url)
        return bool(host) and (host == cls.domain or host.endswith("." + cls.domain))


# ─── Rozetka ─────────────────────────────────────────────────────

class RozetkaScraper(AbstractScraper):
    domain = "rozetka.com.ua"

    def canonicalize_url(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    def _parse(self, html: str, url: str) -> ScrapeResult:
        soup = BeautifulSoup(html, "lxml")

        # Спроба 1: структуровані дані (JSON-LD/microdata/OpenGraph) — найстабільніший метод
        structured = self._structured(html, url, soup, allow_heuristic=False)
        if structured:
            return structured

        # Спроба 2: CSS-селектори з динамічного store
        sel = _selector_store.get(self.domain)
        title_tag = _find_by_selectors(soup, sel.get("title", []))
        if not title_tag:
            # Спроба 3: усе, що зміг універсальний екстрактор (включно з евристикою)
            return self._structured(html, url, soup) or ScrapeError(url=url, reason="parse_failed")

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
            strategy="selectors",
        )


# ─── OLX ─────────────────────────────────────────────────────────

class OLXScraper(AbstractScraper):
    domain = "olx.ua"

    def canonicalize_url(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    def _parse(self, html: str, url: str) -> ScrapeResult:
        soup = BeautifulSoup(html, "lxml")

        # Спроба 1: JSON-LD/microdata/OpenGraph через універсальний екстрактор
        structured = self._structured(html, url, soup, allow_heuristic=False)
        if structured:
            return structured

        # Спроба 2: meta-теги за назвами зі store
        sel = _selector_store.get(self.domain)
        title_meta = soup.find("meta", property=sel.get("title_meta", "og:title"))
        price_meta = soup.find("meta", property=sel.get("price_meta", "product:price:amount"))
        title = title_meta.get("content", "") if title_meta else ""
        price = _parse_price(str(price_meta.get("content", ""))) if price_meta else None

        if not title:
            # Спроба 3: евристика універсального екстрактора
            return self._structured(html, url, soup) or ScrapeError(url=url, reason="parse_failed")

        return ProductData(
            title=str(title),
            price=price,
            old_price=None,
            currency="UAH",
            is_available=price is not None,
            image_url=None,
            canonical_url=url,
            strategy="selectors",
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
        price_tag = _find_by_selectors(soup, sel.get("price", []))
        price = _parse_price(price_tag.get_text()) if price_tag else None
        if not title_tag or price is None:
            # Селектори не спрацювали — пробуємо структуровані дані сторінки
            structured = self._structured(html, url, soup)
            if structured:
                return structured
        if not title_tag:
            return ScrapeError(url=url, reason="parse_failed")
        return ProductData(
            title=title_tag.get_text(strip=True),
            price=price,
            old_price=None,
            currency="UAH",
            is_available=price is not None,
            image_url=None,
            canonical_url=url,
            strategy="selectors",
        )


# ─── Generic (будь-який магазин зі структурованими даними) ────────

# Параметри відстеження, які викидаємо з URL (решту лишаємо: variant/sku важливі)
_TRACKING_PARAMS = {
    "gclid", "fbclid", "yclid", "ref", "_ga", "mc_cid", "mc_eid", "srsltid", "igshid",
    "ttclid", "msclkid", "dclid", "_gl", "gbraid", "wbraid", "_openstat", "ysclid",
}
_TRACKING_PREFIXES = ("utm_", "sc_")


def _is_tracking_param(key: str) -> bool:
    k = key.lower()
    return k in _TRACKING_PARAMS or k.startswith(_TRACKING_PREFIXES)


class GenericScraper(AbstractScraper):
    """
    Парсер для будь-якого http(s) URL: JSON-LD → microdata → OpenGraph → евристика.
    Без селекторів, тому Claude-healer тут не запускаємо — одразу parse_failed.
    `domain` береться з хоста URL (без www.), тому rate limiter і статистика — по сайтах.
    """

    domain = ""

    def __init__(self, url: str = "") -> None:
        self.domain = _host_of(url)

    @classmethod
    def supports(cls, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        return parsed.scheme in ("http", "https") and "." in host     # localhost / внутрішні — netguard

    def canonicalize_url(self, url: str) -> str:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        kept = [
            part for part in parsed.query.split("&")
            if part and not _is_tracking_param(unquote(part.split("=", 1)[0]))
        ]
        return urlunparse((scheme, netloc, parsed.path or "/", parsed.params, "&".join(kept), ""))

    def _parse(self, html: str, url: str) -> ScrapeResult:
        try:
            data = extract_product(html, url)
        except Exception as e:  # noqa: BLE001
            logger.warning("extract_failed", domain=self.domain, error=str(e))
            data = None
        return data if data is not None else ScrapeError(url=url, reason="parse_failed")

    async def _try_heal(self, html: str, url: str) -> ScrapeResult | None:
        return None


# ─── Factory ─────────────────────────────────────────────────────

_SCRAPERS: list[type[AbstractScraper]] = [
    RozetkaScraper,
    OLXScraper,
    PromScraper,
]


class ScraperFactory:
    @staticmethod
    def get(url: str) -> AbstractScraper | None:
        """Спеціальний парсер для відомих сайтів, GenericScraper для будь-якого http(s) URL, None для не-URL."""
        url = (url or "").strip()
        for cls in _SCRAPERS:
            if cls.supports(url):
                return cls()
        if GenericScraper.supports(url):
            return GenericScraper(url)
        return None

    @staticmethod
    def is_specific(url: str) -> bool:
        """Чи є для URL власний (не універсальний) парсер."""
        url = (url or "").strip()
        return any(cls.supports(url) for cls in _SCRAPERS)

    @staticmethod
    def supported_domains() -> list[str]:
        return [cls.domain for cls in _SCRAPERS]