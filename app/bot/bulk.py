"""
Масове додавання товарів: кілька посилань одним повідомленням або файлом .txt/.csv.

    await bulk_add(message, user, urls)

- не більше BULK_MAX_ITEMS за раз (решту перелічуємо як «не додано: забагато»);
- посилання обробляються по черзі (rate limiter парсера й так розводить запити по сайтах);
- одне повідомлення з прогресом, яке в кінці стає підсумком — без потоку окремих повідомлень;
- для одного користувача одночасно працює лише один імпорт;
- без цілей: для таких товарів бот повідомить про будь-яке помітне зниження ціни;
- ліміт безкоштовного плану — як завжди (sync_free_limit): зайві товари на паузі.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from html import escape as html_escape
from urllib.parse import urlparse

import structlog

from app.config import settings
from app.database.models import User
from app.i18n import fmt_money, t
from app.services.limits import sync_free_limit
from app.services.scrapers.registry import platform_for
from app.services.scrapers.scraper import ScrapeError, ScraperFactory

logger = structlog.get_logger(__name__)

BULK_MAX_ITEMS = 20
PROGRESS_EVERY_SECONDS = 2.0
TITLE_MAX = 45

_RUNNING: set[int] = set()     # telegram_id користувачів, у яких зараз іде імпорт

# Причина помилки → ключ i18n для рядка в підсумку
_FAIL_KEYS = {
    "blocked": "bulk_fail_blocked",
    "needs_js": "bulk_fail_blocked",
    "not_found": "bulk_fail_not_found",
    "parse_failed": "bulk_fail_no_price",
    "forbidden_target": "bulk_fail_forbidden",
    "not_url": "bulk_fail_not_url",
    "auction_ended": "bulk_fail_auction_ended",
}


@dataclass
class BulkResult:
    added: list[tuple[str, str]] = field(default_factory=list)       # (назва, ціна)
    duplicates: list[str] = field(default_factory=list)              # назви / хости
    failed: list[tuple[str, str]] = field(default_factory=list)      # (хост, причина)
    skipped: int = 0                                                  # понад BULK_MAX_ITEMS
    paused: int = 0                                                   # на паузі через ліміт
    new_domains: list[tuple[str, str]] = field(default_factory=list)  # (домен, стратегія) поза реєстром


def _host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return url[:40]
    return host[4:] if host.startswith("www.") else host


def _short(title: str | None, fallback: str) -> str:
    title = (title or fallback).strip()
    return title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1] + "…"


async def _add_one(user: User, url: str, res: BulkResult) -> None:
    from app.bot.handlers.main import create_tracker   # уникаємо циклічного імпорту

    scraper = ScraperFactory.get(url)
    if scraper is None:
        res.failed.append((url[:40], "not_url"))
        return
    host = _host(url)
    platform = platform_for(host)
    if platform and platform.status in ("blocked", "needs_js"):
        res.failed.append((platform.name, "blocked"))
        return
    result = await scraper.scrape(url)
    if isinstance(result, ScrapeError):
        res.failed.append((platform.name if platform else host, result.reason))
        return
    domain = getattr(scraper, "domain", "") or host
    auction = {}
    if platform is not None and platform.auction:
        if not result.is_available:
            res.failed.append((_short(result.title, host), "auction_ended"))
            return
        # Масово напрямок не питаємо: без цілі, але з нагадуванням за годину до кінця торгів
        from app.bot.handlers.main import DEFAULT_REMIND_MINUTES
        ends_at = getattr(result, "ends_at", None)
        auction = {"ends_at": ends_at, "remind_before_minutes": DEFAULT_REMIND_MINUTES if ends_at else None}
    new_id = await create_tracker(
        user.id, url=url, canonical_url=result.canonical_url, source=domain, title=result.title,
        price=result.price, currency=(result.currency or "UAH"), **auction,
    )
    title = _short(result.title, host)
    if new_id is None:
        res.duplicates.append(title)
        return
    price = fmt_money(result.price, result.currency, user.lang) if result.price else t(user.lang, "price_unknown")
    res.added.append((title, price))
    if platform is None or platform.status == "experimental":
        res.new_domains.append((domain, getattr(result, "strategy", "")))


def render_summary(res: BulkResult, lang: str, free_limit: int) -> str:
    lines = [t(lang, "bulk_done_title")]
    if res.added:
        lines.append("")
        lines.append(t(lang, "bulk_added", n=len(res.added)))
        lines += [f"• {html_escape(title, quote=False)} — {price}" for title, price in res.added]
    if res.duplicates:
        lines.append("")
        lines.append(t(lang, "bulk_duplicates", n=len(res.duplicates)))
    if res.failed:
        lines.append("")
        lines.append(t(lang, "bulk_failed", n=len(res.failed)))
        lines += [f"• {html_escape(host, quote=False)} — {t(lang, _FAIL_KEYS.get(reason, 'bulk_fail_network'))}"
                  for host, reason in res.failed]
    if res.skipped:
        lines.append("")
        lines.append(t(lang, "bulk_skipped", n=res.skipped, max=BULK_MAX_ITEMS))
    if res.paused:
        lines.append("")
        lines.append(t(lang, "bulk_paused", n=res.paused, limit=free_limit))
    if res.added:
        lines.append("")
        lines.append(t(lang, "bulk_hint"))
    return "\n".join(lines)


async def _notify_admin_new_sites(bot, domains: list[tuple[str, str]]) -> None:
    """Одне повідомлення адміну на весь імпорт (а не по одному на сайт) — лише про ще не бачені домени."""
    from app.bot.handlers.main import _NEW_SITE_NOTIFIED
    fresh = [(d, s) for d, s in dict(domains).items() if d not in _NEW_SITE_NOTIFIED]
    if not fresh or not bot or not settings.ADMIN_TELEGRAM_ID:
        return
    _NEW_SITE_NOTIFIED.update(d for d, _ in fresh)
    text = "🆕 Нові сайти з масового додавання:\n" + "\n".join(f"• {d} (strategy={s or '?'})" for d, s in fresh)
    try:
        await bot.send_message(chat_id=int(settings.ADMIN_TELEGRAM_ID), text=text)
    except Exception as e:  # noqa: BLE001
        logger.warning("new_sites_notify_failed", error=str(e))


async def bulk_add(message, user: User, urls: list[str]) -> BulkResult | None:
    """Додає товари за списком посилань і показує прогрес в одному повідомленні."""
    from app.services import runtime_settings as rt

    lang = user.lang
    if user.telegram_id in _RUNNING:
        await message.answer(t(lang, "bulk_busy"))
        return None
    _RUNNING.add(user.telegram_id)
    res = BulkResult()
    try:
        batch = urls[:BULK_MAX_ITEMS]
        res.skipped = max(0, len(urls) - BULK_MAX_ITEMS)
        progress = await message.answer(t(lang, "bulk_progress", done=0, total=len(batch)))
        last_edit = time.monotonic()
        for i, url in enumerate(batch, 1):
            try:
                await _add_one(user, url, res)
            except Exception as e:  # noqa: BLE001 — одне погане посилання не зупиняє імпорт
                logger.error("bulk_item_failed", url=url[:120], error=str(e))
                res.failed.append((_host(url), "network"))
            if i < len(batch) and time.monotonic() - last_edit >= PROGRESS_EVERY_SECONDS:
                try:
                    await progress.edit_text(t(lang, "bulk_progress", done=i, total=len(batch)))
                except Exception:  # noqa: BLE001 — «message is not modified» тощо
                    pass
                last_edit = time.monotonic()

        if res.added:
            _, paused = await sync_free_limit(user.id)
            res.paused = len(paused)
        summary = render_summary(res, lang, rt.cached("FREE_TIER_MAX_ITEMS"))
        try:
            await progress.edit_text(summary, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            await message.answer(summary, parse_mode="HTML", disable_web_page_preview=True)
        await _notify_admin_new_sites(message.bot, res.new_domains)
        logger.info("bulk_add_done", user=user.telegram_id, added=len(res.added), dup=len(res.duplicates),
                    failed=len(res.failed), skipped=res.skipped, paused=res.paused)
        return res
    finally:
        _RUNNING.discard(user.telegram_id)
