"""Системна статистика (/system): збір ресурсів, розпізнавання процесів сервісу, статистика бази, сторінка."""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.admin import system as sysmod
from app.database.models import PriceHistory, PriceTracker, ScrapeLog, User
from app.database.session import get_session
from tests.admin_helpers import login, make_admin


# ─── Форматування ────────────────────────────────────────────────

@pytest.mark.parametrize("n, lang, expected", [
    (None, "uk", "—"),
    (0, "uk", "0 Б"),
    (512, "en", "512 B"),
    (1536, "uk", "1,5 КБ"),
    (1536, "en", "1.5 KB"),
    (712704, "uk", "696 КБ"),
    (24 * 1024 ** 3, "en", "24.0 GB"),
    (250 * 1024 ** 2, "uk", "250 МБ"),
])
def test_fmt_bytes(n, lang, expected):
    assert sysmod.fmt_bytes(n, lang) == expected


@pytest.mark.parametrize("seconds, lang, expected", [
    (None, "uk", "—"),
    (20, "uk", "<1 хв"),
    (20, "en", "<1 min"),
    (12 * 60, "uk", "12 хв"),
    (3 * 3600 + 5 * 60, "en", "3 h 5 min"),
    (2 * 86400 + 4 * 3600, "uk", "2 д 4 год"),
    (-5, "en", "<1 min"),
])
def test_fmt_duration(seconds, lang, expected):
    assert sysmod.fmt_duration(seconds, lang) == expected


@pytest.mark.parametrize("pct, expected", [(None, "good"), (10, "good"), (69.9, "good"), (70, "warning"), (85, "critical"), (99, "critical")])
def test_level(pct, expected):
    assert sysmod.level(pct) == expected


def test_seconds_since_naive_is_utc():
    dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
    assert 590 <= sysmod.seconds_since(dt) <= 610
    assert sysmod.seconds_since(None) is None


# ─── Розпізнавання процесів ──────────────────────────────────────

ROOT = Path("/Users/x/work/priceguard")


@pytest.mark.parametrize("cmdline, expected", [
    ([f"{ROOT}/venv/bin/python", f"{ROOT}/bot.py"], "bot"),
    ([f"{ROOT}/venv/bin/python", f"{ROOT}/admin.py"], "admin"),
    (["/Users/x/work/snipket/venv/bin/python", "/Users/x/work/snipket/bot.py"], None),   # чужий проєкт
    (["/Users/x/work/snipket/venv/bin/python", "/Users/x/work/snipket/admin.py"], None),
    (["python", "bot.py"], "maybe:bot"),                                                # відносний — перевіряється cwd
    (["python", f"{ROOT}/scripts/probe.py"], None),
    ([], None),
    (None, None),
])
def test_match_role(cmdline, expected):
    assert sysmod.match_role(cmdline, ROOT) == expected


def _spawn(script: Path, cwd: Path) -> subprocess.Popen:
    script.write_text("import time\ntime.sleep(30)\n")
    return subprocess.Popen([sys.executable, str(script)], cwd=str(cwd))


def test_find_service_processes_real_subprocess_and_foreign_project(tmp_path):
    ours = tmp_path / "priceguard"
    theirs = tmp_path / "snipket"
    ours.mkdir(); theirs.mkdir()
    p_bot = _spawn(ours / "bot.py", ours)
    p_foreign = _spawn(theirs / "bot.py", theirs)
    try:
        deadline = time.time() + 5
        found = {}
        while time.time() < deadline:
            found = sysmod.find_service_processes(ours)
            if "bot" in found:
                break
            time.sleep(0.1)
        assert set(found) == {"bot"} and found["bot"].pid == p_bot.pid
        assert p_foreign.pid not in {p.pid for p in found.values()}

        snap = sysmod.collect_system(root=ours, sample_seconds=0.05)
        bot = snap.processes["bot"]
        assert bot.pids == [p_bot.pid] and bot.rss > 0 and bot.threads >= 1
        assert bot.started is not None and sysmod.seconds_since(bot.started) < 60
        assert snap.service_rss == bot.rss and "admin" not in snap.processes and "browser" not in snap.processes
    finally:
        for p in (p_bot, p_foreign):
            p.kill(); p.wait()


