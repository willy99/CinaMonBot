"""
Ліміт частоти: ковзне вікно в пам'яті процесу. Для Telegram-бота
(app/bot/middlewares.py). Ключ — довільний рядок («user:<id>», «tg:<id>», …).
"""
from __future__ import annotations

import time
from collections import deque

_hits: dict[str, deque[float]] = {}
_calls = 0
_SWEEP_EVERY = 1000


class RateLimited(Exception):
    def __init__(self, retry_after: int, lang: str = "uk") -> None:
        self.retry_after, self.lang = retry_after, lang


def reset() -> None:
    _hits.clear()


def hit(key: str, limit: int, per_seconds: float, now: float | None = None) -> None:
    """Рахує запит; кидає RateLimited, якщо за останні per_seconds їх уже limit."""
    global _calls
    now = time.monotonic() if now is None else now
    q = _hits.setdefault(key, deque())
    while q and now - q[0] >= per_seconds:
        q.popleft()
    if len(q) >= limit:
        raise RateLimited(max(1, int(per_seconds - (now - q[0])) + 1))
    q.append(now)
    _calls += 1
    if _calls % _SWEEP_EVERY == 0:
        _sweep(now)


def _sweep(now: float, max_window: float = 3600) -> None:
    for k in [k for k, q in _hits.items() if not q or now - q[-1] >= max_window]:
        _hits.pop(k, None)
