"""Модулі з БД на тестовому контексті: адмін-автентифікація й запити, heartbeat, перевірка живучості, бекап, middleware."""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx

from botkit.admin import auth
from botkit.admin import queries as q
from botkit.bot.middlewares import BlockedUserMiddleware, UserRateLimitMiddleware
from botkit.ops import backup as bk
from botkit.ops import healthcheck as hc
from botkit.ops import heartbeat


async def add_admin(kit, username="alice", role="admin", password="correct-horse-7"):
    return await q.create_admin(username, auth.hash_password(password), 42, role=role)


async def test_password_and_safe_next():
    stored = auth.hash_password("correct-horse-7")
    assert auth.verify_password("correct-horse-7", stored) and not auth.verify_password("nope", stored)
    assert auth.safe_next_path("/users") == "/users"
    assert auth.safe_next_path("//evil.example") == auth.safe_next_path("/\\evil") == "/"


async def test_queries_bump_session_version_and_owner(kit):
    a = await add_admin(kit)
    await add_admin(kit, "bob")
    assert await q.ensure_owner() == "alice" and await q.ensure_owner() is None
    await q.set_admin_password(a.id, "x")
    rows = {r["username"]: r for r in await q.admins_list()}
    assert rows["alice"]["role"] == "owner"
    async with kit.get_session() as s:
        assert (await s.get(kit.models.Admin, a.id)).session_version == 2     # ensure_owner + пароль
    assert await q.owners_count_active() == 1 and await q.admins_count_active() == 2
    assert await auth.verify_current_password(a.id, "correct-horse-7") is False   # пароль змінено на «x»


async def test_db_lockout_and_audit(kit):
    await add_admin(kit)
    for _ in range(kit.settings.ADMIN_LOGIN_MAX_ATTEMPTS * auth.DB_LOCK_FACTOR):
        await auth.db_register_failure("alice")
    assert await auth.db_lock_remaining("ALICE") > 0
    await auth.audit("test_action", "target")
    assert (await q.audit_log())[0]["action"] == "test_action"


async def test_heartbeat_status(kit):
    assert (await heartbeat.status())["stale"] is True
    await heartbeat.beat(heartbeat.SCHEDULER, paused={})
    assert (await heartbeat.status())["stale"] is False


class _Rec:
    def __init__(self):
        self.calls = []

    def client(self):
        def handler(req):
            self.calls.append((req.method, str(req.url), req.content.decode() if req.content else ""))
            return httpx.Response(200, json={"ok": True})
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def telegram(self):
        return [json.loads(c[2])["text"] for c in self.calls if "api.telegram.org" in c[1]]


async def test_healthcheck_debounced_admin_only(kit, monkeypatch):
    monkeypatch.setattr(hc, "STATE_FILE", kit.root / "state.json")
    rec = _Rec()
    async with rec.client() as c:
        assert await hc.check(c, now=1000.0) == []                   # свіжий старт — тиша
    async with kit.get_session() as s:
        s.add(kit.models.Heartbeat(name="scheduler", at=datetime.utcnow() - timedelta(minutes=30)))
    async with rec.client() as c:
        await hc.check(c, now=1300.0)
        assert rec.telegram() == []                                  # один збій — тиша
        await hc.check(c, now=1600.0)
        assert rec.telegram() == []                                  # 5 хв збою — ще тиша (поріг 10 хв)
        await hc.check(c, now=1900.0)
    assert len(rec.telegram()) == 1 and rec.telegram()[0].startswith("🚨 TestBot")
    assert all('"chat_id": 1' in c[2] for c in rec.calls if "api.telegram.org" in c[1])   # лише адміну
    await heartbeat.beat(heartbeat.SCHEDULER)
    async with rec.client() as c:
        await hc.check(c, now=2200.0)
    assert "знову працює" in rec.telegram()[-1]


