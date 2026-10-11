"""
Тести запитів дашборду по платформах (app.admin.queries.platforms_*).

Тимчасова SQLite-база налаштовується у tests/conftest.py.

    venv/bin/python -m pytest tests/test_platform_queries.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.admin import queries as q  # noqa: E402
from app.database.models import PriceTracker, ScrapeLog, User  # noqa: E402
from app.database.session import get_session, init_db  # noqa: E402
from app.services.scrapers import registry  # noqa: E402
from app.services.scrapers.registry import Platform  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="module")


TEST_PLATFORMS = [
    Platform("rozetka.com.ua", "Rozetka", "UA", "verified", "selectors"),
    Platform("comfy.ua", "Comfy", "UA", "verified", "json-ld", aliases=("comfy.com.ua",)),
    Platform("allo.ua", "Allo", "UA", "experimental", "json-ld"),
    Platform("amazon.com", "Amazon", "US", "blocked", ""),
    Platform("foxtrot.com.ua", "Foxtrot", "UA", "needs_js", ""),
]


def _utc(**delta) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(**delta)


@pytest_asyncio.fixture(loop_scope="module", scope="module", autouse=True)
async def seed():
    saved = list(registry.PLATFORMS)
    registry.PLATFORMS[:] = TEST_PLATFORMS
    # Спільна база з conftest: попередні модулі лишають рядки — починаємо з чистої схеми
    from app.database.models import Base
    from app.database.session import engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()

    async with get_session() as s:
        u1 = User(telegram_id=1001, first_name="A", language_code="uk")
        u2 = User(telegram_id=1002, first_name="B", language_code="en")
        s.add_all([u1, u2])
        await s.flush()

        def tr(user, source, status="active", errors=0, n=0):
            return PriceTracker(
                user_id=user.id, url=f"https://{source}/p/{n}", canonical_url=f"https://{source}/p/{n}",
                source=source, title=f"{source} #{n}", status=status, consecutive_errors=errors,
            )

        s.add_all([
            # rozetka: 3 трекери (2 активних, 1 error), 2 юзери; один — піддомен
            tr(u1, "rozetka.com.ua", n=1),
            tr(u1, "hard.rozetka.com.ua", n=2),
            tr(u2, "rozetka.com.ua", status="error", errors=5, n=3),
            # comfy: по основному домену і по аліасу, один юзер → users=1
            tr(u1, "comfy.ua", n=4),
            tr(u1, "comfy.com.ua", status="paused", n=5),
            # невідомий домен
            tr(u2, "newshop.example", n=6),
        ])

        def log(domain, ok, *, reason=None, strategy=None, healed=False, ms=100, ago_h=1.0):
            return ScrapeLog(domain=domain, ok=ok, reason=reason, strategy=strategy, healed=healed,
                             duration_ms=ms, created_at=_utc(hours=ago_h))

        s.add_all([
            # rozetka за 24 год: 4 спроби, 2 невдачі (50%), 1 healed; +1 стара (5 днів) успішна
            log("rozetka.com.ua", True, strategy="selectors", ms=200, ago_h=1),
            log("rozetka.com.ua", False, reason="parse_failed", ms=300, ago_h=2),
            log("hard.rozetka.com.ua", True, strategy="selectors", healed=True, ms=1000, ago_h=3),
            log("rozetka.com.ua", False, reason="network", ms=500, ago_h=4),
            log("rozetka.com.ua", True, strategy="json-ld", ms=100, ago_h=24 * 5),
            # comfy: аліас + основний домен об'єднуються; 3 спроби, 1 невдача → 33.3% (проблема)
            log("comfy.ua", True, strategy="json-ld", ms=400, ago_h=1),
            log("comfy.com.ua", False, reason="blocked", ms=50, ago_h=2),
            log("comfy.com.ua", True, strategy="json-ld", ms=600, ago_h=3),
            # невідомий домен: 2 спроби (< 3 → не проблема), 1 невдача
            log("newshop.example", True, strategy="opengraph", ms=800, ago_h=5),
            log("newshop.example", False, reason="parse_failed", ms=900, ago_h=6),
            # допис 40 днів тому — поза вікном 30 днів
            log("allo.ua", False, reason="parse_failed", ms=10, ago_h=24 * 40),
            # домен лише в журналі (трекерів немає)
            log("logonly.example", True, strategy="microdata", ms=120, ago_h=2),
        ])

    yield
    registry.PLATFORMS[:] = saved


async def test_overview_counts_and_rates():
    ov = await q.platforms_overview()
    assert (ov.verified, ov.experimental, ov.needs_js, ov.blocked) == (2, 1, 1, 1)
    assert ov.other_domains == 2  # newshop.example + logonly.example
    # 24 год: rozetka 4 (2 ok) + comfy 3 (2 ok) + newshop 2 (1 ok) + logonly 1 (1 ok) = 10 спроб, 6 ok
    assert ov.checks_24h == 10
    assert ov.success_rate_24h == 60.0
    assert ov.healed_30d == 1


async def test_table_rows_shapes_and_alias_matching():
    rows = await q.platforms_table()
    by = {r["domain"]: r for r in rows}

    # усі платформи реєстру + невідомі домени з трекерів і журналу
    assert set(by) == {"rozetka.com.ua", "comfy.ua", "allo.ua", "amazon.com", "foxtrot.com.ua",
                       "newshop.example", "logonly.example"}

    rz = by["rozetka.com.ua"]
    assert (rz["trackers"], rz["active"], rz["error"], rz["users"]) == (3, 2, 1, 2)
    assert rz["status"] == "verified" and rz["strategy"] == "selectors"
    assert (rz["checks_30d"], rz["fails_30d"], rz["healed_30d"]) == (5, 2, 1)
    assert rz["error_rate_30d"] == 40.0
    assert rz["avg_ms"] == round((200 + 300 + 1000 + 500 + 100) / 5)
    assert rz["last_fail_reason"] == "parse_failed"  # найсвіжіша невдача (2 год тому)
    assert rz["last_ok"] != "—"
    assert (rz["checks_24h"], rz["fails_24h"], rz["error_rate_24h"], rz["healed_24h"]) == (4, 2, 50.0, 1)

    # аліас comfy.com.ua злито в comfy.ua; той самий юзер → users=1
    cf = by["comfy.ua"]
    assert (cf["trackers"], cf["active"], cf["paused"], cf["users"]) == (2, 1, 1, 1)
    assert (cf["checks_30d"], cf["fails_30d"], cf["error_rate_30d"]) == (3, 1, 33.3)
    assert cf["last_fail_reason"] == "blocked"
    assert "comfy.com.ua" not in by

    # невідомий домен → status "other", стратегія з останнього успішного запису
    ns = by["newshop.example"]
    assert ns["status"] == "other" and ns["country"] == "" and ns["name"] == "newshop.example"
    assert ns["strategy"] == "opengraph"
    assert (ns["trackers"], ns["checks_30d"], ns["fails_30d"]) == (1, 2, 1)

    lo = by["logonly.example"]
    assert lo["trackers"] == 0 and lo["checks_30d"] == 1 and lo["error_rate_30d"] == 0.0

    # поза вікном 30 днів — порожня статистика, але рядок є (реєстр)
    al = by["allo.ua"]
    assert al["checks_30d"] == 0 and al["error_rate_30d"] is None and al["avg_ms"] is None
    assert al["last_ok"] == "—"
    # ...але остання невдача береться з усього журналу
    assert al["last_fail_reason"] == "parse_failed"

    # сортування: активні ↓, потім назва
    assert rows[0]["domain"] == "rozetka.com.ua"
    actives = [r["active"] for r in rows]
    assert actives == sorted(actives, reverse=True)


async def test_table_filters():
    assert {r["domain"] for r in await q.platforms_table(status="other")} == {"newshop.example", "logonly.example"}
    assert {r["domain"] for r in await q.platforms_table(status="verified")} == {"rozetka.com.ua", "comfy.ua"}
    assert {r["domain"] for r in await q.platforms_table(country="US")} == {"amazon.com"}
    assert {r["domain"] for r in await q.platforms_table(search="COMFY")} == {"comfy.ua"}
    assert {r["domain"] for r in await q.platforms_table(search="comfy.com")} == {"comfy.ua"}  # за аліасом
    assert await q.platforms_table(status="blocked", country="UA") == []


async def test_problems_24h():
    problems = await q.platform_problems_24h()
    by = {p["domain"]: p for p in problems}
    # rozetka 50% з 4 спроб (healed), comfy 33.3% з 3 спроб; newshop лише 2 спроби → ні
    assert set(by) == {"rozetka.com.ua", "comfy.ua"}
    assert problems[0]["domain"] == "rozetka.com.ua"  # найвища частка першою
    assert by["rozetka.com.ua"]["healed"] == 1 and by["comfy.ua"]["healed"] == 0
    assert by["comfy.ua"]["rate"] == 33.3 and by["comfy.ua"]["checks"] == 3
    assert await q.platform_problems_24h(min_attempts=5) == []


async def test_top_and_detail():
    top = await q.platforms_top(limit=2)
    assert [r["domain"] for r in top] == ["rozetka.com.ua", "comfy.ua"]
    assert all(r["trackers"] > 0 for r in await q.platforms_top())

    d = await q.platform_detail("www.comfy.com.ua")  # аліас із www → картка Comfy
    assert d["domain"] == "comfy.ua" and d["name"] == "Comfy" and d["status"] == "verified"
    assert d["aliases"] == ["comfy.com.ua"]
    assert len(d["daily_ok"]) == 30 and len(d["daily_fail"]) == 30
    assert sum(v for _, v in d["daily_ok"]) == 2 and sum(v for _, v in d["daily_fail"]) == 1
    assert all(len(day) == 10 for day, _ in d["daily_ok"])
    assert [r["ok"] for r in d["recent"]] == [True, False, True]  # найновіші першими
    assert d["recent"][1]["reason"] == "blocked" and d["recent"][0]["strategy"] == "json-ld"
    assert {r["domain"] for r in d["recent"]} == {"comfy.ua", "comfy.com.ua"}

    u = await q.platform_detail("newshop.example")
    assert u["status"] == "other" and u["sample_url"] == "" and len(u["recent"]) == 2

    empty = await q.platform_detail("nothing.example")
    assert empty["recent"] == [] and sum(v for _, v in empty["daily_ok"]) == 0
