"""
Тести запитів дашборду (app.admin.queries), окрім platforms_* — вони в test_platform_queries.py.

Один детермінований сід на тест (фікстура seed) з явними датами; очікувані числа порахувані
в коментарях до сіду.

    venv/bin/python -m pytest tests/test_admin_queries.py -q
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest_asyncio

from app.admin import auth
from app.admin import queries as q
from app.timeutil import fmt, kyiv_date_key
from app.config import settings
from app.database.models import (
    Admin, AdminAction, Notification, PriceHistory, PriceTracker, SubscriptionTier, TrackerStatus, User,
)
from app.database.session import get_session
from tests.admin_helpers import make_tracker, make_user, utc


@pytest_asyncio.fixture
async def seed(db) -> SimpleNamespace:
    """
    Користувачі (created_at):
      alice  −20д  free            uk  @alice   1001
      bob    −2д   premium +30д    en  @bobby   1002
      carol  −10д  premium +3д1г   uk  —        1003   (закінчується ≤7д)
      dave   −1д   free, blocked   uk  @dave    1004
      eve    −40д  tier=premium, але premium_until −1д → рахується як free   en  @eve  1005
    → total 5, blocked 1, premium 2, new_7d 2 (bob, dave), new_prev_7d 1 (carol), new_30d 4.

    Трекери:
      t1 alice rozetka  «iPhone 15» active  (−10д)      history: −2г 41000, −30г 40500, −5д 42000
      t2 alice comfy    «Samsung TV» paused (−5д)
      t3 bob   rozetka  «iPhone 15» active  (−2д)       той самий canonical_url, що t1 → топ-товар ×2
      t4 bob   allo     «Dyson» error, errors=5 (−2д)
      t5 carol amazon   «Kindle» active, errors=2, USD (−3д)
      t6 dave  rozetka  «Laptop» active, errors=1 (−1д)
    → total 6, active 4, paused 1, error 1; problem: t4, t5.

    Сповіщення: n1 alice/t1 decrease sent −3г; n2 bob/t3 target_reached sent −30г;
                n3 bob/t4 decrease failed −1г; n4 alice/t1 restock sent −40д (поза 30д).
    → notif_24h 1, prev 1, failed 1; checks_24h 1, prev 1.
    """
    bob_until = utc(days=-30)
    carol_until = utc(days=-3, hours=-1)
    alice = await make_user(1001, "Alice", "alice", lang="uk", created=utc(days=20))
    bob = await make_user(1002, "Bob", "bobby", lang="en", tier="premium", premium_until=bob_until, created=utc(days=2))
    carol = await make_user(1003, "Carol", None, lang="uk", tier="premium", premium_until=carol_until, created=utc(days=10))
    dave = await make_user(1004, "Dave", "dave", lang="uk", blocked=True, created=utc(days=1))
    eve = await make_user(1005, "Eve", "eve", lang="en", tier="premium", premium_until=utc(days=1), created=utc(days=40))

    iphone = "https://rozetka.com.ua/p/iphone-15"
    t1 = await make_tracker(alice.id, title="iPhone 15", url=iphone, created=utc(days=10), last_checked=utc(hours=1))
    t2 = await make_tracker(alice.id, source="comfy.ua", title="Samsung TV", status="paused", price=25000, created=utc(days=5))
    t3 = await make_tracker(bob.id, title="iPhone 15", url=iphone, created=utc(days=2))
    t4 = await make_tracker(bob.id, source="allo.ua", title="Dyson", status="error", errors=5, price=15000,
                            created=utc(days=2), last_checked=utc(hours=2))
    t5 = await make_tracker(carol.id, source="amazon.com", title="Kindle", errors=2, price=99.99, currency="USD",
                            created=utc(days=3), last_checked=utc(hours=3))
    t6 = await make_tracker(dave.id, title="Laptop", errors=1, price=30000, created=utc(days=1))

    history = [(utc(hours=2), 41000), (utc(hours=30), 40500), (utc(days=5), 42000)]
    notifs = [
        (alice.id, t1.id, "decrease", 41000, 40000, "sent", None, utc(hours=3)),
        (bob.id, t3.id, "target_reached", None, 40000, "sent", None, utc(hours=30)),
        (bob.id, t4.id, "decrease", 16000, 15000, "failed", "chat not found", utc(hours=1)),
        (alice.id, t1.id, "restock", None, 42000, "sent", None, utc(days=40)),
    ]
    async with get_session() as s:
        s.add_all([PriceHistory(tracker_id=t1.id, price=Decimal(p), recorded_at=dt) for dt, p in history])
        s.add_all([
            Notification(user_id=uid, tracker_id=tid, event_type=ev, old_price=old, new_price=new,
                         status=st, error_message=err, created_at=dt)
            for uid, tid, ev, old, new, st, err, dt in notifs
        ])
    return SimpleNamespace(
        alice=alice, bob=bob, carol=carol, dave=dave, eve=eve, bob_until=bob_until, carol_until=carol_until,
        t1=t1, t2=t2, t3=t3, t4=t4, t5=t5, t6=t6, history=history, notifs=notifs,
    )


# ─── Dashboard ───────────────────────────────────────────────────

async def test_overview_counts(seed):
    ov = await q.overview()
    assert (ov.users_total, ov.users_active, ov.users_blocked, ov.users_premium) == (5, 4, 1, 2)
    assert (ov.users_new_7d, ov.users_new_prev_7d, ov.users_new_30d) == (2, 1, 4)
    assert (ov.trackers_total, ov.trackers_active, ov.trackers_paused, ov.trackers_error) == (6, 4, 1, 1)
    assert (ov.checks_24h, ov.checks_prev_24h) == (1, 1)
    assert (ov.notif_24h, ov.notif_prev_24h, ov.notif_failed_24h) == (1, 1, 1)
    assert ov.premium_expiring_7d == 1
    assert ov.revenue_month_uah == 2 * settings.PREMIUM_PRICE_UAH


async def test_overview_empty_db(db):
    ov = await q.overview()
    assert ov.users_total == 0 and ov.trackers_total == 0 and ov.revenue_month_uah == 0


async def test_daily_series_zero_filled(seed):
    days = 7
    # Доби — київські: ключі й межа "since" беруться з timeutil, групування — за київською датою
    from app.timeutil import utc_range_for_kyiv_days
    since, keys = utc_range_for_kyiv_days(days)

    def expected(dts: list[datetime]) -> dict[str, int]:
        c = Counter(kyiv_date_key(dt) for dt in dts if dt >= since)
        return {k: c.get(k, 0) for k in keys}

    series = await q.daily_series(days)
    assert set(series) == {"users", "checks", "notifications"}
    for name in series:
        assert [d for d, _ in series[name]] == keys, name
    assert dict(series["users"]) == expected([seed.bob.created_at, seed.dave.created_at, seed.carol.created_at,
                                              seed.alice.created_at, seed.eve.created_at])
    assert dict(series["checks"]) == expected([dt for dt, _ in seed.history])
    assert dict(series["notifications"]) == expected([n[7] for n in seed.notifs if n[5] == "sent"])
    assert sum(v for _, v in series["users"]) == 2
    assert sum(v for _, v in series["checks"]) == 3
    assert sum(v for _, v in series["notifications"]) == 2


async def test_breakdowns(seed):
    b = await q.breakdowns()
    assert b["domain"] == [("rozetka.com.ua", 3), ("amazon.com", 1)]   # лише активні
    assert b["language"] == [("uk", 3), ("en", 2)]
    assert b["currency"] == [("UAH", 3), ("USD", 1)]
    assert b["event"] == [("decrease", 2), ("target_reached", 1)]     # restock −40д поза вікном


async def test_premium_expiring(seed):
    rows = await q.premium_expiring(14)
    assert [r["name"] for r in rows] == ["Carol"]
    assert rows[0]["days_left"] == 3
    assert rows[0]["until"] == fmt(seed.carol_until, "%d.%m.%Y")
    assert rows[0]["username"] == "" and rows[0]["telegram_id"] == 1003
    rows = await q.premium_expiring(40)
    assert [r["name"] for r in rows] == ["Carol", "Bob"]   # за датою закінчення


async def test_problem_trackers(seed):
    rows = await q.problem_trackers()
    assert [r["id"] for r in rows] == [seed.t4.id, seed.t5.id]
    assert rows[0]["status"] == "error" and rows[0]["errors"] == 5 and rows[0]["user"] == "Bob @bobby"
    assert rows[1]["status"] == "active" and rows[1]["errors"] == 2 and rows[1]["user"] == "Carol"
    assert rows[0]["last_checked"] != "—"
    assert await q.problem_trackers(limit=1) == rows[:1]


async def test_top_products(seed):
    top = await q.top_products()
    assert top[0] == ("iPhone 15", 2)
    assert {name for name, _ in top} == {"iPhone 15", "Kindle", "Laptop"}   # paused/error не рахуються
    assert await q.top_products(limit=1) == [("iPhone 15", 2)]


# ─── Users ───────────────────────────────────────────────────────

async def test_users_list_default(seed):
    rows = await q.users_list()
    assert [r["name"] for r in rows] == ["Dave", "Bob", "Carol", "Alice", "Eve"]   # нові зверху
    by = {r["name"]: r for r in rows}
    assert by["Alice"]["trackers"] == 2 and by["Alice"]["trackers_active"] == 1
    assert by["Alice"]["username"] == "@alice" and by["Carol"]["username"] == ""
    assert by["Bob"]["tier"] == "premium" and by["Bob"]["until"] == fmt(seed.bob_until, "%d.%m.%Y")
    assert by["Eve"]["tier"] == "free" and by["Eve"]["until"] == "" and by["Eve"]["trackers"] == 0
    assert by["Dave"]["blocked"] is True and by["Alice"]["blocked"] is False
    assert by["Bob"]["lang"] == "en" and by["Bob"]["telegram_id"] == 1002
    assert by["Alice"]["created"] == fmt(seed.alice.created_at, "%d.%m.%Y")


async def test_users_list_search(seed):
    assert [r["name"] for r in await q.users_list(search="ALI")] == ["Alice"]       # ім'я, без регістру
    assert [r["name"] for r in await q.users_list(search=" bobby ")] == ["Bob"]     # username
    assert [r["name"] for r in await q.users_list(search="1003")] == ["Carol"]      # telegram id
    assert await q.users_list(search="zzz") == []


async def test_users_list_tier_and_status(seed):
    assert {r["name"] for r in await q.users_list(tier="premium")} == {"Bob", "Carol"}
    assert {r["name"] for r in await q.users_list(tier="free")} == {"Alice", "Dave", "Eve"}  # Eve прострочена
    assert {r["name"] for r in await q.users_list(status="blocked")} == {"Dave"}
    assert {r["name"] for r in await q.users_list(status="active")} == {"Alice", "Bob", "Carol", "Eve"}
    assert {r["name"] for r in await q.users_list(tier="free", status="active")} == {"Alice", "Eve"}


async def test_user_detail(seed):
    d = await q.user_detail(seed.alice.id)
    assert d["name"] == "Alice" and d["username"] == "alice" and d["tier"] == "free"
    assert d["premium_until"] is None and d["premium_until_str"] == "—" and d["blocked"] is False
    assert [t["title"] for t in d["trackers"]] == ["Samsung TV", "iPhone 15"]   # нові зверху
    t1 = d["trackers"][1]
    assert t1["currency"] == "UAH" and t1["price"] == Decimal("40000.00") and t1["status"] == "active"
    assert t1["url"] == "https://rozetka.com.ua/p/iphone-15" and t1["last_checked"] != "—"
    assert [(n["event"], n["title"]) for n in d["notifications"]] == [("decrease", "iPhone 15"), ("restock", "iPhone 15")]
    assert d["notifications"][0]["old"] == Decimal("41000.00") and d["notifications"][0]["status"] == "sent"

    carol = await q.user_detail(seed.carol.id)
    assert carol["tier"] == "premium" and carol["trackers"][0]["currency"] == "USD"
    assert carol["premium_until_str"] == fmt(seed.carol_until, "%d.%m.%Y")

    bob = await q.user_detail(seed.bob.id)
    assert [n["status"] for n in bob["notifications"]] == ["failed", "sent"]
    assert bob["notifications"][0]["error"] == "chat not found"

    assert await q.user_detail("no-such-user") is None


async def test_tracker_history_ascending_with_limit(seed):
    points = await q.tracker_history(seed.t1.id)
    assert [p for _, p in points] == [42000.0, 40500.0, 41000.0]
    assert points[0][0] == fmt(seed.history[2][0], "%Y-%m-%d %H:%M")
    assert await q.tracker_history(seed.t1.id, limit=2) == points[:2]
    assert await q.tracker_history(seed.t2.id) == []


async def _user(uid: str) -> User:
    async with get_session() as s:
        return await s.get(User, uid)


async def test_set_premium_from_free_starts_now(seed):
    until = await q.set_premium(seed.alice.id, 10)
    assert abs((until - (utc() + timedelta(days=10))).total_seconds()) < 60
    u = await _user(seed.alice.id)
    assert u.tier == SubscriptionTier.PREMIUM and u.premium_until == until


async def test_set_premium_extends_active_subscription(seed):
    until = await q.set_premium(seed.bob.id, 10)
    assert until == seed.bob_until + timedelta(days=10)


async def test_set_premium_expired_restarts_from_now(seed):
    until = await q.set_premium(seed.eve.id, 10)
    assert abs((until - (utc() + timedelta(days=10))).total_seconds()) < 60


async def test_set_premium_unknown_user(seed):
    assert await q.set_premium("no-such-user", 10) is None


async def test_revoke_premium(seed):
    assert await q.revoke_premium(seed.bob.id) is True
    u = await _user(seed.bob.id)
    assert u.tier == SubscriptionTier.FREE and u.premium_until is None
    assert await q.revoke_premium("no-such-user") is False


async def test_set_blocked(seed):
    assert await q.set_blocked(seed.alice.id, True) is True
    assert (await _user(seed.alice.id)).is_blocked is True
    assert await q.set_blocked(seed.alice.id, False) is True
    assert (await _user(seed.alice.id)).is_blocked is False
    assert await q.set_blocked("no-such-user", True) is False


async def test_set_tracker_status(seed):
    assert await q.set_tracker_status(seed.t4.id, TrackerStatus.ACTIVE) is True
    async with get_session() as s:
        t4 = await s.get(PriceTracker, seed.t4.id)
        assert t4.status == TrackerStatus.ACTIVE and t4.consecutive_errors == 0   # ACTIVE скидає лічильник
    assert await q.set_tracker_status(seed.t6.id, TrackerStatus.PAUSED) is True
    async with get_session() as s:
        t6 = await s.get(PriceTracker, seed.t6.id)
        assert t6.status == TrackerStatus.PAUSED and t6.consecutive_errors == 1   # PAUSED — ні
    assert await q.set_tracker_status("no-such-tracker", TrackerStatus.ACTIVE) is False


# ─── Trackers ────────────────────────────────────────────────────

async def test_trackers_list_all(seed):
    rows, sources = await q.trackers_list()
    assert len(rows) == 6
    assert sources == ["allo.ua", "amazon.com", "comfy.ua", "rozetka.com.ua"]
    titles = [r["title"] for r in rows]
    assert titles[0] == "Laptop" and titles[-2:] == ["Samsung TV", "iPhone 15"]   # нові зверху
    by = {r["id"]: r for r in rows}
    assert by[seed.t5.id]["user"] == "Carol" and by[seed.t5.id]["price"] == 99.99 and by[seed.t5.id]["currency"] == "USD"
    assert by[seed.t4.id]["user"] == "Bob @bobby" and by[seed.t4.id]["errors"] == 5
    assert by[seed.t1.id]["last_checked"] != "—" and by[seed.t3.id]["last_checked"] == "—"


async def test_trackers_list_filters(seed):
    rows, sources = await q.trackers_list(status="error")
    assert [r["id"] for r in rows] == [seed.t4.id]
    assert len(sources) == 4   # джерела — завжди всі, не залежать від фільтра
    rows, _ = await q.trackers_list(source="rozetka.com.ua")
    assert {r["id"] for r in rows} == {seed.t1.id, seed.t3.id, seed.t6.id}
    rows, _ = await q.trackers_list(search="iphone")
    assert {r["id"] for r in rows} == {seed.t1.id, seed.t3.id}
    rows, _ = await q.trackers_list(status="paused", source="rozetka.com.ua")
    assert rows == []


async def test_trackers_list_price_none(db):
    u = await make_user(1, "A")
    await make_tracker(u.id, title=None, price=None)
    rows, _ = await q.trackers_list()
    assert rows[0]["price"] is None and rows[0]["title"] == "Без назви" and rows[0]["user"] == "A"


# ─── Admins ──────────────────────────────────────────────────────

async def test_create_admin_lowercases_and_exists(db):
    a = await q.create_admin("  NewAdmin ", "hash", 5)
    assert a.username == "newadmin" and a.telegram_id == 5 and a.is_active
    assert await q.admin_exists("NEWADMIN") is True
    assert await q.admin_exists("newadmin ") is True
    assert await q.admin_exists("other") is False
    rows = await q.admins_list()
    assert len(rows) == 1
    assert rows[0]["username"] == "newadmin" and rows[0]["last_login"] == "ніколи" and rows[0]["telegram_id"] == 5


async def test_admin_mutations(db):
    a = await q.create_admin("one", "hash", None)
    b = await q.create_admin("two", "hash", None)
    assert await q.admins_count_active() == 2

    assert await q.set_admin_active(b.id, False) is True
    assert await q.admins_count_active() == 1
    assert await q.set_admin_active("no-such", False) is False

    assert await q.set_admin_password(a.id, auth.hash_password("new-password-1")) is True
    assert await q.set_admin_password("no-such", "x") is False
    assert await q.set_admin_telegram(a.id, 777) is True
    assert await q.set_admin_telegram("no-such", 1) is False

    async with get_session() as s:
        fresh = await s.get(Admin, a.id)
        assert auth.verify_password("new-password-1", fresh.password_hash) and fresh.telegram_id == 777
        assert (await s.get(Admin, b.id)).is_active is False

    assert await q.set_admin_telegram(a.id, None) is True
    rows = await q.admins_list()
    assert [(r["username"], r["active"], r["telegram_id"]) for r in rows] == [("one", True, ""), ("two", False, "")]


async def test_audit_log_newest_first_with_limit(db):
    async with get_session() as s:
        s.add_all([
            AdminAction(admin_username="a", action="first", target="t1", created_at=utc(hours=3)),
            AdminAction(admin_username="b", action="second", details="d2", created_at=utc(hours=2)),
            AdminAction(admin_username="c", action="third", created_at=utc(hours=1)),
        ])
    rows = await q.audit_log()
    assert [r["action"] for r in rows] == ["third", "second", "first"]
    assert rows[2]["target"] == "t1" and rows[2]["details"] == "" and rows[1]["details"] == "d2"
    assert [r["action"] for r in await q.audit_log(limit=2)] == ["third", "second"]
    assert await q.audit_log() != [] and rows[0]["when"] != "—"
