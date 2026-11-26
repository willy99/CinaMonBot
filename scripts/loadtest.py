"""
Load test for the price-check pipeline: synthetic users and trackers, fake shops, fake Telegram.

Runs the REAL dispatcher (app.scheduler.price_checker) in real time against a scratch SQLite DB
and records how requests spread over shops, how late checks get, and what happens to notifications.

No real network, ever:
  * every socket connect / DNS lookup to anything but localhost raises (counted in the results);
  * shops are an in-process httpx transport (latency, rate limits, 403/429 like a real antibot);
  * Telegram is a fake bot that enforces Telegram's flood limits (30 msg/s per bot, ~1 msg/s per chat);
  * the AI selector healer and the Playwright renderer are switched off.

    venv/bin/python scripts/loadtest.py --db /tmp/lt/steady.db --scenario steady --users 1000 \
        --minutes 15 --out /tmp/lt/steady-1000.json

Scenarios:
  steady       trackers' last checks spread evenly over their interval (normal day)
  backlog      every tracker last checked 3 days ago (recovery after the host was down)
  blackfriday  steady + at minute 3 the popular products drop 25 % (notification storm)
  strict       steady + the biggest shop answers 429 above 10 req/min, another one 403s from minute 2
  digest       steady seed + 8-day price history, then one weekly digest run (Monday 10:00 job)

Do not run it from the project directory with the live .env: the script sets DATABASE_URL and
BOT_TOKEN itself and refuses to touch an existing database file.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import socket
import subprocess
import sys
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ─── CLI first: settings are read from the environment at import time ───────────────────

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--db", required=True, help="scratch SQLite file to create (must not exist)")
p.add_argument("--scenario", choices=["steady", "backlog", "blackfriday", "strict", "digest"], default="steady")
p.add_argument("--users", type=int, default=100)
p.add_argument("--premium-share", type=float, default=0.10)
p.add_argument("--shared-share", type=float, default=0.25, help="share of trackers on popular (shared) products")
p.add_argument("--minutes", type=float, default=15)
p.add_argument("--seed", type=int, default=20261125)
p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a setting (env)")
p.add_argument("--out", required=True)
ARGS = p.parse_args()

db_path = Path(ARGS.db).resolve()
if db_path.exists():
    sys.exit(f"{db_path} already exists — the load test only works on a fresh scratch database")
db_path.parent.mkdir(parents=True, exist_ok=True)
os.environ.update({
    "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
    "BOT_TOKEN": "0:loadtest",
    "ADMIN_TELEGRAM_ID": "0",
    "ANTHROPIC_API_KEY": "",
    "HEALTHCHECK_PING_URL": "",
    "ENVIRONMENT": "production",   # only switches SQL echo off
})
for kv in ARGS.set:
    k, _, v = kv.partition("=")
    os.environ[k] = v
os.chdir(db_path.parent)            # never pick up the project's .env
random.seed(ARGS.seed)

# ─── Network guard: nothing leaves this machine ──────────────────────────────────────────

BLOCKED: Counter[str] = Counter()
_LOCAL = {"127.0.0.1", "::1", "localhost", None}
_real_connect, _real_connect_ex, _real_gai = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo


def _check(addr) -> None:
    host = addr[0] if isinstance(addr, tuple) else None
    if isinstance(addr, tuple) and host not in _LOCAL:
        BLOCKED[str(host)] += 1
        raise ConnectionRefusedError(f"loadtest: real network blocked ({host})")


def _connect(self, addr):
    _check(addr)
    return _real_connect(self, addr)


def _connect_ex(self, addr):
    _check(addr)
    return _real_connect_ex(self, addr)


def _getaddrinfo(host, *a, **kw):
    if host not in _LOCAL:
        BLOCKED[str(host)] += 1
        raise socket.gaierror(f"loadtest: DNS blocked ({host})")
    return _real_gai(host, *a, **kw)


socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = _connect, _connect_ex, _getaddrinfo

# ─── App imports (after env + guard) ─────────────────────────────────────────────────────

import httpx  # noqa: E402
from aiogram.exceptions import TelegramRetryAfter  # noqa: E402
from aiogram.methods import SendMessage  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.database.models import (  # noqa: E402
    DomainBlock, Notification, PriceHistory, PriceTracker, ScrapeLog, SubscriptionTier, TrackerStatus, User,
)
from app.database.session import get_session  # noqa: E402
from app.scheduler import breaker  # noqa: E402
from app.scheduler import digest as digest_mod  # noqa: E402
from app.scheduler import price_checker as pc  # noqa: E402
from app.services import runtime_settings as rt  # noqa: E402
from app.services.scrapers import scraper as scraper_mod  # noqa: E402

loop_time = time.monotonic
T0 = 0.0


def now_s() -> float:
    return round(loop_time() - T0, 3)


# ─── Fake shops ──────────────────────────────────────────────────────────────────────────

@dataclass
class Shop:
    domain: str
    weight: float                      # share of trackers
    latency_ms: float = 700            # median response time
    limit_per_min: int | None = None   # 429 when more requests than this in the last 60 s
    block_from_min: float | None = None  # 403 to everything from this minute on (antibot)
    window: deque = field(default_factory=deque, repr=False)


SHOPS = {s.domain: s for s in [
    Shop("rozetka.test", 0.35, 900),
    Shop("prom.test", 0.18, 1400),
    Shop("olx.test", 0.10, 700),
    Shop("epicentrk.test", 0.08, 1100),
    Shop("comfy.test", 0.06, 1600),
    Shop("foxtrot.test", 0.05, 800),
    Shop("allo.test", 0.05, 900),
    Shop("makeup.test", 0.05, 600),
    Shop("notino.test", 0.04, 1000),
    Shop("kasta.test", 0.04, 1200),
]}
if ARGS.scenario == "strict":
    SHOPS["rozetka.test"].limit_per_min = 10
    SHOPS["makeup.test"].block_from_min = 2


@dataclass
class Product:
    url: str
    domain: str
    title: str
    price: Decimal
    available: bool = True
    hot: bool = False
    bf_done: bool = False


PRODUCTS: dict[str, Product] = {}
REQUESTS: list[tuple[float, str, int, int]] = []       # (sent_at, domain, status, latency_ms)
P_CHANGE, P_STOCK = 0.03, 0.004
BLACK_FRIDAY = {"on": False}


def _page(prod: Product) -> str:
    ld = {
        "@context": "https://schema.org", "@type": "Product", "name": prod.title,
        "image": f"https://{prod.domain}/img/{abs(hash(prod.url)) % 10_000}.jpg",
        "offers": {"@type": "Offer", "price": f"{prod.price:.2f}", "priceCurrency": "UAH",
                   "availability": "https://schema.org/" + ("InStock" if prod.available else "OutOfStock")},
    }
    return (f"<!doctype html><html><head><title>{prod.title}</title>"
            f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>'
            f"</head><body><h1>{prod.title}</h1><p>{'x' * 20_000}</p></body></html>")


def _drift(prod: Product) -> None:
    if BLACK_FRIDAY["on"] and prod.hot and not prod.bf_done:
        prod.price = (prod.price * Decimal("0.75")).quantize(Decimal("0.01"))
        prod.bf_done = True
        return
    r = random.random()
    if r < P_CHANGE:
        prod.price = (prod.price * Decimal(str(round(random.uniform(0.88, 1.10), 3)))).quantize(Decimal("0.01"))
    elif r < P_CHANGE + P_STOCK:
        prod.available = not prod.available


async def shop_handler(request: httpx.Request) -> httpx.Response:
    t = loop_time()
    shop = SHOPS.get(request.url.host)
    if shop is None:
        REQUESTS.append((now_s(), request.url.host, 599, 0))
        return httpx.Response(404, text="unknown shop")
    while shop.window and t - shop.window[0] > 60:
        shop.window.popleft()
    shop.window.append(t)
    if shop.block_from_min is not None and (t - T0) / 60 >= shop.block_from_min:
        await asyncio.sleep(0.15)
        status, body = 403, "<html><title>Access denied</title></html>"
    elif shop.limit_per_min is not None and len(shop.window) > shop.limit_per_min:
        await asyncio.sleep(0.1)
        status, body = 429, "Too Many Requests"
    else:
        await asyncio.sleep(random.lognormvariate(0, 0.5) * shop.latency_ms / 1000)
        prod = PRODUCTS.get(str(request.url))
        if prod is None:
            status, body = 404, "not found"
        else:
            _drift(prod)
            status, body = 200, _page(prod)
    REQUESTS.append((round(t - T0, 3), shop.domain, status, int((loop_time() - t) * 1000)))   # sent at, latency
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


# ─── Fake Telegram ───────────────────────────────────────────────────────────────────────

TG: list[tuple[float, str, int, bool]] = []            # (t, kind, chat_id, ok)


class FakeBot:
    """Telegram flood limits: ≤30 messages per second per bot, ~1 per second per chat (burst of 3)."""
    GLOBAL_PER_SEC, CHAT_BURST = 30, 3

    def __init__(self) -> None:
        self._global: deque[float] = deque()
        self._chat: dict[int, deque[float]] = defaultdict(deque)
        self._ids = 0

    async def send_message(self, chat_id: int, text: str, **kw):
        await asyncio.sleep(random.uniform(0.08, 0.25))     # Bot API round trip
        t = loop_time()
        for q, span in ((self._global, 1.0), (self._chat[chat_id], 1.0)):
            while q and t - q[0] > span:
                q.popleft()
        kind = "digest" if kw.get("disable_web_page_preview") else "notification"
        if len(self._global) >= self.GLOBAL_PER_SEC or len(self._chat[chat_id]) >= self.CHAT_BURST:
            TG.append((now_s(), kind, chat_id, False))
            raise TelegramRetryAfter(method=SendMessage(chat_id=chat_id, text="."), message="Too Many Requests",
                                     retry_after=1 if len(self._global) < self.GLOBAL_PER_SEC else 3)
        self._global.append(t)
        self._chat[chat_id].append(t)
        TG.append((now_s(), kind, chat_id, True))
        self._ids += 1
        return SimpleNamespace(message_id=self._ids)

    def __getattr__(self, name):                       # edit_message_*, answer_* … — just succeed
        async def ok(*a, **kw):
            return SimpleNamespace(message_id=0)
        return ok


# ─── Seed ────────────────────────────────────────────────────────────────────────────────

def _pick_shop() -> Shop:
    r, acc = random.random(), 0.0
    for s in SHOPS.values():
        acc += s.weight
        if r <= acc:
            return s
    return s


def _new_product(hot: bool = False) -> Product:
    shop = _pick_shop()
    pid = len(PRODUCTS) + 1
    prod = Product(url=f"https://{shop.domain}/p/{pid}/", domain=shop.domain, title=f"Товар {pid} ({shop.domain})",
                   price=Decimal(random.randint(199, 49_999)), hot=hot)
    PRODUCTS[prod.url] = prod
    return prod


async def seed() -> dict:
    subprocess.run([sys.executable, "-m", "app.database.migrate"], cwd=db_path.parent, check=True,
                   env={**os.environ, "PYTHONPATH": str(ROOT)}, stdout=subprocess.DEVNULL)
    hot = [_new_product(hot=True) for _ in range(max(20, ARGS.users // 5))]
    hot_weights = [1 / (i + 1) for i in range(len(hot))]           # Zipf: a few products are very popular
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    free_iv = int(rt.cached("FREE_TIER_CHECK_INTERVAL_MINUTES"))
    prem_iv = int(rt.cached("PREMIUM_CHECK_INTERVAL_MINUTES"))
    users = trackers = premium = 0
    async with get_session() as s:
        for i in range(ARGS.users):
            is_prem = random.random() < ARGS.premium_share
            u = User(telegram_id=9_000_000_000 + i, first_name=f"LT{i}", language_code="uk",
                     tier=SubscriptionTier.PREMIUM if is_prem else SubscriptionTier.FREE,
                     premium_until=now + timedelta(days=30) if is_prem else None)
            s.add(u)
            await s.flush()
            users += 1
            premium += is_prem
            n = random.randint(3, 25) if is_prem else random.choices([1, 2, 3, 4, 5], [25, 20, 20, 15, 20])[0]
            interval = prem_iv if is_prem else free_iv
            seen: set[str] = set()
            for _ in range(n):
                prod = (random.choices(hot, hot_weights)[0] if random.random() < ARGS.shared_share
                        else _new_product())
                if prod.url in seen:
                    continue
                seen.add(prod.url)
                if ARGS.scenario == "backlog":
                    last = now - timedelta(days=3)
                else:
                    last = now - timedelta(minutes=random.uniform(0, interval))
                tr = PriceTracker(user_id=u.id, url=prod.url, canonical_url=prod.url, source=prod.domain,
                                  title=prod.title, current_price=prod.price, currency="UAH", is_available=True,
                                  status=TrackerStatus.ACTIVE, last_checked_at=last,
                                  target_price=(prod.price * Decimal("0.9")).quantize(Decimal("1"))
                                  if random.random() < 0.3 else None)
                s.add(tr)
                trackers += 1
                if ARGS.scenario == "digest":
                    await s.flush()
                    old = (prod.price * Decimal(str(random.uniform(1.0, 1.3)))).quantize(Decimal("0.01"))
                    s.add(PriceHistory(tracker_id=tr.id, price=old, is_available=True,
                                       recorded_at=now - timedelta(days=8)))
                    s.add(PriceHistory(tracker_id=tr.id, price=prod.price, is_available=True,
                                       recorded_at=now - timedelta(hours=random.uniform(1, 48))))
    shops = Counter(p.domain for p in PRODUCTS.values())
    return {"users": users, "premium_users": premium, "trackers": trackers, "unique_urls": len(PRODUCTS),
            "hot_products": len(hot), "urls_per_shop": dict(shops),
            "free_interval_min": free_iv, "premium_interval_min": prem_iv}


# ─── Instrumentation ─────────────────────────────────────────────────────────────────────

CHECKS: list[tuple[float, float, str, float]] = []     # (start, end, domain, lateness_s)
RUNS: list[dict] = []
SAMPLES: list[dict] = []
_real_check = pc.check_single_price


async def _timed_check(tracker, bot):
    t = now_s()
    iv = pc._check_interval_minutes(tracker, datetime.now(timezone.utc).replace(tzinfo=None)) * 60
    last = tracker.last_checked_at
    late = (datetime.now(timezone.utc).replace(tzinfo=None) - last).total_seconds() - iv if last else 0.0
    try:
        await _real_check(tracker, bot)
    finally:
        CHECKS.append((t, now_s(), tracker.source, round(late, 1)))


def _rss_mb() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024


async def sampler(stop: asyncio.Event) -> None:
    free_iv = int(rt.cached("FREE_TIER_CHECK_INTERVAL_MINUTES")) * 60
    prem_iv = int(rt.cached("PREMIUM_CHECK_INTERVAL_MINUTES")) * 60
    cpu0, wall0 = time.process_time(), loop_time()
    while not stop.is_set():
        t_db = loop_time()
        async with get_session() as s:
            rows = (await s.execute(
                select(PriceTracker.last_checked_at, User.tier, User.premium_until)
                .join(User, User.id == PriceTracker.user_id)
                .where(PriceTracker.status == TrackerStatus.ACTIVE)
            )).all()
        db_ms = (loop_time() - t_db) * 1000
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        overdue, worst = 0, 0.0
        for last, tier, until in rows:
            iv = prem_iv if tier != SubscriptionTier.FREE and until and until > now else free_iv
            late = (now - last).total_seconds() - iv if last else 10 ** 9
            if late > 60:                                 # more than a tick late
                overdue += 1
                worst = max(worst, late)
        cpu1, wall1 = time.process_time(), loop_time()
        SAMPLES.append({"t": now_s(), "active": len(rows), "overdue": overdue, "worst_late_min": round(worst / 60, 1),
                        "rss_mb": round(_rss_mb(), 1), "cpu_pct": round(100 * (cpu1 - cpu0) / max(wall1 - wall0, 1e-6), 1),
                        "db_query_ms": round(db_ms, 1), "breaker_open": sorted(breaker.snapshot())})
        cpu0, wall0 = cpu1, wall1
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass


async def summary(meta: dict) -> dict:
    async with get_session() as s:
        notif = dict((await s.execute(select(Notification.status, func.count()).group_by(Notification.status))).all())
        statuses = dict((await s.execute(select(PriceTracker.status, func.count()).group_by(PriceTracker.status))).all())
        blocks = [d for (d,) in (await s.execute(select(DomainBlock.domain))).all()]
        scrape = [list(r) for r in (await s.execute(
            select(ScrapeLog.domain, ScrapeLog.ok, ScrapeLog.reason, func.count())
            .group_by(ScrapeLog.domain, ScrapeLog.ok, ScrapeLog.reason))).all()]
    return {"notifications": {str(k): v for k, v in notif.items()},
            "tracker_status": {str(k): v for k, v in statuses.items()},
            "domain_blocks": blocks, "scrape_log": scrape}


# ─── Main ────────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    global T0
    meta = await seed()
    print(f"seeded: {meta}", flush=True)

    # Shops through the fake transport; SSRF DNS check, renderer and the paid AI healer off
    scraper_mod._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(shop_handler), timeout=scraper_mod.settings.REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True, headers=scraper_mod.BROWSER_HEADERS)

    async def _no_dns(url):
        return None
    scraper_mod.netguard.check_url = _no_dns
    scraper_mod.render_fallback_enabled = lambda: False
    healer_calls = Counter()

    async def _no_heal(domain, html):
        healer_calls[domain] += 1
        return None
    scraper_mod._auto_healer.find_selectors = _no_heal
    pc.check_single_price = _timed_check

    bot = FakeBot()
    T0 = loop_time()
    stop = asyncio.Event()
    sampler_task = asyncio.create_task(sampler(stop))
    running: asyncio.Task | None = None
    skipped = 0

    async def run_dispatch() -> None:
        start, n0 = now_s(), len(CHECKS)
        await pc.dispatch_price_checks(bot)
        RUNS.append({"start": start, "duration_s": round(now_s() - start, 1), "checks": len(CHECKS) - n0})

    digest_result = None
    if ARGS.scenario == "digest":
        t = now_s()
        n0 = len(TG)
        sent = await digest_mod.send_weekly_digests(bot)
        digest_result = {"duration_s": round(now_s() - t, 1), "sent": sent,
                         "tg_failed": sum(1 for x in TG[n0:] if not x[3])}
        print(f"digest: {digest_result}", flush=True)

    end = T0 + ARGS.minutes * 60
    tick = 0
    while loop_time() < end:
        if ARGS.scenario == "blackfriday" and tick == 3:
            BLACK_FRIDAY["on"] = True
            print("black friday: popular products drop 25 %", flush=True)
        if running is not None and not running.done():
            skipped += 1                                   # APScheduler max_instances=1 + coalesce
        else:
            running = asyncio.create_task(run_dispatch())
        tick += 1
        done = len(CHECKS)
        print(f"[{now_s() / 60:5.1f} min] checks={done} requests={len(REQUESTS)} runs={len(RUNS)} "
              f"skipped_ticks={skipped} overdue={SAMPLES[-1]['overdue'] if SAMPLES else '?'}", flush=True)
        await asyncio.sleep(max(0.0, T0 + tick * 60 - loop_time()))
    if running is not None and not running.done():
        try:
            await asyncio.wait_for(running, timeout=180)
        except asyncio.TimeoutError:
            running.cancel()
    stop.set()
    await sampler_task

    result = {
        "scenario": ARGS.scenario, "args": vars(ARGS), "seed": meta,
        "settings": {k: rt.cached(k) for k in ("FREE_TIER_CHECK_INTERVAL_MINUTES", "PREMIUM_CHECK_INTERVAL_MINUTES",
                                               "DOMAIN_MAX_CHECKS_PER_MINUTE", "SCRAPE_DELAY_SECONDS",
                                               "SCRAPE_MAX_RETRIES")},
        "max_checks_per_run": pc.MAX_CHECKS_PER_RUN,
        "shops": {d: {k: v for k, v in asdict(s).items() if k != "window"} for d, s in SHOPS.items()},
        "skipped_ticks": skipped, "runs": RUNS, "samples": SAMPLES,
        "requests": REQUESTS, "checks": CHECKS, "telegram": TG, "digest": digest_result,
        "network_blocked": dict(BLOCKED), "healer_calls": dict(healer_calls),
        "end": await summary(meta),
    }
    Path(ARGS.out).parent.mkdir(parents=True, exist_ok=True)
    Path(ARGS.out).write_text(json.dumps(result, default=str))
    tg_fail = sum(1 for x in TG if not x[3])
    print(f"done: checks={len(CHECKS)} requests={len(REQUESTS)} tg_sent={len(TG) - tg_fail} tg_failed={tg_fail} "
          f"network_blocked={sum(BLOCKED.values())} → {ARGS.out}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
