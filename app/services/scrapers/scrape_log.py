"""
Запис спроб парсингу в таблицю scrape_log (статистика по платформах у дашборді).

    await record_scrape(domain="comfy.ua", ok=True, strategy="json-ld", duration_ms=412)
    await record_scrape(domain="allo.ua", ok=False, reason="parse_failed", duration_ms=800)
    await record_scrape(domain="prom.ua", ok=True, strategy="selectors", healed=True, duration_ms=3200)

Ніколи не кидає виключень назовні: помилка запису в журнал не має ламати парсинг.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import delete

from app.database.models import ScrapeLog
from app.database.session import get_session

logger = structlog.get_logger(__name__)

RETENTION_DAYS = 30


async def record_scrape(
    domain: str,
    ok: bool,
    *,
    strategy: str | None = None,
    reason: str | None = None,
    healed: bool = False,
    duration_ms: int = 0,
) -> None:
    try:
        async with get_session() as session:
            session.add(ScrapeLog(
                domain=domain[:128],
                ok=ok,
                strategy=(strategy or None),
                reason=(reason or None),
                healed=healed,
                duration_ms=int(duration_ms),
            ))
    except Exception as e:  # noqa: BLE001
        logger.warning("scrape_log_write_failed", domain=domain, error=str(e))


async def prune_scrape_log(days: int = RETENTION_DAYS) -> int:
    """Видаляє записи старіші за N днів. Повертає кількість видалених."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    try:
        async with get_session() as session:
            result = await session.execute(delete(ScrapeLog).where(ScrapeLog.created_at < cutoff))
            return result.rowcount or 0
    except Exception as e:  # noqa: BLE001
        logger.warning("scrape_log_prune_failed", error=str(e))
        return 0
