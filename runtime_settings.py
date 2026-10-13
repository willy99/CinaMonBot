"""
Налаштування, які адмін міняє з дашборду без рестарту (спільне для проєктів).

Джерело правди — .env / Settings проєкту (значення за замовчуванням). Таблиця app_settings (модель
Context.models.AppSetting: key, value, updated_by, updated_at) зберігає перекриття. Бот і адмінка — різні
процеси, тож обмін іде через базу: процес перечитує перекриття не частіше ніж раз на CACHE_TTL секунд.

    from botkit import runtime_settings as rt
    rt.register({"CHECK_INTERVAL": rt.Spec(int, 60, 86400, "monitoring", "unit_seconds")}, ("monitoring",))
    interval = await rt.get("CHECK_INTERVAL")      # async, з кешем
    interval = rt.cached("CHECK_INTERVAL")         # sync, з кешу (оновлюй refresh() у фоні)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from botkit.context import ctx

CACHE_TTL = 10.0


@dataclass(frozen=True)
class Spec:
    kind: type            # int | float
    min: float
    max: float
    group: str            # "monitoring" | "uz" | "plans"
    unit: str = ""        # ключ i18n для одиниці ("unit_seconds", ...) або ""


# Що можна редагувати — реєструє проєкт: register({"KEY": Spec(int, 1, 100, "group", "unit_i18n_key")}, groups).
# Ключ = ім'я поля Settings; підписи — в i18n адмінки проєкту: setting_<KEY>, setting_<KEY>_help, settings_group_<g>.
EDITABLE: dict[str, Spec] = {}
GROUPS: tuple[str, ...] = ()


def register(specs: dict[str, Spec], groups: tuple[str, ...]) -> None:
    global GROUPS
    EDITABLE.clear()
    EDITABLE.update(specs)
    GROUPS = tuple(groups)


def reset_cache() -> None:
    """Для тестів: наступний get() перечитає перекриття з бази."""
    global _cache_at
    _cache.clear()
    _cache_at = 0.0


_cache: dict[str, Any] = {}
_cache_at = 0.0


def default(name: str) -> Any:
    return getattr(ctx().settings, name)


def coerce(name: str, raw: Any) -> Any:
    """Рядок/число → значення потрібного типу в межах [min, max]. ValueError, якщо не підходить."""
    spec = EDITABLE[name]
    try:
        value = spec.kind(float(raw)) if spec.kind is int else spec.kind(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name}: not a number") from e
    if spec.kind is int and float(raw) != int(float(raw)):
        raise ValueError(f"{name}: must be an integer")
    if not (spec.min <= value <= spec.max):
        raise ValueError(f"{name}: out of range {spec.min}..{spec.max}")
    return value


async def load_overrides() -> dict[str, Any]:
    """Усі перекриття з бази (вже приведені до типу). Невалідні/невідомі ключі ігноруються."""
    async with ctx().get_session() as s:
        rows = (await s.execute(select(ctx().models.AppSetting))).scalars().all()
    out: dict[str, Any] = {}
    for r in rows:
        if r.key in EDITABLE:
            try:
                out[r.key] = coerce(r.key, r.value)
            except ValueError:
                continue
    return out


async def refresh(force: bool = False) -> dict[str, Any]:
    global _cache, _cache_at
    if force or time.monotonic() - _cache_at > CACHE_TTL:
        _cache = await load_overrides()
        _cache_at = time.monotonic()
    return _cache


async def get(name: str) -> Any:
    """Поточне значення: перекриття з бази (кеш CACHE_TTL) або дефолт із .env."""
    if name not in EDITABLE:
        return default(name)
    overrides = await refresh()
    return overrides.get(name, default(name))


def cached(name: str) -> Any:
    """Синхронно, з кешу (для місць без await). Після старту процесу до першого refresh() — дефолт."""
    return _cache.get(name, default(name)) if name in EDITABLE else default(name)


async def set_value(name: str, raw: Any, by: str | None = None) -> Any:
    if name not in EDITABLE:
        raise KeyError(name)
    value = coerce(name, raw)
    async with ctx().get_session() as s:
        row = await s.get(ctx().models.AppSetting, name)
        if row is None:
            s.add(ctx().models.AppSetting(key=name, value=str(value), updated_by=by))
        else:
            row.value, row.updated_by = str(value), by
    await refresh(force=True)
    return value


async def reset_value(name: str) -> None:
    async with ctx().get_session() as s:
        row = await s.get(ctx().models.AppSetting, name)
        if row is not None:
            await s.delete(row)
    await refresh(force=True)


async def snapshot() -> list[dict]:
    """Для сторінки налаштувань: кожне поле з дефолтом, поточним значенням і ким/коли змінено."""
    async with ctx().get_session() as s:
        rows = {r.key: r for r in (await s.execute(select(ctx().models.AppSetting))).scalars().all()}
    overrides = await refresh(force=True)
    out = []
    for name, spec in EDITABLE.items():
        row = rows.get(name)
        out.append({
            "key": name, "group": spec.group, "kind": "int" if spec.kind is int else "float",
            "min": spec.min, "max": spec.max, "unit": spec.unit,
            "default": default(name), "value": overrides.get(name, default(name)),
            "overridden": name in overrides,
            "updated_at": row.updated_at if row else None, "updated_by": row.updated_by if row else None,
        })
    return out
