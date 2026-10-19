"""
Violity (аукціон): ціна = поточна ставка в грн зі сторінки лота, завершені торги → немає в наявності.

Фікстури — реальні сторінки violity.com (19.10.2026), стиснуті до потрібних блоків:
- active.html — активний лот, 59 ставок, 451 153 грн, закінчення 24.10.2026 21:59, без «Купити зараз»;
- active-buy-now.html — активний, 16 ставок, 101 104 грн, «Купити зараз» 1 000 001 грн;
- finished.html — торги завершені 03.04.2026, 750 грн (блок «Торги завершені» видимий).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.scrapers import violity
from app.services.scrapers.registry import platform_for
from app.services.scrapers.scraper import GenericScraper, ScrapeError, ScraperFactory, ViolityScraper
from app.timeutil import KYIV

FIX = Path(__file__).parent / "fixtures" / "violity"
BEFORE_END = datetime(2026, 10, 19, 12, 0, tzinfo=KYIV)


def html(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_active_lot():
    lot = violity.extract_lot(html("active.html"), "https://violity.com/ua/124877135", now=BEFORE_END)
    assert lot["title"].startswith("Герой Соціалістичної праці")
    assert lot["price"] == Decimal("451153.00") and lot["currency"] == "UAH" and lot["bids"] == 59
    assert lot["ends_at"] == datetime(2026, 10, 24, 21, 59, tzinfo=KYIV)
    assert lot["ended"] is False and lot["buy_now"] is None


def test_active_lot_with_buy_now():
    lot = violity.extract_lot(html("active-buy-now.html"), "https://violity.com/ua/124306466", now=BEFORE_END)
    assert lot["title"] == "Breguet Marine Chronograph 5827BR/Z2/5ZU"
    assert lot["price"] == Decimal("101104.00") and lot["bids"] == 16 and lot["buy_now"] == Decimal("1000001.00")


def test_finished_lot_is_unavailable():
    p = violity.extract_violity(html("finished.html"), "https://violity.com/ua/123000000", now=BEFORE_END)
    assert p.price == Decimal("750.00") and p.is_available is False and p.strategy == "violity-lot"


def test_lot_past_end_time_counts_as_ended_even_if_block_hidden():
    after = datetime(2026, 10, 25, 0, 0, tzinfo=KYIV)
    p = violity.extract_violity(html("active.html"), "https://violity.com/ua/124877135", now=after)
    assert p.is_available is False


def test_usd_meta_price_is_not_used():
    """Метатеги сторінки дають ціну в USD — ми беремо ставку в грн."""
    p = violity.extract_violity(html("active.html"), "https://violity.com/ua/124877135", now=BEFORE_END)
    assert p.currency == "UAH" and p.price > Decimal("100000")


def test_not_a_lot_page():
    page = '<html><head><meta property="product.price.amount" content="99"></head><body><h1 class="page-title">Каталог</h1></body></html>'
    assert violity.extract_lot(page, "https://violity.com/ua/") is None
    r = ViolityScraper()._parse(page, "https://violity.com/ua/")
    assert isinstance(r, ScrapeError) and r.reason == "parse_failed"


@pytest.mark.parametrize("text, expected", [
    ("24.10.2026, 21:59:00", datetime(2026, 10, 24, 21, 59, tzinfo=KYIV)),
    ("03.04.2026, 19:48", datetime(2026, 4, 3, 19, 48, tzinfo=KYIV)),
    ("31.02.2026, 10:00:00", None), ("", None), (None, None),
])
def test_parse_end_time(text, expected):
    assert violity.parse_end_time(text) == expected


@pytest.mark.parametrize("url, canon", [
    ("https://violity.com/ua/124306466-breguet-marine?utm_source=x#bids", "https://violity.com/ua/124306466"),
    ("https://violity.com/ru/124306466-breguet", "https://violity.com/ua/124306466"),
    ("https://www.violity.com/ua/124306466", "https://violity.com/ua/124306466"),
    ("https://violity.com/ua/catalog/coins?page=2", "https://violity.com/ua/catalog/coins"),
])
def test_canonicalize(url, canon):
    assert ViolityScraper().canonicalize_url(url) == canon


def test_factory_and_registry():
    assert isinstance(ScraperFactory.get("https://violity.com/ua/124306466-x"), ViolityScraper)
    assert isinstance(ScraperFactory.get("https://www.violity.com/ua/1"), ViolityScraper)
    assert isinstance(ScraperFactory.get("https://fakeviolity.com/ua/1"), GenericScraper)
    p = platform_for("violity.com")
    assert p and p.name == "Violity" and p.status == "verified" and p.strategy == "violity-lot"