async def test_healthcheck_repeats_less_often_new_kind_at_once(kit, monkeypatch):
    """13.10: УЗ лежала годинами — «uz на паузі вже N-й раз» щогодини. Тепер 1 год, 2 год, 4 год…;
    новий вид проблеми (підтверджений другою перевіркою) — одразу."""
    monkeypatch.setattr(hc, "STATE_FILE", kit.root / "state.json")
    problems = ["uz на паузі вже 5-й раз (X)"]

    async def fake_problems(client):
        return list(problems)
    monkeypatch.setattr(hc, "_problems", fake_problems)
    rec = _Rec()
    sent_at = []
    async with rec.client() as c:
        for minute in range(0, 9 * 60, 5):                            # 9 годин перевірок кожні 5 хв
            problems[0] = f"uz на паузі вже {5 + minute // 30}-й раз (X)"
            before = len(rec.telegram())
            await hc.check(c, now=minute * 60.0)
            if len(rec.telegram()) > before:
                sent_at.append(minute)
    assert sent_at == [10, 70, 190, 430]                              # +1 год, +2 год, +4 год
    async with rec.client() as c:
        problems.append("планувальник бота не тікав 900 с")
        await hc.check(c, now=(9 * 60 + 5) * 60.0)
        assert len(rec.telegram()) == 4                               # новий вид — ще одна перевірка
        await hc.check(c, now=(9 * 60 + 10) * 60.0)
    assert len(rec.telegram()) == 5 and "не тікав" in rec.telegram()[-1]


async def test_backup_restorable_and_pruned(kit):
    async with kit.get_session() as s:
        s.add(kit.models.User(telegram_id=555))
    path = bk.backup()
    assert path.name.startswith("testbot-") and oct(path.stat().st_mode)[-3:] == "600"
    restored = kit.root / "r.db"
    restored.write_bytes(gzip.decompress(path.read_bytes()))
    assert sqlite3.connect(restored).execute("select count(*) from users").fetchone() == (1,)
    os.utime(path, (time.time() - 30 * 86400,) * 2)
    assert bk.prune(keep_days=14) == [path]


async def test_middlewares(kit):
    async with kit.get_session() as s:
        s.add(kit.models.User(telegram_id=11, is_blocked=True))
    handler = AsyncMock(return_value="ok")
    who = lambda uid: MagicMock(id=uid, language_code="uk")  # noqa: E731
    assert await BlockedUserMiddleware()(handler, MagicMock(), {"event_from_user": who(11)}) is None
    assert await BlockedUserMiddleware()(handler, MagicMock(), {"event_from_user": who(12)}) == "ok"
    mw = UserRateLimitMiddleware()
    results = [await mw(handler, MagicMock(), {"event_from_user": who(13)}) for _ in range(22)]
    assert results.count("ok") == 20 and results[-1] is None
    assert await mw(handler, MagicMock(), {"event_from_user": who(1)}) == "ok"      # адмін без ліміту


async def test_healthcheck_silent_after_laptop_sleep(kit, monkeypatch):
    """13.10: Mac спав 25 хв — бот не тікав, а pm2 після пробудження зробив дві перевірки за 18 с → хибна тривога."""
    monkeypatch.setattr(hc, "STATE_FILE", kit.root / "state.json")
    rec = _Rec()
    await heartbeat.beat(heartbeat.SCHEDULER)
    async with rec.client() as c:
        await hc.check(c, now=1000.0)                                # усе добре, далі сон
    async with kit.get_session() as s:
        hb = await s.get(kit.models.Heartbeat, "scheduler")
        hb.at = datetime.utcnow() - timedelta(minutes=26)
    async with rec.client() as c:
        await hc.check(c, now=1000.0 + 26 * 60)                      # прокинулись: heartbeat старий
        await hc.check(c, now=1000.0 + 26 * 60 + 18)                 # pm2 надолужує
    await heartbeat.beat(heartbeat.SCHEDULER)                        # бот тікнув після пробудження
    async with rec.client() as c:
        await hc.check(c, now=1000.0 + 31 * 60)
    assert rec.telegram() == []                                      # ні тривоги, ні «знову працює»
