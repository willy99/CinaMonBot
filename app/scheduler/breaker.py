"""
Запобіжник (circuit breaker) на домен магазину: тимчасові збої — антибот (blocked), мережа, браузер —
не вина конкретного товару. Тоді домен стає на паузу з експоненційною затримкою, і трекери не набирають
5 помилок за кілька годин збою (раніше після цього вони назавжди ставали ERROR, і людина мовчки
переставала отримувати сповіщення).

Постійні помилки (404, parse_failed, внутрішня адреса) рахуються як і раніше — до ERROR після ERROR_LIMIT.
Тимчасові теж рахуються, але з порогом TRANSIENT_ERROR_LIMIT: вічно мертве посилання все одно колись вимкнеться.
Стан у пам'яті процесу бота; після рестарту — чистий аркуш.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

TRANSIENT_REASONS = frozenset({"blocked", "max_retries_exceeded", "render_failed", "render_unavailable",
                               "render_timeout", "network", "timeout"})
ERROR_LIMIT = 5
TRANSIENT_ERROR_LIMIT = 30
BASE_SECONDS = 300.0
MAX_SECONDS = 4 * 3600.0


def is_transient(reason: str | None) -> bool:
    return (reason or "") in TRANSIENT_REASONS


@dataclass
class _State:
    failures: int = 0
    open_until: float = 0.0
    reason: str = ""


_state: dict[str, _State] = {}


def reset() -> None:
    _state.clear()


def is_open(domain: str, now: float | None = None) -> bool:
    st = _state.get(domain)
    return bool(st and (time.monotonic() if now is None else now) < st.open_until)


def trip(domain: str, reason: str, now: float | None = None) -> tuple[bool, float]:
    """Рахує тимчасовий збій домену. (чи це перший збій поспіль — для одноразового алерту, пауза в секундах)."""
    now = time.monotonic() if now is None else now
    st = _state.setdefault(domain, _State())
    st.failures += 1
    pause = min(MAX_SECONDS, BASE_SECONDS * 2 ** (st.failures - 1))
    st.open_until, st.reason = now + pause, reason
    return st.failures == 1, pause


def success(domain: str) -> bool:
    """Домен знову відповідає. True — якщо до цього був на паузі."""
    st = _state.pop(domain, None)
    return bool(st and st.failures)


def snapshot(now: float | None = None) -> dict[str, dict]:
    now = time.monotonic() if now is None else now
    return {d: {"paused_s": max(0, int(st.open_until - now)), "failures": st.failures, "reason": st.reason}
            for d, st in _state.items() if st.failures}
