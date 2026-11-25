"""Модулі з БД на тестовому контексті: адмін-автентифікація й запити, heartbeat, перевірка живучості, бекап, middleware."""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import unquote_plus

import httpx
import pytest

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


async def test_healthcheck_never_messages_telegram(kit, monkeypatch):
    """14.10: технічні тривоги в Telegram прибрано (спам «не тікав» / «на паузі»). Лишається лише пінг моніторингу."""
    monkeypatch.setattr(kit.settings, "HEALTHCHECK_PING_URL", "https://hc-ping.example/uuid")
    rec = _Rec()
    async with kit.get_session() as s:
        s.add(kit.models.Heartbeat(name="scheduler", at=datetime.utcnow() - timedelta(hours=3)))
    async with rec.client() as c:
        for _ in range(5):                                           # годинами погано
            assert await hc.check(c)
    await heartbeat.beat(heartbeat.SCHEDULER)
    async with rec.client() as c:
        assert await hc.check(c) == []                               # відновився
    assert rec.telegram() == [] and not any("api.telegram.org" in c[1] for c in rec.calls)
    fails = [c for c in rec.calls if c[1].endswith("/fail")]
    assert len(fails) == 5 and "не тікав" in fails[0][2]
    assert rec.calls[-1][:2] == ("GET", "https://hc-ping.example/uuid")


@pytest.mark.parametrize("url, ok_call, fail_call", [
    # Uptime Kuma: адресу копіюють разом із ?status=up&msg=OK&ping= — параметри замінюємо своїми
    ("http://healthcheck:3001/api/push/AbC123?status=up&msg=OK&ping=",
     ("GET", "http://healthcheck:3001/api/push/AbC123?status=up&msg=OK"),
     ("GET", "http://healthcheck:3001/api/push/AbC123?status=down&msg=")),
    ("https://hc-ping.example/uuid",
     ("GET", "https://hc-ping.example/uuid"),
     ("POST", "https://hc-ping.example/uuid/fail")),
])
async def test_healthcheck_ping_formats(kit, monkeypatch, url, ok_call, fail_call):
    """22.10: Kuma-URL + «/fail» давав ping=/fail при status=up — Kuma бачила «up» навіть під час збою."""
    monkeypatch.setattr(kit.settings, "HEALTHCHECK_PING_URL", url)
    rec = _Rec()
    await heartbeat.beat(heartbeat.SCHEDULER)
    async with rec.client() as c:
        await hc.check(c)
    assert rec.calls[-1][:2] == ok_call
    async with kit.get_session() as s:
        (await s.get(kit.models.Heartbeat, "scheduler")).at = datetime.utcnow() - timedelta(minutes=30)
    async with rec.client() as c:
        await hc.check(c)
    method, sent_url, body = rec.calls[-1]
    assert method == fail_call[0] and sent_url.startswith(fail_call[1])
    text = unquote_plus(sent_url) + body
    assert "планувальник бота не тікав" in text


async def test_healthcheck_survives_unreachable_monitor(kit, monkeypatch):
    """Монітор лежить / tailnet недоступний (немає мережі) — перевірка не падає, монітор сам побачить тишу."""
    monkeypatch.setattr(kit.settings, "HEALTHCHECK_PING_URL", "http://healthcheck:3001/api/push/x")

    def handler(req):
        if "healthcheck" in str(req.url):
            raise httpx.ConnectError("no route", request=req)
        return httpx.Response(200, json={"ok": True})
    await heartbeat.beat(heartbeat.SCHEDULER)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await hc.check(c) == []


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
