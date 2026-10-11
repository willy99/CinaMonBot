"""
Рендеринг сторінок у headless Chromium (Playwright) для магазинів, які без JS не
віддають ціну (needs_js) або ховаються за антиботом (blocked).

Ідея: один процес Chromium на весь бот, окремий BrowserContext на домен (свій
User-Agent, локаль, часовий пояс, cookies — так челендж Cloudflare проходиться один
раз і далі живе в cookies контексту). Картинки/шрифти/медіа не вантажимо.

    from app.services.scrapers.render import get_renderer, RenderError

    result = await get_renderer().render("https://comfy.ua/ua/...html")
    result.html, result.final_url, result.status, result.elapsed

Помилки — завжди RenderError з .reason:
    render_timeout      — не вклалися в RENDER_TIMEOUT_SECONDS
    blocked             — Cloudflare «Just a moment» не пройшов за ~12 с
    render_failed       — усе інше (браузер упав, сторінка закрилась тощо)
    render_unavailable  — Playwright не встановлено або немає Chromium
                          (підклас RenderUnavailable; scraper тоді працює через httpx)

Налаштування (env або app.config.Settings; значення за замовчуванням у DEFAULTS):
    RENDER_MAX_CONCURRENCY      скільки сторінок рендеримо одночасно (2)
    RENDER_TIMEOUT_SECONDS      бюджет на одну сторінку (35)
    RENDER_FALLBACK             пробувати рендер, якщо httpx дав blocked/parse_failed (true)
    RENDER_IDLE_CONTEXT_MINUTES через скільки хвилин простою закривати контекст домену (10)
    RENDER_BROWSER_CHANNEL      "chromium" — новий headless повного Chromium (реалістичніший
                                відбиток); "" — легкий headless shell

Безпечно викликати з процесу бота (APScheduler loop): усі помилки ловляться,
Chromium стартує ліниво при першому рендері, close_renderer() — при зупинці.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

DEFAULTS: dict[str, Any] = {
    "RENDER_MAX_CONCURRENCY": 2,
    "RENDER_TIMEOUT_SECONDS": 35.0,
    "RENDER_FALLBACK": True,
    "RENDER_IDLE_CONTEXT_MINUTES": 10.0,
    "RENDER_BROWSER_CHANNEL": "chromium",
}

# Скільки чекати, поки Cloudflare/JS-челендж змінить заголовок сторінки
CHALLENGE_WAIT_SECONDS = 12.0
# Скільки чекати появи структурованих даних після domcontentloaded
DATA_WAIT_SECONDS = 10.0
# Якщо даних так і не з'явилось — ще трохи чекаємо тиші в мережі
NETWORK_IDLE_WAIT_SECONDS = 8.0
# Коротка пауза після появи даних: даємо гідратації дописати ціну
SETTLE_SECONDS = 1.0
# Після невдалого запуску браузера не пробуємо знову стільки секунд
UNAVAILABLE_RETRY_SECONDS = 600.0

VIEWPORT = {"width": 1366, "height": 768}
BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

# TLD → (locale, timezone, Accept-Language)
_LOCALES: list[tuple[str, tuple[str, str, str]]] = [
    (".ua", ("uk-UA", "Europe/Kyiv", "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7")),
    (".pl", ("pl-PL", "Europe/Warsaw", "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7")),
    (".de", ("de-DE", "Europe/Berlin", "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7")),
    (".cz", ("cs-CZ", "Europe/Prague", "cs-CZ,cs;q=0.9,en-US;q=0.8,en;q=0.7")),
    (".fr", ("fr-FR", "Europe/Paris", "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7")),
    (".nl", ("nl-NL", "Europe/Amsterdam", "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7")),
    (".se", ("sv-SE", "Europe/Stockholm", "sv-SE,sv;q=0.9,en-US;q=0.8,en;q=0.7")),
    (".co.uk", ("en-GB", "Europe/London", "en-GB,en;q=0.9")),
]
_DEFAULT_LOCALE = ("en-US", "America/New_York", "en-US,en;q=0.9")

# Сторінка-челендж: Cloudflare «Just a moment», «Verify you are human», DDoS-Guard тощо
_CHALLENGE_JS = """
() => {
  const t = (document.title || '');
  if (/just a moment|attention required|verify you are human|checking your browser|один момент|ddos-guard|please wait/i.test(t)) return true;
  return !!document.querySelector('#challenge-running, #cf-challenge-running, .cf-browser-verification, #challenge-form, #challenge-stage');
}
"""
# Дані товару вже в DOM: JSON-LD Product, microdata price > 0 або OpenGraph-ціна > 0
_HAS_DATA_JS = """
() => {
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    if (/"@type"\\s*:\\s*(\\[\\s*)?"(Product|ProductGroup|IndividualProduct|ProductModel)"/i.test(s.textContent || '')) return true;
  }
  const p = document.querySelector('[itemprop=price]');
  if (p) {
    const raw = (p.getAttribute('content') || p.textContent || '').replace(/[^\\d.,]/g, '').replace(',', '.');
    if (raw && parseFloat(raw) > 0) return true;
  }
  const m = document.querySelector('meta[property="product:price:amount"], meta[property="og:price:amount"]');
  if (m && parseFloat(m.getAttribute('content') || '0') > 0) return true;
  return false;
}
"""
_UA_TEMPLATE = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
)
_CLOSED_RE = re.compile(r"target closed|browser has been closed|context .* closed|has been closed|connection closed", re.I)
_MISSING_BROWSER_RE = re.compile(r"executable doesn't exist|playwright install|browserType\.launch: Failed", re.I)


# ─── Налаштування ────────────────────────────────────────────────

def setting(name: str, default: Any = None) -> Any:
    """
    RENDER_* з app.config.Settings (якщо поле там є), інакше з env, інакше з DEFAULTS.
    Так модуль працює і без правок config.py: достатньо змінної оточення.
    """
    if default is None:
        default = DEFAULTS.get(name)
    value = getattr(settings, name, None)
    if value is None:
        value = os.environ.get(name)
    if value is None:
        return default
    if isinstance(default, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return str(value)


def render_fallback_enabled() -> bool:
    return bool(setting("RENDER_FALLBACK"))


# ─── Типи ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RenderResult:
    html: str
    final_url: str
    status: int | None      # HTTP-статус головного документа (None — невідомо, напр. після челенджу)
    elapsed: float          # секунд на рендер
    challenged: bool = False  # чи був JS-челендж, який ми пройшли


class RenderError(Exception):
    """Будь-яка помилка рендеру; .reason — коротка причина для scrape_log."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


