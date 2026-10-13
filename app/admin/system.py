"""
Системна статистика для сторінки /system: ресурси машини, процеси сервісу, база даних.

Процеси сервісу шукаються за шляхом проєкту в командному рядку: на тій самій машині
можуть крутитися інші проєкти зі своїми bot.py/admin.py (напр. ~/work/snipket) — їх не рахуємо.

    snap = await asyncio.to_thread(collect_system)   # psutil, блокує ~0.5 с на вимір CPU
    db = await db_stats()                            # async, через сесію SQLAlchemy

Пам'ять процесів — RSS: на macOS USS/"physical footprint" чужим процесам недоступні
навіть для того самого користувача, а RSS трохи завищує спільні бібліотеки.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil
import structlog
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from app.config import settings
from app.database.models import Base, PriceHistory, ScrapeLog
from app.database.session import engine, get_session

logger = structlog.get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROLE_SCRIPTS = {"bot.py": "bot", "admin.py": "admin"}
BROWSER_MARKERS = ("chrom", "headless_shell", "playwright", "node")
SAMPLE_SECONDS = 0.5
PM2_CACHE_SECONDS = 60


# ─── Моделі ──────────────────────────────────────────────────────

@dataclass
class HostStats:
    os_name: str
    python: str
    cpu_logical: int
    cpu_physical: int
    cpu_percent: float
    load_avg: tuple[float, float, float] | None
    ram_total: int
    ram_used: int
    ram_available: int
    ram_percent: float
    swap_total: int
    swap_used: int
    swap_percent: float
    disk_total: int
    disk_used: int
    disk_free: int
    disk_percent: float
    boot_time: datetime


@dataclass
class ProcStats:
    role: str                      # "bot" | "admin" | "browser"
    pids: list[int]
    rss: int
    cpu_percent: float             # частка всієї машини, %
    threads: int
    started: datetime | None       # UTC
    restarts: int | None = None    # з pm2, якщо доступно
    count: int = 1                 # для браузера — кількість процесів Chromium


@dataclass
class SystemSnapshot:
    host: HostStats
    processes: dict[str, ProcStats] = field(default_factory=dict)
    service_rss: int = 0
    service_cpu_percent: float = 0.0
    logs_bytes: int = 0
    collected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TableStats:
    name: str
    rows: int
    bytes: int | None              # дані + індекси; None, якщо СУБД не дає порахувати


@dataclass
class DbStats:
    engine: str                    # "sqlite" | "postgresql"
    path: str | None
    file_bytes: int                # основний файл (SQLite) або розмір бази (Postgres)
    wal_bytes: int
    free_bytes: int                # порожні сторінки, які звільнить VACUUM
    total_rows: int
    tables: list[TableStats]
    growth_bytes_per_month: int | None
    last_check_at: datetime | None
    last_scrape_at: datetime | None


# ─── Процеси ─────────────────────────────────────────────────────

def match_role(cmdline: list[str] | None, root: Path = PROJECT_ROOT) -> str | None:
    """Роль процесу сервісу за командним рядком: 'bot' / 'admin' / None (чужий процес)."""
    if not cmdline:
        return None
    root_s = str(root)
    for arg in cmdline:
        p = Path(arg)
        if p.name in ROLE_SCRIPTS and (str(p.parent) == root_s or str(p) == str(root / p.name)):
            return ROLE_SCRIPTS[p.name]
    # Відносний шлях (python bot.py) — перевіряємо робочу теку окремо в find_service_processes
    for arg in cmdline:
        if arg in ROLE_SCRIPTS:
            return "maybe:" + ROLE_SCRIPTS[arg]
    return None


def find_service_processes(root: Path = PROJECT_ROOT) -> dict[str, psutil.Process]:
    found: dict[str, psutil.Process] = {}
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            role = match_role(proc.info.get("cmdline"), root)
            if role is None:
                continue
            if role.startswith("maybe:"):
                if Path(proc.cwd()) != root:
                    continue
                role = role.split(":", 1)[1]
            # Якщо кілька екземплярів — беремо найстаріший (головний)
            if role not in found or proc.create_time() < found[role].create_time():
                found[role] = proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return found


_pm2_cache: tuple[float, dict[int, int]] = (0.0, {})


def pm2_restarts() -> dict[int, int]:
    """{pid: кількість перезапусків} з pm2 jlist; порожньо, якщо pm2 недоступний. Кеш 60 с."""
    global _pm2_cache
    ts, data = _pm2_cache
    if time.monotonic() - ts < PM2_CACHE_SECONDS:
        return data
    data = {}
    exe = shutil.which("pm2")
    if exe:
        try:
            out = subprocess.run([exe, "jlist"], capture_output=True, text=True, timeout=5).stdout
            for app_ in json.loads(out or "[]"):
                pid = app_.get("pid")
                if pid:
                    data[int(pid)] = int(app_.get("pm2_env", {}).get("restart_time", 0))
        except Exception as e:  # noqa: BLE001
            logger.debug("pm2_jlist_failed", error=type(e).__name__)
    _pm2_cache = (time.monotonic(), data)
    return data


def _is_browser(proc: psutil.Process) -> bool:
    try:
        name = (proc.name() or "").lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    return any(m in name for m in BROWSER_MARKERS)


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def collect_system(root: Path = PROJECT_ROOT, sample_seconds: float | None = None) -> SystemSnapshot:
    """Синхронний збір (psutil). Викликати через asyncio.to_thread — чекає sample_seconds для CPU."""
    sample = SAMPLE_SECONDS if sample_seconds is None else sample_seconds
    ncpu = psutil.cpu_count() or 1

    procs = find_service_processes(root)
    groups: dict[str, list[psutil.Process]] = {}
    for role, proc in procs.items():
        groups.setdefault(role, []).append(proc)
        try:
            for child in proc.children(recursive=True):
                key = "browser" if role == "bot" and _is_browser(child) else role
                groups.setdefault(key, []).append(child)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # CPU: перший виклик "заводить" лічильник, другий після паузи дає відсоток
    psutil.cpu_percent(None)
    for plist in groups.values():
        for p in plist:
            try:
                p.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    time.sleep(sample)
    host_cpu = psutil.cpu_percent(None)

    restarts = pm2_restarts()
    processes: dict[str, ProcStats] = {}
    for role, plist in groups.items():
        rss = cpu = threads = 0
        pids: list[int] = []
        for p in plist:
            try:
                with p.oneshot():
                    rss += p.memory_info().rss
                    cpu += p.cpu_percent(None)
                    threads += p.num_threads()
                    pids.append(p.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        if not pids:
            continue
        main = procs.get(role) or plist[0]
        try:
            started = datetime.fromtimestamp(main.create_time(), tz=timezone.utc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            started = None
        processes[role] = ProcStats(
            role=role, pids=pids, rss=rss, cpu_percent=round(cpu / ncpu, 1), threads=threads,
            started=started, restarts=restarts.get(main.pid) if role != "browser" else None,
            count=len(pids),
        )

    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    du = psutil.disk_usage(str(root))
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = None
    host = HostStats(
        os_name=f"{platform.system()} {platform.release()} ({platform.machine()})",
        python=sys.version.split()[0],
        cpu_logical=ncpu,
        cpu_physical=psutil.cpu_count(logical=False) or ncpu,
        cpu_percent=round(host_cpu, 1),
        load_avg=tuple(round(x, 2) for x in load) if load else None,
        ram_total=vm.total, ram_used=vm.total - vm.available, ram_available=vm.available,
        ram_percent=round(vm.percent, 1),
        swap_total=sw.total, swap_used=sw.used, swap_percent=round(sw.percent, 1),
        disk_total=du.total, disk_used=du.used, disk_free=du.free, disk_percent=round(du.percent, 1),
        boot_time=datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc),
    )
    return SystemSnapshot(
        host=host,
        processes=processes,
        service_rss=sum(p.rss for p in processes.values()),
        service_cpu_percent=round(sum(p.cpu_percent for p in processes.values()), 1),
        logs_bytes=_dir_size(root / "logs"),
    )


# ─── База даних ──────────────────────────────────────────────────

def sqlite_path() -> Path | None:
    url = make_url(settings.DATABASE_URL)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        return None
    p = Path(url.database)
    return p if p.is_absolute() else (Path.cwd() / p).resolve()


def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


async def db_stats() -> DbStats:
    tables = [t for t in Base.metadata.sorted_tables]
    counts: dict[str, int] = {}
    async with get_session() as s:
        for t in tables:
            try:
                counts[t.name] = int((await s.execute(select(func.count()).select_from(t))).scalar_one())
            except Exception:  # noqa: BLE001 — таблиці може ще не бути
                counts[t.name] = 0
        last_check = (await s.execute(select(func.max(PriceHistory.recorded_at)))).scalar_one_or_none()
        last_scrape = (await s.execute(select(func.max(ScrapeLog.created_at)))).scalar_one_or_none()
        week_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
        history_week = int((await s.execute(
            select(func.count()).select_from(PriceHistory).where(PriceHistory.recorded_at >= week_ago)
        )).scalar_one())

    sizes: dict[str, int | None] = {t.name: None for t in tables}
    path = sqlite_path()
    dialect = engine.dialect.name
    file_bytes = wal_bytes = free_bytes = 0

    if dialect == "sqlite":
        async with engine.connect() as conn:
            page_size = (await conn.execute(text("PRAGMA page_size"))).scalar_one()
            freelist = (await conn.execute(text("PRAGMA freelist_count"))).scalar_one()
            page_count = (await conn.execute(text("PRAGMA page_count"))).scalar_one()
            free_bytes = int(freelist) * int(page_size)
            try:
                owners = dict((await conn.execute(
                    text("SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table','index')")
                )).all())
                per_obj = (await conn.execute(text("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"))).all()
                for obj, size in per_obj:
                    owner = owners.get(obj, obj)
                    if owner in sizes:
                        sizes[owner] = (sizes[owner] or 0) + int(size or 0)
            except Exception as e:  # noqa: BLE001 — dbstat є не в кожній збірці SQLite
                logger.debug("dbstat_unavailable", error=type(e).__name__)
        if path is not None:
            file_bytes = _file_size(path) or int(page_count) * int(page_size)
            wal_bytes = _file_size(Path(str(path) + "-wal"))
        else:
            file_bytes = int(page_count) * int(page_size)
    elif dialect == "postgresql":
        async with engine.connect() as conn:
            file_bytes = int((await conn.execute(text("SELECT pg_database_size(current_database())"))).scalar_one())
            for t in tables:
                try:
                    sizes[t.name] = int((await conn.execute(
                        text("SELECT pg_total_relation_size(:t)"), {"t": t.name}
                    )).scalar_one())
                except Exception:  # noqa: BLE001
                    pass

    table_stats = sorted(
        (TableStats(t.name, counts.get(t.name, 0), sizes.get(t.name)) for t in tables),
        key=lambda x: (x.bytes or 0, x.rows), reverse=True,
    )

    # Приріст: price_history — основна таблиця, що росте (scrape_log чиститься через 30 днів)
    growth = None
    ph_rows = counts.get(PriceHistory.__tablename__, 0)
    ph_bytes = sizes.get(PriceHistory.__tablename__)
    if ph_rows and ph_bytes:
        growth = int(history_week / 7 * 30 * (ph_bytes / ph_rows))

    return DbStats(
        engine=dialect, path=str(path) if path else None,
        file_bytes=file_bytes, wal_bytes=wal_bytes, free_bytes=free_bytes,
        total_rows=sum(counts.values()), tables=table_stats,
        growth_bytes_per_month=growth,
        last_check_at=last_check, last_scrape_at=last_scrape,
    )


# ─── Форматування ────────────────────────────────────────────────

_UNITS = {"uk": ["Б", "КБ", "МБ", "ГБ", "ТБ"], "en": ["B", "KB", "MB", "GB", "TB"]}


def fmt_bytes(n: int | float | None, lang: str = "uk") -> str:
    if n is None:
        return "—"
    units = _UNITS.get(lang, _UNITS["uk"])
    value = float(n)
    i = 0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1
    s = f"{value:.0f}" if i == 0 or value >= 100 else f"{value:.1f}"
    if lang == "uk":
        s = s.replace(".", ",")
    return f"{s} {units[i]}"


def fmt_duration(seconds: float | None, lang: str = "uk") -> str:
    """Тривалість: '3 д 4 год' / '3 d 4 h', '12 хв' / '12 min', '<1 хв' / '<1 min'."""
    if seconds is None:
        return "—"
    s = max(0, int(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    u = {"uk": ("д", "год", "хв"), "en": ("d", "h", "min")}.get(lang, ("д", "год", "хв"))
    if d:
        return f"{d} {u[0]} {h} {u[1]}"
    if h:
        return f"{h} {u[1]} {m} {u[2]}"
    if m:
        return f"{m} {u[2]}"
    return f"<1 {u[2]}"


def seconds_since(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def level(percent: float | None, warn: float = 70, crit: float = 85) -> str:
    """Рівень навантаження для кольору: good / warning / critical."""
    if percent is None:
        return "good"
    if percent >= crit:
        return "critical"
    if percent >= warn:
        return "warning"
    return "good"
