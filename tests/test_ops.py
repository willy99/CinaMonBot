"""A5: бекап бази (відновлюваний, ротація) і зовнішня перевірка живучості з алертом адміну."""
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
def health_env(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(settings, "HEALTHCHECK_PING_URL", "https://hc-ping.example/uuid")
    monkeypatch.setattr(settings, "API_HEALTH_URL", "")
    return Recorder()


async def test_stale_scheduler_alerts_once_then_recovers(db, health_env):
    rec = health_env
    async with get_session() as s:
        s.add(Heartbeat(name="scheduler", at=datetime.utcnow() - timedelta(minutes=30)))
    async with rec.client() as c:
        assert await hc.check(c, now=1000.0)
        assert rec.telegram() == []                          # перша погана перевірка — ще не тривога
        assert await hc.check(c, now=1300.0)                 # друга поспіль — тривога
        assert await hc.check(c, now=1600.0)                 # ще погано, але алерт не повторюємо
    tg = rec.telegram()
    assert len(tg) == 1 and "планувальник" in tg[0] and "BOT_TOKEN" not in tg[0]
    assert sum(1 for c in rec.calls if c[1].endswith("/fail")) == 3

    await heartbeat.beat(heartbeat.SCHEDULER)
    async with rec.client() as c:
        assert await hc.check(c, now=1700.0) == []
    assert "знову працює" in rec.telegram()[-1]
    assert ("GET", "https://hc-ping.example/uuid", "") in rec.calls        # пінг «живий»


async def test_fresh_start_and_single_blip_are_silent(db, health_env):
    """Щойно запущений бот (ще жодного тіку) і разовий збій не шлють адміну нічого — ні тривоги, ні «відновився»."""
    rec = health_env
    async with rec.client() as c:
        assert await hc.check(c, now=1000.0) == []           # heartbeat ще не було — свіжий старт
    async with get_session() as s:
        s.add(Heartbeat(name="scheduler", at=datetime.utcnow() - timedelta(minutes=30)))
    async with rec.client() as c:
        assert await hc.check(c, now=1300.0)                 # один збій
    await heartbeat.beat(heartbeat.SCHEDULER)
    async with rec.client() as c:
        assert await hc.check(c, now=1600.0) == []           # одразу відновився
    assert rec.telegram() == []
