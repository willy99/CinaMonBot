"""
Запобіжник (circuit breaker) на ключ (перевізник, домен магазину): загальні збої джерела — капча, 429,
мережа, браузер — не вина конкретного запису. Тоді ключ стає на паузу з експоненційною затримкою
(base × 2^(n-1), до max), а успіх його закриває. Стан у пам'яті процесу.

    UZ = Breaker(base_seconds=60, max_seconds=1800)
    first, pause = UZ.trip("uz", "HumanCheckRequired", base_seconds=300)
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _State:
    failures: int = 0
    open_until: float = 0.0
    reason: str = ""


class Breaker:
    def __init__(self, base_seconds: float, max_seconds: float) -> None:
        self.base_seconds, self.max_seconds = base_seconds, max_seconds
        self._state: dict[str, _State] = {}

    def reset(self) -> None:
        self._state.clear()

    def is_open(self, key: str, now: float | None = None) -> bool:
        st = self._state.get(key)
        return bool(st and (time.monotonic() if now is None else now) < st.open_until)

    def trip(self, key: str, reason: str, now: float | None = None,
             base_seconds: float | None = None) -> tuple[bool, float]:
        """Рахує збій. (чи це перший збій поспіль — для одноразового алерту, пауза в секундах)."""
        now = time.monotonic() if now is None else now
        st = self._state.setdefault(key, _State())
        st.failures += 1
        base = self.base_seconds if base_seconds is None else base_seconds
        pause = min(self.max_seconds, base * 2 ** (st.failures - 1))
        st.open_until, st.reason = now + pause, reason
        return st.failures == 1, pause

    def restore(self, key: str, failures: int, paused_s: float, reason: str = "", now: float | None = None) -> None:
        """Повернути стан після рестарту процесу (напр. зі snapshot() у heartbeat): серія збоїв триває,
        тож наступний trip() — не «перший» (без повторного алерту), а пауза росте далі, а не з base."""
        if failures <= 0:
            return
        now = time.monotonic() if now is None else now
        self._state[key] = _State(failures=int(failures), open_until=now + max(0.0, paused_s), reason=reason)

    def success(self, key: str) -> bool:
        """Джерело знову відповідає. True — якщо до цього було на паузі."""
        st = self._state.pop(key, None)
        return bool(st and st.failures)

    def snapshot(self, now: float | None = None) -> dict[str, dict]:
        now = time.monotonic() if now is None else now
        return {k: {"paused_s": max(0, int(st.open_until - now)), "failures": st.failures, "reason": st.reason}
                for k, st in self._state.items() if st.failures}
