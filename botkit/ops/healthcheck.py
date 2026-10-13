"""
Зовнішня перевірка живучості — окремим процесом, тож працює й тоді, коли бот упав чи завис.

Кожні 5 хв (pm2 «<проєкт>-health»): heartbeat планувальника з бази (+ /v1/health API, якщо API_HEALTH_URL).
  - усе добре → пінг HEALTHCHECK_PING_URL (healthchecks.io / Uptime Kuma push), якщо задано;
  - погано → <url>/fail; повідомлення ЛИШЕ адміну (ADMIN_TELEGRAM_ID) напряму через Bot API, і тільки коли
    проблема тримається CONFIRM_CHECKS перевірок поспіль, не частіше ніж раз на ALERT_REPEAT_SECONDS;
    «✅ знову працює» — лише якщо перед цим була тривога. Свіжий старт бота (ще жодного тіку) — не проблема.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import structlog

from botkit.context import ctx
from botkit.ops import heartbeat

logger = structlog.get_logger(__name__)
STATE_FILE: Path | None = None        # None → <root>/logs/.health_state.json
ALERT_REPEAT_SECONDS = 3600
CONFIRM_CHECKS = 2            # 2 × 5 хв: тривога лише для стійкої проблеми


def _state_file() -> Path:
    return STATE_FILE or ctx().root / "logs" / ".health_state.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {}


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
        if problems:
            if ping:
                await client.post(ping.rstrip("/") + "/fail", content="\n".join(problems), timeout=10)
            state["fail_count"] = state.get("fail_count", 0) + 1
            last = state.get("alerted_at")
            # Тривога — лише якщо проблема тримається CONFIRM_CHECKS перевірок поспіль (рестарт, разовий збій — ні)
            if state["fail_count"] >= CONFIRM_CHECKS and (last is None or now - last >= ALERT_REPEAT_SECONDS):
                await _tell_admin(client, f"🚨 {ctx().app_name}: " + "; ".join(problems))
                state["alerted_at"] = now
        else:
            if ping:
                await client.get(ping, timeout=10)
            if state.get("alerted_at") is not None:          # «відновився» — лише після справжньої тривоги
                await _tell_admin(client, f"✅ {ctx().app_name} знову працює.")
            state = {}
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
