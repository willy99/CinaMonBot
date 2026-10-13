"""
Час для показу людям — завжди Europe/Kyiv.

У базі всі datetime наївні в UTC (SQLite CURRENT_TIMESTAMP, datetime.utcnow()).
Будь-яке форматування для юзера чи адмінки йде через цей модуль:

    fmt(dt, "%d.%m.%Y %H:%M")   # наївний UTC або aware → рядок за Києвом
    to_kyiv(dt)                 # aware datetime у київському часі
    kyiv_date_key(dt)           # "YYYY-MM-DD" київської доби (для денних графіків)
    now_kyiv()
    utc_range_for_kyiv_days(n)  # (since_naive_utc, [день-ключі]) для групування по днях
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")


def to_kyiv(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KYIV)


def fmt(dt: datetime | None, pattern: str = "%d.%m.%Y %H:%M", default: str = "—") -> str:
    local = to_kyiv(dt)
    return local.strftime(pattern) if local else default


def now_kyiv() -> datetime:
    return datetime.now(KYIV)


def kyiv_date_key(dt: datetime) -> str:
    return to_kyiv(dt).strftime("%Y-%m-%d")


def kyiv_day_start_utc(day: date) -> datetime:
    """Початок київської доби як наївний UTC (для порівнянь із колонками в базі)."""
    local = datetime(day.year, day.month, day.day, tzinfo=KYIV)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def utc_range_for_kyiv_days(days: int) -> tuple[datetime, list[str]]:
    """
    Останні `days` київських діб включно з сьогоднішньою:
    повертає (since — наївний UTC початку першої доби, список ключів "YYYY-MM-DD").
    """
    today = now_kyiv().date()
    first = today - timedelta(days=days - 1)
    keys = [(first + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    return kyiv_day_start_utc(first), keys
