"""
Запобіжник на домен магазину (логіка — botkit.breaker.Breaker): тимчасові збої — антибот (blocked), мережа,
браузер — не вина конкретного товару. Тоді домен стає на паузу з експоненційною затримкою, і трекери не набирають
5 помилок за кілька годин збою (раніше після цього вони назавжди ставали ERROR, і людина мовчки
переставала отримувати сповіщення).

Постійні помилки (404, parse_failed, внутрішня адреса) рахуються як і раніше — до ERROR після ERROR_LIMIT.
Тимчасові теж рахуються, але з порогом TRANSIENT_ERROR_LIMIT: вічно мертве посилання все одно колись вимкнеться.
Стан у пам'яті процесу бота; після рестарту — чистий аркуш.
"""
from __future__ import annotations

from botkit.breaker import Breaker

TRANSIENT_REASONS = frozenset({"blocked", "max_retries_exceeded", "render_failed", "render_unavailable",
                               "render_timeout", "network", "timeout"})
ERROR_LIMIT = 5
TRANSIENT_ERROR_LIMIT = 30
BASE_SECONDS = 300.0
MAX_SECONDS = 4 * 3600.0

_breaker = Breaker(BASE_SECONDS, MAX_SECONDS)
reset, is_open, trip, success, snapshot = (_breaker.reset, _breaker.is_open, _breaker.trip, _breaker.success,
                                           _breaker.snapshot)


def is_transient(reason: str | None) -> bool:
    return (reason or "") in TRANSIENT_REASONS
