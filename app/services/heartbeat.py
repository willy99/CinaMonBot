"""
Heartbeat планувальника: dispatch_price_checks пише beat("scheduler") кожен тік, healthcheck читає status().

Застарілим вважаємо, якщо тіку не було довше за max(STALE_MIN_SECONDS, STALE_TICKS × TICK_SECONDS).
"""
from __future__ import annotations

import json
from datetime import datetime

from app.database.models import Heartbeat
from app.database.session import get_session

SCHEDULER = "scheduler"
STALE_MIN_SECONDS = 120
STALE_TICKS = 3
TICK_SECONDS = 300            # dispatch_price_checks — кожні 5 хв


async def beat(name: str, **info) -> None:
    async with get_session() as s:
        row = await s.get(Heartbeat, name)
        data = json.dumps(info, ensure_ascii=False) if info else None
        if row is None:
            s.add(Heartbeat(name=name, at=datetime.utcnow(), info=data))
        else:
            row.at, row.info = datetime.utcnow(), data


async def status(name: str = SCHEDULER, now: datetime | None = None) -> dict:
    """{"seen": bool, "age_s": int | None, "stale": bool, "info": dict}."""
    async with get_session() as s:
        row = await s.get(Heartbeat, name)
    if row is None:
        return {"seen": False, "age_s": None, "stale": True, "info": {}}
    age = int(((now or datetime.utcnow()) - row.at).total_seconds())
    limit = max(STALE_MIN_SECONDS, STALE_TICKS * TICK_SECONDS)
    return {"seen": True, "age_s": age, "stale": age > limit, "info": json.loads(row.info) if row.info else {}}
