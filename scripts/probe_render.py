#!/usr/bin/env python
"""
Проба магазинів через headless Chromium (Playwright): чи рятує рендер ті магазини,
які plain httpx не бере (status needs_js або blocked у registry.py).

Для кожного такого запису реєстру робимо ОДИН рендер sample_url через
app.services.scrapers.render.BrowserRenderer (той самий код, що й у боті), проганяємо
extract_product і друкуємо таблицю: домен, старий статус, результат рендеру
(ok / no_price / blocked / timeout / failed), стратегія, назва, ціна, секунди.

Якщо sample_url — головна сторінка (Horoshop-приклади), пробуємо знайти на ній
посилання на товар і відрендерити вже його (ще один рендер).

Фікстури успішних сторінок: tests/fixtures/<domain>.render.html (скорочено: <head> без
скриптів/стилів + ld+json + microdata [+ h1 і цінові елементи для евристики])
та <domain>.render.json ({url, expected_strategy, expected_currency, rendered: true, …}).
Перед записом перевіряємо, що детектор бот-стін scraper._is_bot_wall їх не ловить.

Запуск:
  venv/bin/python scripts/probe_render.py                       # усі needs_js + blocked
  venv/bin/python scripts/probe_render.py --only comfy.ua,allegro.pl
  venv/bin/python scripts/probe_render.py --status blocked --no-fixtures --out /tmp/render.json
  venv/bin/python scripts/probe_render.py --workers 1 --pause 5   # повільніше й ввічливіше

Ввічливість: один рендер на магазин, пауза 3 с між магазинами в кожному воркері,
2 воркери (RENDER_MAX_CONCURRENCY), бюджет 30 с на сторінку. Увесь прогін — ~10–15 хв.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BOT_TOKEN", "0:probe")   # settings вимагає токен; тут він не потрібен

from bs4 import BeautifulSoup  # noqa: E402

from app.services.scrapers import registry  # noqa: E402
from app.services.scrapers.extract import extract_product  # noqa: E402
from app.services.scrapers.render import RenderError, RenderUnavailable, get_renderer, close_renderer  # noqa: E402
from app.services.scrapers.scraper import _is_bot_wall  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
PROBE_DATE = "2026-10-11"
PAGE_TIMEOUT = 30.0
PAUSE_BETWEEN_SHOPS = 3.0
# Посилання на товар на головній (для записів, де sample_url — корінь сайту)
PRODUCT_LINK_RE = re.compile(r"/(p|product|products|tovar|item|goods)/|/[a-z0-9-]+-p\d+|\.html?$", re.I)


@dataclass
class Row:
    domain: str
    name: str
    country: str
    old_status: str
    url: str
    final_url: str = ""
    result: str = "failed"          # ok | no_price | blocked | timeout | failed | not_found
    strategy: str = ""
    title: str | None = None
    price: str | None = None
    currency: str | None = None
    http_status: int | None = None
    challenged: bool = False
    seconds: float = 0.0
    size: int = 0
    notes: list[str] = field(default_factory=list)
    fixture: str | None = None


# ─── Фікстури ────────────────────────────────────────────────────

def minimal_render_fixture(html: str, strategy: str) -> str:
    """
    <head> без <script>/<style>/<link> (крім ld+json) + усі ld+json + microdata-вузли.
    Для евристики додаємо <h1> і до 20 елементів із «price» у class/id — інакше ціна зникне.
    """
    soup = BeautifulSoup(html, "lxml")
    parts = ["<!DOCTYPE html>", "<html>"]
    if soup.head:
        head = soup.head
        for tag in head.find_all(["script", "style", "link", "noscript"]):
            if tag.name == "script" and "ld+json" in (tag.get("type") or ""):
                continue
            tag.decompose()
        parts.append(str(head))
    parts.append("<body>")
    head_scripts = set(map(id, soup.head.find_all("script"))) if soup.head else set()
    for s in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        if id(s) not in head_scripts:
            parts.append(str(s))
    seen: set[int] = set()
    for n in soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/.*Product", re.I)}):
        if id(n) not in seen:
            seen.add(id(n))
            for tag in n.find_all(["script", "style", "svg", "img", "picture", "iframe"]):
                tag.decompose()
            parts.append(str(n))
    if strategy == "heuristic":
        h1 = soup.find("h1")
        if h1:
            parts.append(str(h1))
        count = 0
        for el in soup.find_all(attrs={"class": re.compile(r"price", re.I)}):
            if el.find_parent(attrs={"itemtype": True}) is not None:
                continue
            for tag in el.find_all(["script", "style", "svg", "img"]):
                tag.decompose()
            parts.append(str(el))
            count += 1
            if count >= 20:
                break
    parts += ["</body>", "</html>"]
    return "\n".join(parts)


def save_fixture(row: Row, html: str) -> str | None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    body = minimal_render_fixture(html, row.strategy)
    if _is_bot_wall(body):
        row.notes.append("фікстуру не збережено: детектор бот-стін ловить скорочену сторінку")
        return None
    check = extract_product(body, row.final_url or row.url)
    if check is None or check.price is None or check.price <= 0 or not check.title:
        row.notes.append("фікстуру не збережено: після скорочення ціна/назва губляться")
        return None
    if check.strategy != row.strategy:
        row.notes.append(f"у скороченій фікстурі стратегія {check.strategy} (було {row.strategy})")
    path = FIXTURES / f"{row.domain}.render.html"
    path.write_text(body, encoding="utf-8")
    (FIXTURES / f"{row.domain}.render.json").write_text(json.dumps({
        "url": row.final_url or row.url,
        "expected_strategy": check.strategy,
        "expected_currency": check.currency,
        "title": check.title,
        "price": str(check.price),
        "rendered": True,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(row.seconds, 1),
        "trimmed": True,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path.name


# ─── Проба ───────────────────────────────────────────────────────

def find_product_link(html: str, base: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    host = urlparse(base).netloc
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"]).split("#")[0]
        if urlparse(href).netloc != host or href.rstrip("/") == base.rstrip("/"):
            continue
        if PRODUCT_LINK_RE.search(urlparse(href).path):
            return href
    return None


async def probe_one(p: registry.Platform, save_fixtures: bool) -> Row:
    row = Row(p.domain, p.name, p.country, p.status, p.sample_url)
    if not p.sample_url:
        row.result, row.notes = "failed", ["немає sample_url"]
        return row
    renderer = get_renderer()
    t0 = time.monotonic()
    url = p.sample_url
    try:
        rendered = await renderer.render(url, timeout=PAGE_TIMEOUT)
        if urlparse(url).path in ("", "/") and not _is_bot_wall(rendered.html):
            link = find_product_link(rendered.html, rendered.final_url)
            if link:
                row.notes.append(f"товар знайдено на головній: {link}")
                url = link
                await asyncio.sleep(1.0)
                rendered = await renderer.render(url, timeout=PAGE_TIMEOUT)
            else:
                row.notes.append("sample_url — головна; посилання на товар не знайдено")
    except RenderUnavailable as e:
        row.result, row.notes = "failed", [f"Playwright недоступний: {e}"]
        row.seconds = time.monotonic() - t0
        return row
    except RenderError as e:
        row.result = {"render_timeout": "timeout", "blocked": "blocked"}.get(e.reason, "failed")
        row.notes.append(f"{e.reason}: {e}")
        row.seconds = time.monotonic() - t0
        return row
    except Exception as e:  # noqa: BLE001
        row.result, row.notes = "failed", [f"{type(e).__name__}: {e}"]
        row.seconds = time.monotonic() - t0
        return row

    row.seconds = time.monotonic() - t0
    row.url, row.final_url = url, rendered.final_url
    row.http_status, row.challenged, row.size = rendered.status, rendered.challenged, len(rendered.html)
    html = rendered.html
    if rendered.status in (404, 410):
        row.result = "not_found"
        return row
    if _is_bot_wall(html):
        row.result = "blocked"
        m = re.search(r"<title[^>]*>(.*?)</title>", html[:20000], re.I | re.S)
        row.notes.append(f"бот-стіна після рендеру: title={(m.group(1).strip() if m else '')[:60]!r}")
        return row

    product = extract_product(html, rendered.final_url)
    if product is None:
        row.result = "failed"
        m = re.search(r"<title[^>]*>(.*?)</title>", html[:20000], re.I | re.S)
        row.notes.append(f"нічого не витягли; title={(m.group(1).strip() if m else '')[:60]!r}")
        if rendered.status and rendered.status >= 400:
            row.result = "blocked"
            row.notes.append(f"HTTP {rendered.status}")
        return row
    row.title, row.strategy, row.currency = product.title, product.strategy, product.currency
    row.price = str(product.price) if product.price is not None else None
    if product.price is None or product.price <= 0:
        row.result = "no_price"
        return row
    row.result = "ok"
    if save_fixtures:
        row.fixture = save_fixture(row, html)
    return row


def chromium_footprint() -> str:
    try:
        out = subprocess.run(["ps", "-axo", "rss,%cpu,command"], capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return "n/a"
    rss = cpu = 0.0
    n = 0
    for line in out.splitlines()[1:]:
        if "ms-playwright" in line:
            parts = line.split(None, 2)
            rss += float(parts[0])
            cpu += float(parts[1])
            n += 1
    return f"{n} процесів, RSS {rss / 1024:.0f} MB, CPU {cpu:.0f}%"


def md_table(rows: list[Row]) -> str:
    out = ["| domain | old status | render | strategy | title | price | s | notes |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        title = (r.title or "").replace("|", "/")[:55]
        price = f"{r.price} {r.currency or ''}".strip() if r.price else ""
        notes = "; ".join(filter(None, [f"HTTP {r.http_status}" if r.http_status and r.http_status >= 400 else "",
                                        "челендж пройдено" if r.challenged else "", *r.notes]))
        out.append(f"| {r.domain} | {r.old_status} | {r.result} | {r.strategy} | {title} | {price} | {r.seconds:.0f} | {notes.replace('|', '/')} |")
    return "\n".join(out)


async def main_async(args: argparse.Namespace) -> list[Row]:
    statuses = set(args.status.split(","))
    targets = [p for p in registry.PLATFORMS if p.status in statuses]
    overrides = dict(item.split("=", 1) for item in args.url)
    if args.only or overrides:
        wanted = {d.strip().lower() for d in (args.only or "").split(",") if d.strip()} | set(overrides)
        targets = [p for p in registry.PLATFORMS if p.domain in wanted or set(p.aliases) & wanted]
    targets = [dataclasses.replace(p, sample_url=overrides[p.domain]) if p.domain in overrides else p for p in targets]
    os.environ.setdefault("RENDER_MAX_CONCURRENCY", str(args.workers))

    rows: list[Row] = [None] * len(targets)  # type: ignore[list-item]
    queue: asyncio.Queue[tuple[int, registry.Platform]] = asyncio.Queue()
    for item in enumerate(targets):
        queue.put_nowait(item)
    footprints: list[str] = []

    async def worker() -> None:
        while not queue.empty():
            i, p = queue.get_nowait()
            print(f"[{i + 1}/{len(targets)}] {p.domain} ({p.status}) …", flush=True)
            row = await probe_one(p, save_fixtures=not args.no_fixtures)
            rows[i] = row
            price = f"{row.price} {row.currency or ''}" if row.price else "-"
            print(f"    → {row.result:9s} {row.strategy or '-':10s} {row.seconds:5.1f}s {price:>14s}  {(row.title or '')[:50]!r}"
                  f"{'  ' + '; '.join(row.notes) if row.notes else ''}", flush=True)
            footprints.append(chromium_footprint())
            await asyncio.sleep(args.pause)

    started = time.monotonic()
    try:
        await asyncio.gather(*(worker() for _ in range(max(1, args.workers))))
    finally:
        print(f"\nChromium під час проби (останній замір): {footprints[-1] if footprints else 'n/a'}")
        peak = max((float(f.split('RSS ')[1].split(' MB')[0]) for f in footprints if 'RSS' in f), default=0)
        print(f"Пік RSS за прогін: {peak:.0f} MB; всього {time.monotonic() - started:.0f} с")
        await close_renderer()
    return [r for r in rows if r is not None]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="кома-список доменів")
    ap.add_argument("--url", action="append", default=[], metavar="DOMAIN=URL",
                    help="підмінити sample_url домену (можна кілька разів); домен додається до вибірки")
    ap.add_argument("--status", default="needs_js,blocked", help="які статуси реєстру пробувати")
    ap.add_argument("--no-fixtures", action="store_true")
    ap.add_argument("--out", help="куди записати JSON з результатами")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--pause", type=float, default=PAUSE_BETWEEN_SHOPS)
    args = ap.parse_args()

    rows = asyncio.run(main_async(args))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.result] = counts.get(r.result, 0) + 1
    print("\n" + md_table(rows))
    print("\nПідсумок:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if args.out:
        Path(args.out).write_text(json.dumps([asdict(r) for r in rows], ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
