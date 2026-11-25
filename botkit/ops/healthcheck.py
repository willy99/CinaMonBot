"""
Зовнішня перевірка живучості — окремим процесом, тож працює й тоді, коли бот упав чи завис.

Кожні 5 хв (pm2 «<проєкт>-health»): heartbeat планувальника з бази (+ /v1/health API, якщо API_HEALTH_URL).
  - усе добре → пінг HEALTHCHECK_PING_URL (healthchecks.io / Uptime Kuma push), якщо задано;
  - погано → healthchecks.io: POST <url>/fail з переліком проблем; Uptime Kuma (…/api/push/<token>?status=up…):
    той самий URL зі status=down&msg=<проблеми> (22.10: «/fail» у кінці Kuma-URL давав ping=/fail при status=up);
  - монітор недоступний (немає мережі) — лише попередження в лозі: монітор сам побачить тишу.
Результат — у лозі (logs/ops.out.log). У Telegram нічого не шле: 14.10 технічні тривоги адміну прибрано на
прохання власника (спам «планувальник не тікав» / «на паузі вже N-й раз»). Хочете сповіщень — задайте
HEALTHCHECK_PING_URL, і хай про збої повідомляє зовнішній моніторинг. Свіжий старт бота (ще жодного тіку) — не проблема.
"""
from __future__ import annotations

import asyncio
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import structlog

from botkit.context import ctx
from botkit.ops import heartbeat

logger = structlog.get_logger(__name__)


async def _problems(client: httpx.AsyncClient) -> list[str]:
    problems = []
    sched = await heartbeat.status()
    # «Жодного тіку» — це щойно запущений бот (перший тік через хвилину), а не збій
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


def _is_kuma(ping: str) -> bool:
    return "/api/push/" in urlsplit(ping).path


def _kuma_down_url(ping: str, problems: list[str]) -> str:
    """Kuma push-URL зі status=down і msg=<проблеми> (решта параметрів — як були)."""
    parts = urlsplit(ping)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(status="down", msg="; ".join(problems)[:250])
    return urlunsplit(parts._replace(query=urlencode(query)))


async def _ping(client: httpx.AsyncClient, ping: str, problems: list[str]) -> None:
    try:
        if not problems:
            await client.get(ping, timeout=10)
        elif _is_kuma(ping):
            await client.get(_kuma_down_url(ping, problems), timeout=10)
        else:
            await client.post(ping.rstrip("/") + "/fail", content="\n".join(problems), timeout=10)
    except httpx.HTTPError as e:
        logger.warning("health_ping_failed", error=type(e).__name__)


async def check(client: httpx.AsyncClient | None = None) -> list[str]:
    own = client is None
    client = client or httpx.AsyncClient()
    try:
        problems = await _problems(client)
        ping = ctx().settings.HEALTHCHECK_PING_URL
        if ping:
            await _ping(client, ping, problems)
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
