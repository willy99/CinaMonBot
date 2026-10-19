"""A5: бекап бази (відновлюваний, ротація) і зовнішня перевірка живучості (пінг моніторингу, без Telegram)."""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

import httpx
import pytest

from app.database.models import Heartbeat
from app.database.session import get_session
from app.config import settings
from botkit.ops import backup as bk
from botkit.ops import healthcheck as hc
from botkit.ops import heartbeat


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path))
    return tmp_path


async def test_backup_is_restorable_and_private(db, backup_dir, tmp_path):
    from app.database.models import User
    async with get_session() as s:
        s.add(User(telegram_id=555))
    path = bk.backup()
    assert path.name.endswith(".db.gz") and oct(path.stat().st_mode)[-3:] == "600"
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(path.read_bytes()))
    con = sqlite3.connect(restored)
    assert con.execute("select count(*) from users where telegram_id = 555").fetchone() == (1,)
    con.close()


def test_prune_keeps_recent(backup_dir):
    old, new = backup_dir / "priceguard-old.db.gz", backup_dir / "priceguard-new.db.gz"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (time.time() - 20 * 86400,) * 2)
    assert bk.prune(keep_days=14) == [old] and new.exists()


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []

    def client(self) -> httpx.AsyncClient:
        def handler(req: httpx.Request) -> httpx.Response:
            self.calls.append((req.method, str(req.url), req.content.decode() if req.content else ""))
            return httpx.Response(200, json={"ok": True})
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def telegram(self) -> list[str]:
        return [json.loads(c[2])["text"] for c in self.calls if "api.telegram.org" in c[1]]


@pytest.fixture
def health_env(monkeypatch):
    monkeypatch.setattr(settings, "HEALTHCHECK_PING_URL", "https://hc-ping.example/uuid")
    monkeypatch.setattr(settings, "API_HEALTH_URL", "")
    return Recorder()


async def test_stale_scheduler_pings_fail_but_never_telegram(db, health_env):
    """14.10: технічні тривоги адміну в Telegram прибрано (спам). Лишився лише пінг зовнішнього моніторингу."""
    rec = health_env
    async with get_session() as s:
        s.add(Heartbeat(name="scheduler", at=datetime.utcnow() - timedelta(minutes=30)))
    async with rec.client() as c:
        for _ in range(4):
            problems = await hc.check(c)
            assert problems and "планувальник" in problems[0]
    assert sum(1 for c in rec.calls if c[1].endswith("/fail")) == 4

    await heartbeat.beat(heartbeat.SCHEDULER)
    async with rec.client() as c:
        assert await hc.check(c) == []
    assert rec.calls[-1][:2] == ("GET", "https://hc-ping.example/uuid")        # пінг «живий»
    assert rec.telegram() == [] and not any("api.telegram.org" in c[1] for c in rec.calls)


async def test_fresh_start_is_not_a_problem(db, health_env):
    """Щойно запущений бот (ще жодного тіку) — не збій."""
    async with health_env.client() as c:
        assert await hc.check(c) == []
