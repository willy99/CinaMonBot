"""
Бекап бази. SQLite — онлайн-копія через sqlite3 backup API (безпечно під час роботи бота, WAL) + gzip;
PostgreSQL — pg_dump. Зберігаємо BACKUP_KEEP_DAYS днів у BACKUP_DIR; BACKUP_REMOTE (rsync-ціль, напр.
user@nas:/backups/<проєкт>) — копія поза машиною.

    python ops.py backup            # pm2 «<проєкт>-backup» щодня о 03:15
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

import structlog

from botkit.context import ctx

logger = structlog.get_logger(__name__)


def _sqlite_path(url: str) -> Path:
    return Path(url.split("///", 1)[1])


def backup(now: datetime | None = None) -> Path:
    settings, PREFIX = ctx().settings, ctx().backup_prefix  # noqa: N806
    out_dir = Path(settings.BACKUP_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    if settings.is_sqlite:
        target = out_dir / f"{PREFIX}{stamp}.db.gz"
        raw = out_dir / f".{PREFIX}{stamp}.db.tmp"
        src = sqlite3.connect(_sqlite_path(settings.DATABASE_URL))
        dst = sqlite3.connect(raw)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        with open(raw, "rb") as f_in, gzip.open(target, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        raw.unlink()
    else:
        target = out_dir / f"{PREFIX}{stamp}.sql.gz"
        url = settings.DATABASE_URL.replace("+asyncpg", "")
        with gzip.open(target, "wb") as f_out:
            proc = subprocess.run(["pg_dump", "--no-owner", url], check=True, capture_output=True)
            f_out.write(proc.stdout)
    target.chmod(0o600)
    logger.info("backup_done", file=str(target), bytes=target.stat().st_size)
    return target


def prune(keep_days: int | None = None, now: float | None = None) -> list[Path]:
    settings, PREFIX = ctx().settings, ctx().backup_prefix  # noqa: N806
    keep_days = settings.BACKUP_KEEP_DAYS if keep_days is None else keep_days
    cutoff = (now or time.time()) - keep_days * 86400
    removed = []
    for p in Path(settings.BACKUP_DIR).glob(f"{PREFIX}*.gz"):
        if p.stat().st_mtime < cutoff:
            p.unlink()
            removed.append(p)
    return removed


def push_remote(path: Path) -> bool:
    settings = ctx().settings
    if not settings.BACKUP_REMOTE:
        return False
    subprocess.run(["rsync", "-a", str(path), settings.BACKUP_REMOTE], check=True)
    logger.info("backup_pushed", remote=settings.BACKUP_REMOTE)
    return True


def run() -> int:
    path = backup()
    prune()
    try:
        push_remote(path)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error("backup_push_failed", error=type(e).__name__)
        return 1
    return 0
