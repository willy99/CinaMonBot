"""
Heartbeat фонових частин: планувальник проєкту пише beat("scheduler") кожен тік, а API / дашборд /
python ops.py healthcheck читають status().

Застарілим вважаємо, якщо тіку не було довше за max(STALE_MIN_SECONDS, STALE_TICKS × інтервал тіку);
інтервал тіку — Context.heartbeat_tick_seconds (число або async-функція, напр. з runtime-налаштувань).
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime

from botkit.context import ctx

SCHEDULER = "scheduler"
STALE_MIN_SECONDS = 120
STALE_TICKS = 3


async def _tick_seconds() -> int:
    tick = ctx().heartbeat_tick_seconds
    value = tick() if callable(tick) else tick
    return int(await value) if inspect.isawaitable(value) else int(value)


async def beat(name: str, **info) -> None:
    Heartbeat = ctx().models.Heartbeat  # noqa: N806
    async with ctx().get_session() as s:
        row = await s.get(Heartbeat, name)
        data = json.dumps(info, ensure_ascii=False) if info else None
        if row is None:
            s.add(Heartbeat(name=name, at=datetime.utcnow(), info=data))
        else:
            row.at, row.info = datetime.utcnow(), data


async def status(name: str = SCHEDULER, now: datetime | None = None) -> dict:
    """{"seen": bool, "age_s": int | None, "stale": bool, "info": dict}."""
    async with ctx().get_session() as s:
        row = await s.get(ctx().models.Heartbeat, name)
    if row is None:
        return {"seen": False, "age_s": None, "stale": True, "info": {}}
    age = int(((now or datetime.utcnow()) - row.at).total_seconds())
    limit = max(STALE_MIN_SECONDS, STALE_TICKS * await _tick_seconds())
    return {"seen": True, "age_s": age, "stale": age > limit, "info": json.loads(row.info) if row.info else {}}