class RenderUnavailable(RenderError):
    """Playwright не встановлено / немає Chromium — працюємо без рендеру."""

    def __init__(self, message: str = "") -> None:
        super().__init__("render_unavailable", message)


def locale_for(host: str) -> tuple[str, str, str]:
    """(locale, timezone, Accept-Language) за TLD хоста."""
    h = (host or "").lower()
    for suffix, loc in _LOCALES:
        if h.endswith(suffix):
            return loc
    return _DEFAULT_LOCALE


def _host_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _context_key(host: str) -> str:
    """Ключ контексту: базовий домен із реєстру (щоб m./www. ділили cookies), інакше хост."""
    try:
        from app.services.scrapers.registry import platform_for
        p = platform_for(host)
        if p:
            return p.domain
    except Exception:  # noqa: BLE001
        pass
    return host


# ─── Renderer ────────────────────────────────────────────────────

class BrowserRenderer:
    """
    Один Chromium на процес, контекст на домен, семафор на кількість одночасних сторінок.
    Усе створюється ліниво в поточному event loop; якщо loop змінився (тести) — починаємо заново.
    """

    _unavailable_logged = False  # «Playwright немає» пишемо в лог один раз на процес

    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sem: asyncio.Semaphore | None = None
        self._launch_lock: asyncio.Lock | None = None
        self._contexts: dict[str, Any] = {}
        self._context_used: dict[str, float] = {}
        self._unavailable_until: float = 0.0
        self._last_render: float = 0.0
        self._idle_task: asyncio.Task | None = None
        self.pages_rendered = 0

    # ── життєвий цикл ──

    def _reset_if_loop_changed(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            # Старий loop (і все, що в ньому жило) уже мертвий — просто забуваємо посилання
            self._pw = self._browser = None
            self._contexts.clear()
            self._context_used.clear()
            self._sem = self._launch_lock = self._idle_task = None
        self._loop = loop
        if self._sem is None:
            self._sem = asyncio.Semaphore(max(1, int(setting("RENDER_MAX_CONCURRENCY"))))
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()

    @staticmethod
    def _import_playwright() -> Any:
        return importlib.import_module("playwright.async_api")

    async def _ensure_browser(self) -> Any:
        self._reset_if_loop_changed()
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        assert self._launch_lock is not None
        async with self._launch_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            now = time.monotonic()
            if now < self._unavailable_until:
                raise RenderUnavailable("browser unavailable (cooldown)")
            if "PYTEST_CURRENT_TEST" in os.environ and os.environ.get("RUN_BROWSER_TESTS") != "1":
                # Юніт-тести не мають запускати справжній Chromium (повільно, потрібна мережа):
                # поводимось так, ніби Playwright немає. Інтеграційні тести вмикають RUN_BROWSER_TESTS=1.
                raise RenderUnavailable("Chromium не запускаємо під pytest без RUN_BROWSER_TESTS=1")
            try:
                pw_api = self._import_playwright()
            except ImportError as e:
                self._mark_unavailable(f"playwright не встановлено: {e}")
                raise RenderUnavailable(str(e)) from e
            try:
                if self._pw is None:
                    self._pw = await pw_api.async_playwright().start()
                channel = setting("RENDER_BROWSER_CHANNEL") or ""
                kwargs: dict[str, Any] = {"headless": True, "args": LAUNCH_ARGS}
                if channel:
                    kwargs["channel"] = channel
                try:
                    self._browser = await self._pw.chromium.launch(**kwargs)
                except Exception as e:  # noqa: BLE001
                    if channel and not _MISSING_BROWSER_RE.search(str(e)):
                        raise
                    # Немає повного Chromium цього каналу — пробуємо headless shell за замовчуванням
                    kwargs.pop("channel", None)
                    self._browser = await self._pw.chromium.launch(**kwargs)
            except Exception as e:  # noqa: BLE001
                self._mark_unavailable(f"не вдалося запустити Chromium: {e}")
                raise RenderUnavailable(str(e)) from e
            self._contexts.clear()
            self._context_used.clear()
            self._browser.on("disconnected", lambda _b: self._on_disconnected())
            logger.info("browser_launched", version=self._browser.version, channel=channel or "headless-shell")
            return self._browser

    def _mark_unavailable(self, why: str) -> None:
        self._unavailable_until = time.monotonic() + UNAVAILABLE_RETRY_SECONDS
        if not BrowserRenderer._unavailable_logged:
            BrowserRenderer._unavailable_logged = True
            logger.warning("render_unavailable", error=why,
                           hint="venv/bin/pip install playwright && venv/bin/python -m playwright install chromium")

    def _on_disconnected(self) -> None:
        if self._browser is not None:   # інакше це ми самі закрили браузер у _close_browser
            logger.warning("browser_disconnected")
        self._browser = None
        self._contexts.clear()
        self._context_used.clear()

    async def _context_for(self, host: str) -> Any:
        browser = await self._ensure_browser()
        key = _context_key(host)
        ctx = self._contexts.get(key)
        if ctx is None:
            locale, tz, accept_language = locale_for(host)
            major = (browser.version or "124").split(".")[0]
            ctx = await browser.new_context(
                user_agent=_UA_TEMPLATE.format(major=major),
                locale=locale,
                timezone_id=tz,
                viewport=VIEWPORT,
                extra_http_headers={"Accept-Language": accept_language},
                java_script_enabled=True,
            )
            await ctx.route("**/*", self._route)
            self._contexts[key] = ctx
            logger.debug("browser_context_created", domain=key, locale=locale)
        self._context_used[key] = time.monotonic()
        return ctx

    @staticmethod
    async def _route(route: Any) -> None:
        try:
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()
        except Exception:  # noqa: BLE001 — сторінку вже закрили
            pass

    async def _sweep_idle(self) -> bool:
        """Закриває контексти, що простоюють довше RENDER_IDLE_CONTEXT_MINUTES. True — щось іще живе."""
        idle = float(setting("RENDER_IDLE_CONTEXT_MINUTES")) * 60
        now = time.monotonic()
        for key in list(self._contexts):
            if now - self._context_used.get(key, now) > idle:
                ctx = self._contexts.pop(key)
                self._context_used.pop(key, None)
                try:
                    await ctx.close()
                except Exception:  # noqa: BLE001
                    pass
                logger.debug("browser_context_closed_idle", domain=key)
        return bool(self._contexts)

    def _schedule_idle_watch(self) -> None:
        """Фонова задача: раз на idle-інтервал прибирає контексти; коли все простоює — закриває Chromium."""
        if self._idle_task is not None and not self._idle_task.done():
            return
        self._idle_task = asyncio.get_running_loop().create_task(self._idle_watch())

    async def _idle_watch(self) -> None:
        idle = max(5.0, float(setting("RENDER_IDLE_CONTEXT_MINUTES")) * 60)
        try:
            while True:
                await asyncio.sleep(idle)
                alive = await self._sweep_idle()
                if not alive and time.monotonic() - self._last_render > idle:
                    await self._close_browser()
                    return
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug("idle_watch_failed", error=str(e))

    async def _close_browser(self) -> None:
        browser, self._browser = self._browser, None
        self._contexts.clear()
        self._context_used.clear()
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
            logger.info("browser_closed")

    async def close(self) -> None:
        """Закриває все; викликати при зупинці бота. Безпечно викликати двічі."""
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        try:
            await self._close_browser()
        finally:
            pw, self._pw = self._pw, None
            if pw is not None:
                try:
                    await pw.stop()
                except Exception:  # noqa: BLE001
                    pass

    # ── рендер ──

    async def render(self, url: str, *, timeout: float | None = None) -> RenderResult:
        """Відкриває URL у Chromium і повертає HTML після того, як з'явились дані (або вийшов час)."""
        host = _host_of(url)
        if not host:
            raise RenderError("render_failed", f"bad url: {url!r}")
        budget = float(timeout if timeout is not None else setting("RENDER_TIMEOUT_SECONDS"))
        self._reset_if_loop_changed()
        assert self._sem is not None
        async with self._sem:
            self._last_render = time.monotonic()
            try:
                return await asyncio.wait_for(self._render_once(host, url, budget), timeout=budget + 10)
            except RenderError:
                raise
            except asyncio.TimeoutError as e:
                raise RenderError("render_timeout", f"hard cap {budget + 10:.0f}s") from e
            except Exception as e:  # noqa: BLE001
                if _CLOSED_RE.search(str(e)):
                    self._on_disconnected()
                raise RenderError("render_failed", f"{type(e).__name__}: {e}") from e
            finally:
                self._last_render = time.monotonic()
                if self._browser is not None:
                    self._schedule_idle_watch()

    async def _render_once(self, host: str, url: str, budget: float) -> RenderResult:
        ctx = await self._context_for(host)              # тут же стартує браузер (або RenderUnavailable)
        PwTimeout = self._import_playwright().TimeoutError
        started = time.monotonic()
        deadline = started + budget

        def remaining(cap: float) -> float:
            return max(0.5, min(cap, deadline - time.monotonic()))

        page = await ctx.new_page()
        try:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=budget * 1000)
            except PwTimeout as e:
                raise RenderError("render_timeout", f"goto > {budget:.0f}s") from e
            status = resp.status if resp is not None else None
            challenged = False

            if await page.evaluate(_CHALLENGE_JS):
                challenged = True
                logger.info("render_challenge", domain=host, status=status)
                try:
                    await page.wait_for_function(f"!({_CHALLENGE_JS})()", polling=500,
                                                 timeout=remaining(CHALLENGE_WAIT_SECONDS) * 1000)
                except PwTimeout as e:
                    raise RenderError("blocked", "challenge not solved") from e
                status = None  # після челенджу сторінка перезавантажилась; статус заглушки вже не актуальний
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=remaining(5.0) * 1000)
                except PwTimeout:
                    pass

            try:
                await page.wait_for_function(_HAS_DATA_JS, polling=500, timeout=remaining(DATA_WAIT_SECONDS) * 1000)
                await page.wait_for_timeout(min(SETTLE_SECONDS, remaining(SETTLE_SECONDS)) * 1000)
            except PwTimeout:
                try:
                    await page.wait_for_load_state("networkidle", timeout=remaining(NETWORK_IDLE_WAIT_SECONDS) * 1000)
                except PwTimeout:
                    pass

            html = await page.content()
            self.pages_rendered += 1
            elapsed = time.monotonic() - started
            logger.debug("render_done", domain=host, status=status, bytes=len(html), seconds=round(elapsed, 1))
            return RenderResult(html=html, final_url=page.url, status=status, elapsed=elapsed, challenged=challenged)
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass


_renderer: BrowserRenderer | None = None


def get_renderer() -> BrowserRenderer:
    global _renderer
    if _renderer is None:
        _renderer = BrowserRenderer()
    return _renderer


async def close_renderer() -> None:
    """Для shutdown бота: закрити Chromium, якщо він узагалі запускався."""
    global _renderer
    r, _renderer = _renderer, None
    if r is not None:
        await r.close()
