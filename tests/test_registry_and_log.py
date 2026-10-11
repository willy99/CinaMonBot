"""Реєстр платформ (registry) і журнал парсингу (scrape_log)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.models import ScrapeLog
from app.database.session import get_session
from app.services.scrapers import registry, scrape_log
from app.services.scrapers.registry import (
    PLATFORMS, Platform, platform_for, platform_name, platforms_by_country, verified_platforms,
)

VALID_STATUSES = {"verified", "experimental", "needs_js", "blocked"}


# ─── registry: пошук ─────────────────────────────────────────────

@pytest.mark.parametrize("host, expected_domain", [
    ("comfy.ua", "comfy.ua"),                  # точний збіг
    ("www.comfy.ua", "comfy.ua"),              # www
    ("WWW.Comfy.UA", "comfy.ua"),              # регістр
    ("  comfy.ua  ", "comfy.ua"),              # пробіли
    ("m.comfy.ua", "comfy.ua"),                # піддомен
    ("shop.m.comfy.ua", "comfy.ua"),           # глибокий піддомен
    ("ctrs.com.ua", "citrus.ua"),              # alias
    ("www.ctrs.com.ua", "citrus.ua"),          # alias + www
    ("zalando.pl", "zalando.de"),              # alias у списку з кількох
    ("eu.novritsch.com", "novritsch.com"),     # alias, що сам є піддоменом
    ("rozetka.com.ua", "rozetka.com.ua"),
])
def test_platform_for_matches(host, expected_domain):
    p = platform_for(host)
    assert p is not None and p.domain == expected_domain


@pytest.mark.parametrize("host", [
    "unknown-shop.example", "", None, "comfy.ua.evil.com", "notcomfy.ua", "ua",
])
def test_platform_for_unknown(host):
    assert platform_for(host) is None  # type: ignore[arg-type]


def test_platform_name():
    assert platform_name("www.comfy.ua") == "Comfy"
    assert platform_name("ctrs.com.ua") == "Citrus"
    assert platform_name("www.unknown-shop.example") == "unknown-shop.example"   # невідомий → домен без www
    assert platform_name("Unknown.EXAMPLE") == "unknown.example"


def test_verified_platforms():
    verified = verified_platforms()
    assert verified and all(p.status == "verified" for p in verified)
    assert {p.domain for p in verified} >= {"rozetka.com.ua", "olx.ua", "prom.ua"}
    assert len(verified) == sum(1 for p in PLATFORMS if p.status == "verified")


def test_platforms_by_country_covers_everything():
    grouped = platforms_by_country()
    assert sum(len(v) for v in grouped.values()) == len(PLATFORMS)
    assert set(grouped) == {p.country for p in PLATFORMS}
    assert "UA" in grouped and all(p.country == "UA" for p in grouped["UA"])


def test_registry_queries_follow_monkeypatched_list(monkeypatch):
    small = [Platform("a.test", "A", "UA", "verified"), Platform("b.test", "B", "PL", "blocked", aliases=("bb.test",))]
    monkeypatch.setattr(registry, "PLATFORMS", small)
    assert [p.name for p in verified_platforms()] == ["A"]
    assert platform_for("x.bb.test").name == "B"
    assert set(platforms_by_country()) == {"UA", "PL"}
    assert platform_for("comfy.ua") is None


# ─── registry: інваріанти даних ──────────────────────────────────

@pytest.mark.parametrize("platform", PLATFORMS, ids=lambda p: p.domain)
def test_platform_entry_is_valid(platform: Platform):
    assert platform.status in VALID_STATUSES
    assert platform.country == "GLOBAL" or (len(platform.country) == 2 and platform.country.isupper())
    assert platform.name.strip()
    assert platform.domain == platform.domain.lower().strip()
    assert not platform.domain.startswith("www.")
    assert "." in platform.domain
    for alias in platform.aliases:
        assert alias == alias.lower().strip() and not alias.startswith("www.")
        assert alias != platform.domain
    if platform.status == "verified":
        assert platform.strategy, f"{platform.domain}: verified без strategy"


def test_platform_domains_unique():
    domains = [p.domain for p in PLATFORMS]
    assert len(domains) == len(set(domains)), "дублікати доменів у реєстрі"


def test_no_alias_collides_with_another_platform():
    owners: dict[str, str] = {}
    for p in PLATFORMS:
        for d in (p.domain, *p.aliases):
            assert d not in owners, f"{d} належить і {owners[d]}, і {p.domain}"
            owners[d] = p.domain


def test_no_platform_is_subdomain_of_another():
    """Інакше platform_for() віддасть перший-ліпший запис за порядком реєстру."""
    names = [(d, p.domain) for p in PLATFORMS for d in (p.domain, *p.aliases)]
    for a, owner_a in names:
        for b, owner_b in names:
            if owner_a != owner_b:
                assert not a.endswith("." + b), f"{a} — піддомен {b}: platform_for неоднозначний"


def test_specific_scrapers_are_in_registry_as_verified():
    from app.services.scrapers.scraper import ScraperFactory
    for domain in ScraperFactory.supported_domains():
        p = platform_for(domain)
        assert p is not None and p.status == "verified", domain


# ─── scrape_log ──────────────────────────────────────────────────

async def _rows() -> list[ScrapeLog]:
    async with get_session() as s:
        return (await s.execute(select(ScrapeLog).order_by(ScrapeLog.created_at))).scalars().all()


async def test_record_scrape_ok_row(db):
    await scrape_log.record_scrape("comfy.ua", True, strategy="json-ld", duration_ms=412.7)
    rows = await _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.domain == "comfy.ua" and row.ok is True
    assert row.strategy == "json-ld" and row.reason is None
    assert row.healed is False and row.duration_ms == 412
    assert row.created_at is not None


async def test_record_scrape_failure_row_and_healed(db):
    await scrape_log.record_scrape("allo.ua", False, reason="parse_failed", duration_ms=800)
    await scrape_log.record_scrape("prom.ua", True, strategy="selectors", healed=True, duration_ms=3200)
    rows = {r.domain: r for r in await _rows()}
    assert rows["allo.ua"].ok is False and rows["allo.ua"].reason == "parse_failed" and rows["allo.ua"].strategy is None
    assert rows["prom.ua"].healed is True and rows["prom.ua"].strategy == "selectors"


async def test_record_scrape_empty_strings_become_null(db):
    await scrape_log.record_scrape("x.ua", True, strategy="", reason="")
    row = (await _rows())[0]
    assert row.strategy is None and row.reason is None


async def test_record_scrape_truncates_domain(db):
    long_domain = "a" * 300 + ".ua"
    await scrape_log.record_scrape(long_domain, True, strategy="json-ld")
    row = (await _rows())[0]
    assert len(row.domain) == 128 and row.domain == long_domain[:128]


async def test_prune_scrape_log_deletes_only_old_rows(db):
    now = datetime.utcnow()
    async with get_session() as s:
        s.add_all([
            ScrapeLog(domain="old.ua", ok=True, created_at=now - timedelta(days=40)),
            ScrapeLog(domain="edge.ua", ok=True, created_at=now - timedelta(days=29, hours=23)),
            ScrapeLog(domain="new.ua", ok=False, reason="network", created_at=now - timedelta(hours=1)),
        ])
    deleted = await scrape_log.prune_scrape_log(days=30)
    assert deleted == 1
    assert sorted(r.domain for r in await _rows()) == ["edge.ua", "new.ua"]


async def test_prune_scrape_log_custom_days_and_nothing_to_delete(db):
    now = datetime.utcnow()
    async with get_session() as s:
        s.add(ScrapeLog(domain="x.ua", ok=True, created_at=now - timedelta(days=3)))
    assert await scrape_log.prune_scrape_log(days=30) == 0
    assert await scrape_log.prune_scrape_log(days=2) == 1
    assert await _rows() == []


def _broken_session():
    @asynccontextmanager
    async def broken():
        raise RuntimeError("db is down")
        yield  # pragma: no cover
    return broken


async def test_record_scrape_swallows_write_failure(db, monkeypatch):
    monkeypatch.setattr(scrape_log, "get_session", _broken_session())
    await scrape_log.record_scrape("comfy.ua", True, strategy="json-ld")   # не має кидати
    monkeypatch.undo()
    assert await _rows() == []


async def test_prune_scrape_log_swallows_failure(db, monkeypatch):
    monkeypatch.setattr(scrape_log, "get_session", _broken_session())
    assert await scrape_log.prune_scrape_log() == 0