def test_collect_system_host_numbers_are_sane(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "bot.out.log").write_bytes(b"x" * 2048)
    snap = sysmod.collect_system(root=tmp_path, sample_seconds=0.05)
    h = snap.host
    assert h.cpu_logical >= 1 and h.cpu_physical >= 1 and 0 <= h.cpu_percent <= 100
    assert h.ram_total > 0 and 0 < h.ram_used <= h.ram_total and 0 <= h.ram_percent <= 100
    assert h.disk_total > 0 and h.disk_free <= h.disk_total
    assert h.boot_time < datetime.now(timezone.utc) and h.python
    assert snap.processes == {} and snap.service_rss == 0
    assert snap.logs_bytes == 2048


def test_pm2_restarts_without_pm2_is_empty(monkeypatch):
    monkeypatch.setattr(sysmod, "_pm2_cache", (0.0, {}))
    monkeypatch.setattr(sysmod.shutil, "which", lambda name: None)
    assert sysmod.pm2_restarts() == {}


def test_pm2_restarts_parses_jlist(monkeypatch):
    monkeypatch.setattr(sysmod, "_pm2_cache", (0.0, {}))
    monkeypatch.setattr(sysmod.shutil, "which", lambda name: "/usr/local/bin/pm2")

    class R:
        stdout = '[{"pid": 111, "pm2_env": {"restart_time": 3}}, {"pid": 0, "pm2_env": {"restart_time": 9}}]'
    monkeypatch.setattr(sysmod.subprocess, "run", lambda *a, **k: R())
    assert sysmod.pm2_restarts() == {111: 3}


# ─── База даних ──────────────────────────────────────────────────

async def _seed_db():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with get_session() as s:
        u = User(telegram_id=1, language_code="uk"); s.add(u); await s.flush()
        tr = PriceTracker(user_id=u.id, url="https://x.ua/p", canonical_url="https://x.ua/p", source="x.ua",
                          title="T", status="active")
        s.add(tr); await s.flush()
        for i in range(40):
            s.add(PriceHistory(tracker_id=tr.id, price=Decimal(100 + i), recorded_at=now - timedelta(hours=i * 6)))
        s.add(ScrapeLog(domain="x.ua", ok=True, strategy="json-ld", duration_ms=10, created_at=now - timedelta(minutes=3)))


async def test_db_stats_counts_sizes_and_growth(db):
    await _seed_db()
    st = await sysmod.db_stats()
    assert st.engine == "sqlite" and st.path and st.path.endswith("test.db")
    by = {t.name: t for t in st.tables}
    assert by["price_history"].rows == 40 and by["users"].rows == 1 and by["price_trackers"].rows == 1
    assert by["scrape_log"].rows == 1 and by["admins"].rows == 0
    assert st.total_rows == sum(t.rows for t in st.tables) == 43
    assert by["price_history"].bytes and by["price_history"].bytes > 0        # dbstat: дані + індекси
    assert st.tables[0].bytes == max(t.bytes or 0 for t in st.tables)         # сортування за розміром
    assert st.file_bytes > 0 and st.free_bytes >= 0 and st.wal_bytes >= 0
    # 29 записів історії за останні 7 днів (кожні 6 год) → приріст > 0
    assert st.growth_bytes_per_month and st.growth_bytes_per_month > 0
    assert sysmod.seconds_since(st.last_check_at) < 120
    assert 150 <= sysmod.seconds_since(st.last_scrape_at) <= 210


async def test_db_stats_empty_db(db):
    st = await sysmod.db_stats()
    assert st.total_rows == 0 and st.growth_bytes_per_month is None
    assert st.last_check_at is None and st.last_scrape_at is None


