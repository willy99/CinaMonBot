"""
Violity (violity.com) — аукціон колекційних речей. Сторінка лота:

    h1.page-title                       — назва
    #current_price .current span        — поточна ціна (найвища ставка), грн: "451 153"
    [data-bids-value]                   — кількість ставок
    #bid_ending .finish_time span       — закінчення торгів, київський час: "24.10.2026, 21:59:00"
    #blitz_form[data-buy-value]         — «Купити зараз» (0 — немає)
    #finished                           — блок результату: у активного лота style="display:none";
                                          видимий — «Лот проданий (N ставок)» або «Торги завершені» (не продано)

Універсальний парсер тут помиляється: бере з метатегів ціну, перераховану в USD,
а не ставку в гривнях, яку бачить покупець. Тому — окремий розбір.
Торги завершені (блок видимий або час минув) → is_available=False; lot_outcome — "sold" / "unsold"
за текстом видимого блоку, None — торги тривають або сторінка ще не показала результат.
"""
from __future__ import annotations

import re
from datetime import datetime
from html import unescape
from urllib.parse import urlparse

from bs4 import BeautifulSoup

STRATEGY = "violity-lot"
LOT_ID_RE = re.compile(r"/(?:ua|ru|en|pl)/(\d{6,})")
_END_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4}),\s*(\d{2}):(\d{2})(?::(\d{2}))?")


def lot_id(url: str) -> str | None:
    m = LOT_ID_RE.search(urlparse(url).path or "")
    return m.group(1) if m else None


def parse_end_time(text: str | None) -> datetime | None:
    """'24.10.2026, 21:59:00' (Київ) → aware datetime."""
    from app.timeutil import KYIV
    m = _END_RE.search(text or "")
    if not m:
        return None
    d, mo, y, h, mi, s = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=KYIV)
    except ValueError:
        return None


def _text(el) -> str:
    return unescape(el.get_text(" ", strip=True)) if el else ""


def extract_lot(html: str, url: str, now: datetime | None = None) -> dict | None:
    """Поля лота або None, якщо це не сторінка лота."""
    from app.services.scrapers.extract import parse_price_text
    soup = BeautifulSoup(html, "lxml")
    price_el = soup.select_one("#current_price .current span")
    title_el = soup.select_one("h1.page-title")
    if price_el is None or title_el is None:
        return None
    price = parse_price_text(_text(price_el))
    bids_el = soup.select_one("[data-bids-value]")
    end = parse_end_time(_text(soup.select_one("#bid_ending .finish_time")))
    finished = soup.select_one("#finished")
    finished_visible = finished is not None and "display:none" not in (finished.get("style") or "").replace(" ", "")
    if now is None:
        from app.timeutil import now_kyiv
        now = now_kyiv()
    ended = finished_visible or (end is not None and end <= now)
    outcome = None
    if finished_visible:
        verdict = _text(finished.select_one(".box_title")).lower()
        outcome = "sold" if "продан" in verdict and "не продан" not in verdict else "unsold"
    blitz = soup.select_one("#blitz_form")
    buy_now = None
    if blitz is not None and (blitz.get("data-buy-value") or "0").strip() not in ("", "0"):
        buy_now = parse_price_text(blitz.get("data-buy-value"))
    og_image = soup.find("meta", property="og:image")
    return {
        "title": re.sub(r"\s+", " ", _text(title_el))[:512],
        "price": price,
        "currency": "UAH",
        "bids": int(_text(bids_el)) if bids_el and _text(bids_el).isdigit() else None,
        "ends_at": end,
        "ended": ended,
        "outcome": outcome,
        "buy_now": buy_now,
        "image_url": og_image.get("content") if og_image else None,
    }


def extract_violity(html: str, url: str, now: datetime | None = None):
    from app.services.scrapers.scraper import ProductData
    lot = extract_lot(html, url, now)
    if lot is None or lot["price"] is None:
        return None
    return ProductData(
        title=lot["title"], price=lot["price"], old_price=None, currency="UAH",
        is_available=not lot["ended"], image_url=lot["image_url"], canonical_url=url, strategy=STRATEGY,
        ends_at=lot["ends_at"], lot_outcome=lot["outcome"],
    )
