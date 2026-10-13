"""
Зовнішня перевірка живучості — окремим процесом, тож працює й тоді, коли бот упав чи завис.

Кожні 5 хв (pm2 «<проєкт>-health»): heartbeat планувальника з бази (+ /v1/health API, якщо API_HEALTH_URL).
  - усе добре → пінг HEALTHCHECK_PING_URL (healthchecks.io / Uptime Kuma push), якщо задано;
  - погано → <url>/fail; повідомлення ЛИШЕ адміну (ADMIN_TELEGRAM_ID) напряму через Bot API, і тільки коли
    проблема тримається безперервно ≥ CONFIRM_SECONDS (і ≥ CONFIRM_CHECKS перевірок); нагадування про ту саму
    проблему — дедалі рідше (ALERT_REPEAT_SECONDS × 2^n, до ALERT_REPEAT_MAX_SECONDS), а новий вид проблеми,
    що підтвердився двома перевірками поспіль, — одразу; після сну машини (пауза між перевірками > SLEEP_GAP_SECONDS)
    відлік починається заново;
    «✅ знову працює» — лише якщо перед цим була тривога. Свіжий старт бота (ще жодного тіку) — не проблема.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import httpx
import structlog

from botkit.context import ctx
from botkit.ops import heartbeat

logger = structlog.get_logger(__name__)
STATE_FILE: Path | None = None        # None → <root>/logs/.health_state.json
ALERT_REPEAT_SECONDS = 3600          # перше нагадування через годину, далі ×2: 2 год, 4 год, …
ALERT_REPEAT_MAX_SECONDS = 12 * 3600
CONFIRM_CHECKS = 2            # і щонайменше дві перевірки з проблемою,
CONFIRM_SECONDS = 600         # і проблема тримається безперервно ≥ 10 хв — лише тоді тривога
SLEEP_GAP_SECONDS = 900       # перевірки не було > 15 хв → машина спала / pm2 стояв: збій рахуємо заново


def _state_file() -> Path:
    return STATE_FILE or ctx().root / "logs" / ".health_state.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {}


def _kind(problem: str) -> str:
    """Вид проблеми без чисел: «uz на паузі вже 6-й раз» і «…7-й раз» — та сама проблема."""
    return re.sub(r"\d+", "#", problem)


def _save_state(state: dict) -> None:
    _state_file().parent.mkdir(parents=True, exist_ok=True)
    _state_file().write_text(json.dumps(state))


async def _problems(client: httpx.AsyncClient) -> list[str]:
    problems = []
    sched = await heartbeat.status()
    # «Жодного тіку» — це щойно запущений бот (перший тік через хвилину), а не збій: не тривожимо
    if sched["seen"] and sched["stale"]:
        problems.append(f"планувальник бота не тікав {sched['age_s']} с")
    for provider, p in (sched.get("info") or {}).get("paused", {}).items():
        if p.get("paused_s", 0) > 0 and p.get("failures", 0) >= 5:
            problems.append(f"{provider} на паузі вже {p['failures']}-й раз ({p.get('reason')})")
    if ctx().settings.API_HEALTH_URL:
        try:
            r = await client.get(ctx().settings.API_HEALTH_URL, timeout=10)
            if r.status_code >= 500 and r.status_code != 503:
                problems.append(f"API відповідає {r.status_code}")
        except httpx.HTTPError as e:
            problems.append(f"API недоступний ({type(e).__name__})")
    return problems


async def _tell_admin(client: httpx.AsyncClient, text: str) -> None:
    if not ctx().settings.ADMIN_TELEGRAM_ID:
        return
    url = f"https://api.telegram.org/bot{ctx().settings.BOT_TOKEN.get_secret_value()}/sendMessage"
    try:
        await client.post(url, json={"chat_id": ctx().settings.ADMIN_TELEGRAM_ID, "text": text}, timeout=10)
    except httpx.HTTPError as e:
        logger.warning("health_alert_failed", error=type(e).__name__)


async def check(client: httpx.AsyncClient | None = None, now: float | None = None) -> list[str]:
    own = client is None
    client = client or httpx.AsyncClient()
    now = time.time() if now is None else now
    try:
        problems = await _problems(client)
        state = _load_state()
        ping = ctx().settings.HEALTHCHECK_PING_URL
        last_run = state.get("last_run_at")
        if last_run is not None and now - last_run > SLEEP_GAP_SECONDS:
            # Ноутбук спав: бот теж стояв, а pm2 після пробудження надолужує пропущені запуски кількома
            # перевірками за секунди. Застарілий heartbeat тут — наслідок сну, а не збою: рахуємо з нуля.
            state.pop("fail_count", None)
            state.pop("first_failure_at", None)
        state["last_run_at"] = now
        if problems:
            if ping:
                await client.post(ping.rstrip("/") + "/fail", content="\n".join(problems), timeout=10)
            state["fail_count"] = state.get("fail_count", 0) + 1
            state.setdefault("first_failure_at", now)
            last = state.get("alerted_at")
            kinds = {_kind(p) for p in problems}
            # Новий вид проблеми (не було в тривогах цієї серії), що тримається вже другу перевірку
            new_kind = bool((kinds - set(state.get("alerted_kinds", []))) & set(state.get("last_kinds", [])))
            repeat = min(ALERT_REPEAT_MAX_SECONDS, ALERT_REPEAT_SECONDS * 2 ** max(0, state.get("alerts", 1) - 1))
            # Тривога — лише для стійкої проблеми: ≥ CONFIRM_CHECKS перевірок і ≥ CONFIRM_SECONDS безперервно
            # (рестарт, разовий збій, пробудження після сну — ні)
            lasting = now - state["first_failure_at"] >= CONFIRM_SECONDS
            if state["fail_count"] >= CONFIRM_CHECKS and lasting and (last is None or new_kind or now - last >= repeat):
                await _tell_admin(client, f"🚨 {ctx().app_name}: " + "; ".join(problems))
                state["alerted_at"] = now
                state["alerts"] = state.get("alerts", 0) + 1
                state["alerted_kinds"] = sorted(set(state.get("alerted_kinds", [])) | kinds)
            state["last_kinds"] = sorted(kinds)
        else:
            if ping:
                await client.get(ping, timeout=10)
            if state.get("alerted_at") is not None:          # «відновився» — лише після справжньої тривоги
                await _tell_admin(client, f"✅ {ctx().app_name} знову працює.")
            state = {"last_run_at": now}
        _save_state(state)
        return problems
    finally:
        if own:
            await client.aclose()


def run(close_engine=None) -> int:
    """close_engine — async-функція проєкту, що закриває пул БД після перевірки (окремий процес)."""
    async def main() -> list[str]:
        try:
            return await check()
        finally:
            if close_engine is not None:
                await close_engine()
    problems = asyncio.run(main())
    logger.info("healthcheck", ok=not problems, problems=problems)
    return 1 if problems else 0