# ─── Сторінка ────────────────────────────────────────────────────

def _fake_snapshot(with_bot: bool) -> sysmod.SystemSnapshot:
    started = datetime.now(timezone.utc) - timedelta(hours=5)
    host = sysmod.HostStats(
        os_name="Darwin 25 (arm64)", python="3.11.9", cpu_logical=14, cpu_physical=14, cpu_percent=12.5,
        load_avg=(2.5, 2.1, 1.9), ram_total=24 * 1024 ** 3, ram_used=18 * 1024 ** 3, ram_available=6 * 1024 ** 3,
        ram_percent=75.0, swap_total=0, swap_used=0, swap_percent=0.0,
        disk_total=500 * 1024 ** 3, disk_used=442 * 1024 ** 3, disk_free=58 * 1024 ** 3, disk_percent=88.4,
        boot_time=started - timedelta(days=3),
    )
    procs = {}
    if with_bot:
        procs["bot"] = sysmod.ProcStats("bot", [111], 24 * 1024 ** 2, 0.4, 6, started, restarts=2)
        procs["browser"] = sysmod.ProcStats("browser", [200, 201, 202], 700 * 1024 ** 2, 3.1, 40, started, count=3)
    procs["admin"] = sysmod.ProcStats("admin", [112], 19 * 1024 ** 2, 0.1, 4, started, restarts=0)
    return sysmod.SystemSnapshot(host=host, processes=procs,
                                 service_rss=sum(p.rss for p in procs.values()),
                                 service_cpu_percent=round(sum(p.cpu_percent for p in procs.values()), 1),
                                 logs_bytes=3 * 1024 ** 2)


def _patch_collector(monkeypatch, fn) -> None:
    """Патчимо модуль, який реально використовує сторінка: фікстура `user` перезавантажує app.* між тестами."""
    page_mod = sys.modules["app.admin.pages.system"]
    monkeypatch.setattr(page_mod.sysmod, "collect_system", fn)


def _table_rows(user, field: str) -> list[dict]:
    from nicegui import ui
    for el in user.find(kind=ui.table).elements:
        if el.rows and field in el.rows[0]:
            return el.rows
    return []


async def test_system_page_renders_uk(db, user, monkeypatch):
    await _seed_db()
    await make_admin()
    _patch_collector(monkeypatch, lambda *a, **k: _fake_snapshot(with_bot=True))
    await login(user)
    await user.open("/system")
    await user.should_see("Система")
    await user.should_see("Бот працює · 5 год 0 хв", retries=40)
    await user.should_see("Браузер активний · 3 проц.")
    await user.should_see("Сервер")
    await user.should_see("Процеси сервісу")
    await user.should_see("База даних")
    roles = {r["role"]: r for r in _table_rows(user, "restarts")}
    assert roles["Бот + планувальник"]["restarts"] == "2" and roles["Браузер (Playwright)"]["ram"] == "700 МБ"
    tables = {r["name"]: r for r in _table_rows(user, "share")}
    assert tables["price_history"]["rows"] == "40"


async def test_system_page_bot_down_en(db, user, monkeypatch):
    await make_admin()
    _patch_collector(monkeypatch, lambda *a, **k: _fake_snapshot(with_bot=False))
    await login(user)
    await user.open("/system")
    user.find(marker="lang-en").click()
    await user.should_see("Bot is not running", retries=40)
    await user.should_see("Browser asleep")
    await user.should_see("No price checks yet")
    await user.should_see("Server")
    await user.should_see("Service processes")


async def test_system_page_survives_collector_crash(db, user, monkeypatch):
    await make_admin()

    def boom(*a, **k):
        raise RuntimeError("psutil exploded")
    _patch_collector(monkeypatch, boom)
    await login(user)
    await user.open("/system")
    await user.should_see("Збираю дані…")
    await user.should_see("База даних", retries=40)
