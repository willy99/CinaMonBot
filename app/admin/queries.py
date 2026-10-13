"""
Запити до бази для адмін-дашборду. Тільки тут SQL — сторінки лише малюють.

Усі функції async і працюють через get_session(). Сумісно з SQLite і PostgreSQL
(func.date() є в обох).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, case, desc, func, or_, select

from app.services import runtime_settings as rt
from app.database.models import (
    Feedback, Notification, NotificationStatus,
    PriceHistory, PriceTracker, ScrapeLog, SubscriptionTier, TrackerStatus, User,
)
from app.database.session import get_session
from app.timeutil import fmt, kyiv_date_key, utc_range_for_kyiv_days
from app.services.scrapers import registry


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _naive_utc(dt: datetime) -> datetime:
    """Колонки DateTime без tz; порівнюємо з naive UTC."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _is_premium_expr():
    now = _naive_utc(_now())
    return and_(User.tier == SubscriptionTier.PREMIUM, User.premium_until.isnot(None), User.premium_until > now)


# ─── Dashboard ───────────────────────────────────────────────────

@dataclass
class Overview:
    users_total: int
    users_active: int
    users_blocked: int
    users_premium: int
    users_new_7d: int
    users_new_30d: int
    users_new_prev_7d: int
    trackers_total: int
    trackers_active: int
    trackers_paused: int
    trackers_error: int
    checks_24h: int
    checks_prev_24h: int
    notif_24h: int
    notif_prev_24h: int
    notif_failed_24h: int
    premium_expiring_7d: int
    revenue_month_uah: int


async def overview() -> Overview:
    now = _now()
    n = _naive_utc(now)
    d1, d2, d7, d14, d30 = (
        n - timedelta(days=1), n - timedelta(days=2), n - timedelta(days=7),
        n - timedelta(days=14), n - timedelta(days=30),
    )
    async with get_session() as s:
        async def count(stmt):
            return (await s.execute(stmt)).scalar_one()

        users_total = await count(select(func.count()).select_from(User))
        users_blocked = await count(select(func.count()).select_from(User).where(User.is_blocked == True))
        users_premium = await count(select(func.count()).select_from(User).where(_is_premium_expr()))
        users_new_7d = await count(select(func.count()).select_from(User).where(User.created_at >= d7))
        users_new_prev_7d = await count(
            select(func.count()).select_from(User).where(User.created_at >= d14, User.created_at < d7))
        users_new_30d = await count(select(func.count()).select_from(User).where(User.created_at >= d30))

        by_status = dict((await s.execute(
            select(PriceTracker.status, func.count()).group_by(PriceTracker.status)
        )).all())

        checks_24h = await count(select(func.count()).select_from(PriceHistory).where(PriceHistory.recorded_at >= d1))
        checks_prev = await count(select(func.count()).select_from(PriceHistory)
                                  .where(PriceHistory.recorded_at >= d2, PriceHistory.recorded_at < d1))
        notif_24h = await count(select(func.count()).select_from(Notification)
                                .where(Notification.created_at >= d1, Notification.status == NotificationStatus.SENT))
        notif_prev = await count(select(func.count()).select_from(Notification)
                                 .where(Notification.created_at >= d2, Notification.created_at < d1,
                                        Notification.status == NotificationStatus.SENT))
        notif_failed = await count(select(func.count()).select_from(Notification)
                                   .where(Notification.created_at >= d1, Notification.status == NotificationStatus.FAILED))
        expiring = await count(select(func.count()).select_from(User)
                               .where(_is_premium_expr(), User.premium_until <= n + timedelta(days=7)))

    return Overview(
        users_total=users_total,
        users_active=users_total - users_blocked,
        users_blocked=users_blocked,
        users_premium=users_premium,
        users_new_7d=users_new_7d,
        users_new_30d=users_new_30d,
        users_new_prev_7d=users_new_prev_7d,
        trackers_total=sum(by_status.values()),
        trackers_active=by_status.get(TrackerStatus.ACTIVE, 0),
        trackers_paused=by_status.get(TrackerStatus.PAUSED, 0) + by_status.get(TrackerStatus.PAUSED_LIMIT, 0),
        trackers_error=by_status.get(TrackerStatus.ERROR, 0),
        checks_24h=checks_24h,
        checks_prev_24h=checks_prev,
        notif_24h=notif_24h,
        notif_prev_24h=notif_prev,
        notif_failed_24h=notif_failed,
        premium_expiring_7d=expiring,
        revenue_month_uah=users_premium * rt.cached("PREMIUM_PRICE_UAH"),
    )


async def daily_series(days: int = 30) -> dict[str, list[tuple[str, int]]]:
    """Три ряди по київських добах: нові юзери, перевірки цін, надіслані сповіщення. Дні без даних — 0."""
    since, day_keys = utc_range_for_kyiv_days(days)

    def bucket(timestamps) -> dict[str, int]:
        out: dict[str, int] = {}
        for ts in timestamps:
            if ts is None:
                continue
            k = kyiv_date_key(ts)
            out[k] = out.get(k, 0) + 1
        return out

    async with get_session() as s:
        users = bucket((await s.execute(
            select(User.created_at).where(User.created_at >= since)
        )).scalars().all())
        checks = bucket((await s.execute(
            select(PriceHistory.recorded_at).where(PriceHistory.recorded_at >= since)
        )).scalars().all())
        notifs = bucket((await s.execute(
            select(Notification.created_at)
            .where(Notification.created_at >= since, Notification.status == NotificationStatus.SENT)
        )).scalars().all())

    return {
        "users": [(d, users.get(d, 0)) for d in day_keys],
        "checks": [(d, checks.get(d, 0)) for d in day_keys],
        "notifications": [(d, notifs.get(d, 0)) for d in day_keys],
    }


async def breakdowns() -> dict[str, list[tuple[str, int]]]:
    async with get_session() as s:
        by_domain = (await s.execute(
            select(PriceTracker.source, func.count())
            .where(PriceTracker.status == TrackerStatus.ACTIVE)
            .group_by(PriceTracker.source).order_by(desc(func.count()))
        )).all()
        by_lang = (await s.execute(
            select(User.language_code, func.count()).group_by(User.language_code).order_by(desc(func.count()))
        )).all()
        by_currency = (await s.execute(
            select(PriceTracker.currency, func.count())
            .where(PriceTracker.status == TrackerStatus.ACTIVE)
            .group_by(PriceTracker.currency).order_by(desc(func.count()))
        )).all()
        by_event = (await s.execute(
            select(Notification.event_type, func.count())
            .where(Notification.created_at >= _naive_utc(_now()) - timedelta(days=30))
            .group_by(Notification.event_type).order_by(desc(func.count()))
        )).all()
    return {
        "domain": [(str(k), int(v)) for k, v in by_domain],
        "language": [(str(k or "—"), int(v)) for k, v in by_lang],
        "currency": [(str(k or "UAH"), int(v)) for k, v in by_currency],
        "event": [(str(k), int(v)) for k, v in by_event],
    }


async def premium_expiring(days: int = 14) -> list[dict]:
    n = _naive_utc(_now())
    async with get_session() as s:
        rows = (await s.execute(
            select(User).where(_is_premium_expr(), User.premium_until <= n + timedelta(days=days))
            .order_by(User.premium_until.asc())
        )).scalars().all()
    return [{
        "id": u.id, "name": u.first_name or "—", "username": u.username or "",
        "telegram_id": u.telegram_id,
        "until": fmt(u.premium_until, "%d.%m.%Y", "—"),
        "days_left": max(0, (u.premium_until - n).days) if u.premium_until else 0,
    } for u in rows]


async def problem_trackers(limit: int = 10) -> list[dict]:
    async with get_session() as s:
        rows = (await s.execute(
            select(PriceTracker, User.first_name, User.username)
            .join(User, User.id == PriceTracker.user_id)
            .where(or_(PriceTracker.status == TrackerStatus.ERROR, PriceTracker.consecutive_errors >= 2))
            .order_by(PriceTracker.consecutive_errors.desc(), PriceTracker.last_checked_at.desc())
            .limit(limit)
        )).all()
    return [{
        "id": t.id, "user_id": t.user_id,
        "title": (t.title or "Без назви")[:60], "source": t.source,
        "status": t.status, "errors": t.consecutive_errors,
        "last_checked": fmt(t.last_checked_at, "%d.%m %H:%M", "—"),
        "user": f"{name or ''} @{uname}" if uname else (name or "—"),
    } for t, name, uname in rows]


async def top_products(limit: int = 8) -> list[tuple[str, int]]:
    async with get_session() as s:
        rows = (await s.execute(
            select(PriceTracker.canonical_url, func.max(PriceTracker.title), func.count())
            .where(PriceTracker.status == TrackerStatus.ACTIVE)
            .group_by(PriceTracker.canonical_url).order_by(desc(func.count())).limit(limit)
        )).all()
    return [((title or url)[:50], int(c)) for url, title, c in rows]


# ─── Users ───────────────────────────────────────────────────────

async def users_list(search: str = "", tier: str = "all", status: str = "all") -> list[dict]:
    n = _naive_utc(_now())
    trackers_sub = (
        select(PriceTracker.user_id, func.count().label("cnt"),
               func.sum(case((PriceTracker.status == TrackerStatus.ACTIVE, 1), else_=0)).label("active"))
        .group_by(PriceTracker.user_id).subquery()
    )
    stmt = (
        select(User, func.coalesce(trackers_sub.c.cnt, 0), func.coalesce(trackers_sub.c.active, 0))
        .outerjoin(trackers_sub, trackers_sub.c.user_id == User.id)
        .order_by(User.created_at.desc())
    )
    if search:
        q = f"%{search.strip()}%"
        conds = [User.first_name.ilike(q), User.username.ilike(q)]
        if search.strip().isdigit():
            conds.append(User.telegram_id == int(search.strip()))
        stmt = stmt.where(or_(*conds))
    if tier == "premium":
        stmt = stmt.where(_is_premium_expr())
    elif tier == "free":
        stmt = stmt.where(~_is_premium_expr())
    if status == "blocked":
        stmt = stmt.where(User.is_blocked == True)
    elif status == "active":
        stmt = stmt.where(User.is_blocked == False)

    async with get_session() as s:
        rows = (await s.execute(stmt)).all()

    out = []
    for u, cnt, active in rows:
        premium = bool(u.tier == SubscriptionTier.PREMIUM and u.premium_until and u.premium_until > n)
        out.append({
            "id": u.id,
            "name": u.first_name or "—",
            "username": f"@{u.username}" if u.username else "",
            "telegram_id": u.telegram_id,
            "lang": u.language_code or "—",
            "tier": "premium" if premium else "free",
            "until": (fmt(u.premium_until, "%d.%m.%Y") if premium else ""),
            "blocked": u.is_blocked,
            "trackers": int(cnt),
            "trackers_active": int(active),
            "created": fmt(u.created_at, "%d.%m.%Y", "—"),
            "created_sort": u.created_at.isoformat() if u.created_at else "",
        })
    return out


async def user_detail(user_id: str) -> dict | None:
    async with get_session() as s:
        u = await s.get(User, user_id)
        if not u:
            return None
        trackers = (await s.execute(
            select(PriceTracker).where(PriceTracker.user_id == user_id)
            .order_by(PriceTracker.created_at.desc())
        )).scalars().all()
        notifs = (await s.execute(
            select(Notification, PriceTracker.title)
            .join(PriceTracker, PriceTracker.id == Notification.tracker_id)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc()).limit(30)
        )).all()
    n = _naive_utc(_now())
    premium = bool(u.tier == SubscriptionTier.PREMIUM and u.premium_until and u.premium_until > n)
    return {
        "id": u.id,
        "telegram_id": u.telegram_id,
        "name": u.first_name or "—",
        "username": u.username or "",
        "lang": u.language_code or "—",
        "tier": "premium" if premium else "free",
        "premium_until": u.premium_until,
        "premium_until_str": fmt(u.premium_until, "%d.%m.%Y", "—"),
        "blocked": u.is_blocked,
        "digest_enabled": bool(u.digest_enabled),
        "last_digest_at": u.last_digest_at,
        "created": fmt(u.created_at, "%d.%m.%Y %H:%M", "—"),
        "trackers": [{
            "id": t.id,
            "title": t.title or "Без назви",
            "url": t.canonical_url,
            "source": t.source,
            "price": t.current_price,
            "currency": t.currency or "UAH",
            "target": t.target_price,
            "target_percent": t.target_percent,
            "baseline_price": t.baseline_price,
            "status": t.status,
            "errors": t.consecutive_errors,
            "available": t.is_available,
            "notify_drop": t.notify_drop,
            "notify_restock": t.notify_restock,
            "last_checked": fmt(t.last_checked_at, "%d.%m %H:%M", "—"),
            "created": fmt(t.created_at, "%d.%m.%Y", "—"),
        } for t in trackers],
        "notifications": [{
            "when": fmt(nf.created_at, "%d.%m %H:%M", "—"),
            "event": nf.event_type,
            "status": nf.status,
            "old": nf.old_price,
            "new": nf.new_price,
            "title": (title or "Без назви")[:50],
            "error": nf.error_message or "",
        } for nf, title in notifs],
    }


async def tracker_history(tracker_id: str, limit: int = 300) -> list[tuple[str, float]]:
    async with get_session() as s:
        rows = (await s.execute(
            select(PriceHistory.recorded_at, PriceHistory.price)
            .where(PriceHistory.tracker_id == tracker_id)
            .order_by(PriceHistory.recorded_at.asc()).limit(limit)
        )).all()
    return [(fmt(dt, "%Y-%m-%d %H:%M"), float(p)) for dt, p in rows]


async def set_premium(user_id: str, days: int) -> datetime | None:
    """Продовжує Premium (від поточної дати закінчення або від сьогодні) і вмикає товари з паузи через ліміт."""
    from app.services.limits import extend_premium, sync_free_limit
    async with get_session() as s:
        u = await s.get(User, user_id)
        if not u:
            return None
        until = extend_premium(u, days)
    await sync_free_limit(user_id)
    return until


async def revoke_premium(user_id: str) -> bool:
    """Знімає Premium; зайві товари йдуть на паузу через ліміт."""
    from app.services.limits import sync_free_limit
    async with get_session() as s:
        u = await s.get(User, user_id)
        if not u:
            return False
        u.tier = SubscriptionTier.FREE
        u.premium_until = None
    await sync_free_limit(user_id)
    return True


async def set_blocked(user_id: str, blocked: bool) -> bool:
    async with get_session() as s:
        u = await s.get(User, user_id)
        if not u:
            return False
        u.is_blocked = blocked
    return True


async def set_tracker_status(tracker_id: str, status: TrackerStatus) -> bool:
    async with get_session() as s:
        t = await s.get(PriceTracker, tracker_id)
        if not t:
            return False
        t.status = status
        if status == TrackerStatus.ACTIVE:
            t.consecutive_errors = 0
    return True


# ─── Trackers ────────────────────────────────────────────────────

async def trackers_list(status: str = "all", source: str = "all", search: str = "") -> list[dict]:
    stmt = (
        select(PriceTracker, User.first_name, User.username, User.telegram_id)
        .join(User, User.id == PriceTracker.user_id)
        .order_by(PriceTracker.created_at.desc())
    )
    if status == "paused":
        stmt = stmt.where(PriceTracker.status.in_([TrackerStatus.PAUSED, TrackerStatus.PAUSED_LIMIT]))
    elif status != "all":
        stmt = stmt.where(PriceTracker.status == status)
    if source != "all":
        stmt = stmt.where(PriceTracker.source == source)
    if search:
        stmt = stmt.where(PriceTracker.title.ilike(f"%{search.strip()}%"))
    async with get_session() as s:
        rows = (await s.execute(stmt)).all()
        sources = [r[0] for r in (await s.execute(
            select(PriceTracker.source).distinct().order_by(PriceTracker.source)
        )).all()]
    return [{
        "id": t.id,
        "user_id": t.user_id,
        "title": (t.title or "Без назви")[:70],
        "url": t.canonical_url,
        "source": t.source,
        "price": float(t.current_price) if t.current_price is not None else None,
        "currency": t.currency or "UAH",
        "status": t.status,
        "errors": t.consecutive_errors,
        "last_checked": fmt(t.last_checked_at, "%d.%m %H:%M", "—"),
        "user": f"{name or ''} @{uname}".strip() if uname else (name or str(tid)),
        "created": fmt(t.created_at, "%d.%m.%Y", "—"),
    } for t, name, uname, tid in rows], sources


# ─── Admins ──────────────────────────────────────────────────────

# Запити про адмінів і журнал дій — botkit.admin.queries (спільний пакет)


# ─── Platforms ───────────────────────────────────────────────────
#
# Домени з БД (PriceTracker.source, ScrapeLog.domain) зводяться до ключа реєстру
# (Platform.domain) через platform_for(): домен + аліаси + піддомени. Домени,
# яких у реєстрі немає, стають окремими рядками зі статусом "other" ("інші сайти").

PLATFORM_STATUSES = ("verified", "experimental", "needs_js", "blocked")


@dataclass
class PlatformsOverview:
    verified: int
    experimental: int
    needs_js: int
    blocked: int
    other_domains: int          # домени з БД, яких немає в реєстрі
    checks_24h: int             # спроб парсингу за 24 год (ScrapeLog)
    success_rate_24h: float | None   # 0..100, None якщо спроб не було
    healed_30d: int


def _norm_host(host: str) -> str:
    host = (host or "").lower().strip()
    return host[4:] if host.startswith("www.") else host


def _platform_key(domain: str) -> str:
    """Домен із БД → ключ реєстру або сам нормалізований домен."""
    p = registry.platform_for(domain)
    return p.domain if p else _norm_host(domain)


def _domain_cond(column, patterns: tuple[str, ...]):
    """SQL-умова: колонка дорівнює одному з доменів або є його піддоменом."""
    return or_(*[or_(column == d, column.like(f"%.{d}")) for d in patterns])


def _as_dt(v) -> datetime | None:
    """max(case(...)) у SQLite повертає рядок — приводимо до datetime."""
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _pct(fails: int, total: int) -> float | None:
    return round(fails / total * 100, 1) if total else None


async def _tracker_stats(s) -> dict[str, dict]:
    """{key: {"trackers", "active", "error", "paused", "users"}} по ключах реєстру."""
    out: dict[str, dict] = {}

    def bucket(key: str) -> dict:
        return out.setdefault(key, {"trackers": 0, "active": 0, "error": 0, "paused": 0, "_users": set()})

    rows = (await s.execute(
        select(PriceTracker.source, PriceTracker.status, func.count())
        .group_by(PriceTracker.source, PriceTracker.status)
    )).all()
    for source, status, cnt in rows:
        b = bucket(_platform_key(source))
        b["trackers"] += int(cnt)
        if status == TrackerStatus.ACTIVE:
            b["active"] += int(cnt)
        elif status == TrackerStatus.ERROR:
            b["error"] += int(cnt)
        elif status in (TrackerStatus.PAUSED, TrackerStatus.PAUSED_LIMIT):
            b["paused"] += int(cnt)

    pairs = (await s.execute(select(PriceTracker.source, PriceTracker.user_id).distinct())).all()
    for source, user_id in pairs:
        bucket(_platform_key(source))["_users"].add(user_id)

    for b in out.values():
        b["users"] = len(b.pop("_users"))
    return out


async def _scrape_stats(s, since: datetime) -> dict[str, dict]:
    """{key: {"checks", "fails", "healed", "avg_ms", "last_ok"}} за період від since."""
    rows = (await s.execute(
        select(
            ScrapeLog.domain,
            func.count(),
            func.sum(case((ScrapeLog.ok == True, 0), else_=1)),
            func.sum(case((ScrapeLog.healed == True, 1), else_=0)),
            func.sum(ScrapeLog.duration_ms),
            func.max(case((ScrapeLog.ok == True, ScrapeLog.created_at))),
        )
        .where(ScrapeLog.created_at >= since)
        .group_by(ScrapeLog.domain)
    )).all()
    out: dict[str, dict] = {}
    for domain, checks, fails, healed, dur, last_ok in rows:
        b = out.setdefault(_platform_key(domain), {"checks": 0, "fails": 0, "healed": 0, "_dur": 0, "last_ok": None})
        b["checks"] += int(checks or 0)
        b["fails"] += int(fails or 0)
        b["healed"] += int(healed or 0)
        b["_dur"] += int(dur or 0)
        dt = _as_dt(last_ok)
        if dt and (b["last_ok"] is None or dt > b["last_ok"]):
            b["last_ok"] = dt
    for b in out.values():
        b["avg_ms"] = round(b.pop("_dur") / b["checks"]) if b["checks"] else None
    return out


async def _last_rows(s, ok: bool) -> dict[str, tuple[datetime, str]]:
    """Останній запис (ok=True → стратегія, ok=False → причина) по кожному ключу."""
    sub = (
        select(ScrapeLog.domain, func.max(ScrapeLog.created_at).label("mx"))
        .where(ScrapeLog.ok == ok).group_by(ScrapeLog.domain).subquery()
    )
    rows = (await s.execute(
        select(ScrapeLog.domain, ScrapeLog.created_at, ScrapeLog.strategy if ok else ScrapeLog.reason)
        .join(sub, and_(ScrapeLog.domain == sub.c.domain, ScrapeLog.created_at == sub.c.mx))
    )).all()
    out: dict[str, tuple[datetime, str]] = {}
    for domain, dt, val in rows:
        key = _platform_key(domain)
        dt = _as_dt(dt)
        if dt and (key not in out or dt > out[key][0]):
            out[key] = (dt, val or "")
    return out


def _fmt_dt(dt: datetime | None) -> str:
    return fmt(dt, "%d.%m %H:%M", "—")


async def _platform_rows() -> list[dict]:
    """Усі рядки таблиці платформ без фільтрів; відсортовано: активні трекери ↓, назва."""
    n = _naive_utc(_now())
    async with get_session() as s:
        tr = await _tracker_stats(s)
        sc30 = await _scrape_stats(s, n - timedelta(days=30))
        sc24 = await _scrape_stats(s, n - timedelta(days=1))
        last_fail = await _last_rows(s, ok=False)
        last_ok = await _last_rows(s, ok=True)

    keys = {p.domain for p in registry.PLATFORMS} | set(tr) | set(sc30)
    empty_tr = {"trackers": 0, "active": 0, "error": 0, "paused": 0, "users": 0}
    empty_sc = {"checks": 0, "fails": 0, "healed": 0, "avg_ms": None, "last_ok": None}
    rows = []
    for key in keys:
        p = registry.platform_for(key)
        t_ = tr.get(key, empty_tr)
        s30 = sc30.get(key, empty_sc)
        s24 = sc24.get(key, empty_sc)
        observed_strategy = last_ok.get(key, (None, ""))[1]
        rows.append({
            "id": key,
            "domain": key,
            "name": p.name if p else key,
            "country": p.country if p else "",
            "status": p.status if p else "other",
            "strategy": (p.strategy if p and p.strategy else observed_strategy) or "",
            "notes": p.notes if p else "",
            "sample_url": p.sample_url if p else "",
            "aliases": list(p.aliases) if p else [],
            "trackers": t_["trackers"], "active": t_["active"], "error": t_["error"], "paused": t_["paused"],
            "users": t_["users"],
            "checks_30d": s30["checks"], "fails_30d": s30["fails"],
            "error_rate_30d": _pct(s30["fails"], s30["checks"]),
            "healed_30d": s30["healed"], "avg_ms": s30["avg_ms"],
            "last_ok": _fmt_dt(s30["last_ok"]),
            "last_ok_sort": s30["last_ok"].isoformat() if s30["last_ok"] else "",
            "last_fail_reason": last_fail.get(key, (None, ""))[1],
            "last_fail_at": _fmt_dt(last_fail.get(key, (None, None))[0]),
            "checks_24h": s24["checks"], "fails_24h": s24["fails"],
            "error_rate_24h": _pct(s24["fails"], s24["checks"]),
            "healed_24h": s24["healed"],
        })
    rows.sort(key=lambda r: (-r["active"], r["name"].lower()))
    return rows


async def platforms_overview() -> PlatformsOverview:
    n = _naive_utc(_now())
    d1, d30 = n - timedelta(days=1), n - timedelta(days=30)
    by_status = {st: 0 for st in PLATFORM_STATUSES}
    for p in registry.PLATFORMS:
        by_status[p.status] = by_status.get(p.status, 0) + 1

    async with get_session() as s:
        sources = [r[0] for r in (await s.execute(select(PriceTracker.source).distinct())).all()]
        logged = [r[0] for r in (await s.execute(select(ScrapeLog.domain).distinct())).all()]
        checks_24h, ok_24h = (await s.execute(
            select(func.count(), func.sum(case((ScrapeLog.ok == True, 1), else_=0)))
            .where(ScrapeLog.created_at >= d1)
        )).one()
        healed_30d = (await s.execute(
            select(func.count()).select_from(ScrapeLog)
            .where(ScrapeLog.created_at >= d30, ScrapeLog.healed == True)
        )).scalar_one()

    other = {_norm_host(d) for d in (*sources, *logged) if d and registry.platform_for(d) is None}
    checks_24h = int(checks_24h or 0)
    return PlatformsOverview(
        verified=by_status["verified"],
        experimental=by_status["experimental"],
        needs_js=by_status["needs_js"],
        blocked=by_status["blocked"],
        other_domains=len(other),
        checks_24h=checks_24h,
        success_rate_24h=round(int(ok_24h or 0) / checks_24h * 100, 1) if checks_24h else None,
        healed_30d=int(healed_30d),
    )


async def platforms_table(status: str = "all", country: str = "all", search: str = "") -> list[dict]:
    """status: all | verified | experimental | needs_js | blocked | other. country: all | ISO-2."""
    rows = await _platform_rows()
    if status != "all":
        rows = [r for r in rows if r["status"] == status]
    if country != "all":
        rows = [r for r in rows if r["country"] == country]
    if search:
        needle = search.strip().lower()
        rows = [r for r in rows if needle in r["name"].lower() or needle in r["domain"].lower()
                or any(needle in a for a in r["aliases"])]
    return rows


async def platform_detail(domain: str, days: int = 30, recent: int = 20) -> dict:
    """Картка платформи: довідник + денні ok/fail за N днів + останні спроби парсингу."""
    p = registry.platform_for(domain)
    patterns = (p.domain, *p.aliases) if p else (_norm_host(domain),)
    cond = _domain_cond(ScrapeLog.domain, patterns)

    # Доби — київські (у базі UTC), тому групуємо в Python
    since, day_keys = utc_range_for_kyiv_days(days)

    async with get_session() as s:
        daily = (await s.execute(
            select(ScrapeLog.created_at, ScrapeLog.ok)
            .where(cond, ScrapeLog.created_at >= since)
        )).all()
        last = (await s.execute(
            select(ScrapeLog).where(cond).order_by(ScrapeLog.created_at.desc()).limit(recent)
        )).scalars().all()

    ok_map: dict[str, int] = {}
    fail_map: dict[str, int] = {}
    for ts, ok in daily:
        if ts is None:
            continue
        k = kyiv_date_key(ts)
        target = ok_map if ok else fail_map
        target[k] = target.get(k, 0) + 1

    return {
        "domain": p.domain if p else _norm_host(domain),
        "name": p.name if p else _norm_host(domain),
        "status": p.status if p else "other",
        "country": p.country if p else "",
        "strategy": p.strategy if p else "",
        "notes": p.notes if p else "",
        "sample_url": p.sample_url if p else "",
        "aliases": list(p.aliases) if p else [],
        "daily_ok": [(d, ok_map.get(d, 0)) for d in day_keys],
        "daily_fail": [(d, fail_map.get(d, 0)) for d in day_keys],
        "recent": [{
            "id": r.id,
            "when": _fmt_dt(r.created_at),
            "domain": r.domain,
            "ok": bool(r.ok),
            "strategy": r.strategy or "",
            "reason": r.reason or "",
            "healed": bool(r.healed),
            "duration_ms": int(r.duration_ms or 0),
        } for r in last],
    }


async def platform_problems_24h(min_attempts: int = 3, min_rate: float = 30.0) -> list[dict]:
    """Платформи з часткою невдач ≥ min_rate % за 24 год при ≥ min_attempts спробах."""
    rows = await _platform_rows()
    out = [{
        "id": r["domain"], "domain": r["domain"], "name": r["name"], "status": r["status"],
        "checks": r["checks_24h"], "fails": r["fails_24h"], "rate": r["error_rate_24h"],
        "healed": r["healed_24h"], "last_fail_reason": r["last_fail_reason"],
    } for r in rows
        if r["checks_24h"] >= min_attempts and (r["error_rate_24h"] or 0) >= min_rate]
    out.sort(key=lambda r: (-(r["rate"] or 0), -r["checks"]))
    return out


async def platforms_top(limit: int = 8) -> list[dict]:
    """Топ платформ за активними трекерами для дашборду (лише ті, де є трекери)."""
    rows = await _platform_rows()
    return [r for r in rows if r["trackers"] > 0][:limit]


# ─── Feedback ────────────────────────────────────────────────────

FEEDBACK_STATUSES = ("new", "read", "replied")


def _feedback_row(f: Feedback) -> dict:
    return {
        "id": f.id,
        "user_id": f.user_id,
        "telegram_id": f.telegram_id,
        "name": f.first_name or "—",
        "username": f.username or "",
        "lang": f.language_code or "—",
        "text": f.text or "",
        "content_type": f.content_type or "text",
        "file_id": f.file_id,
        "file_name": f.file_name or "",
        "mime_type": f.mime_type or "",
        "status": f.status,
        "reply_text": f.reply_text or "",
        "replied_by": f.replied_by or "",
        "replied_at": fmt(f.replied_at, "%d.%m.%Y %H:%M", ""),
        "forwarded": f.forwarded_message_id is not None,
        "when": fmt(f.created_at, "%d.%m.%Y %H:%M", "—"),
        "created_sort": f.created_at.isoformat() if f.created_at else "",
    }


async def feedback_list(status: str = "all", search: str = "", limit: int = 200) -> list[dict]:
    """status: "all" | "new" | "read" | "replied" | "unreplied" (new + read)."""
    stmt = select(Feedback).order_by(Feedback.created_at.desc(), Feedback.id.desc()).limit(limit)
    if status == "unreplied":
        stmt = stmt.where(Feedback.status.in_(("new", "read")))
    elif status in FEEDBACK_STATUSES:
        stmt = stmt.where(Feedback.status == status)
    if search and search.strip():
        q = f"%{search.strip()}%"
        conds = [Feedback.first_name.ilike(q), Feedback.username.ilike(q), Feedback.text.ilike(q)]
        if search.strip().isdigit():
            conds.append(Feedback.telegram_id == int(search.strip()))
        stmt = stmt.where(or_(*conds))
    async with get_session() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return [_feedback_row(f) for f in rows]


async def feedback_counts() -> dict[str, int]:
    async with get_session() as s:
        rows = (await s.execute(
            select(Feedback.status, func.count()).group_by(Feedback.status)
        )).all()
    counts = {k: 0 for k in FEEDBACK_STATUSES}
    for status, n in rows:
        counts[status] = counts.get(status, 0) + int(n)
    counts["total"] = sum(int(n) for _, n in rows)
    return counts


async def feedback_get(feedback_id: str) -> dict | None:
    async with get_session() as s:
        f = await s.get(Feedback, feedback_id)
    return _feedback_row(f) if f else None


async def feedback_mark(feedback_id: str, status: str) -> bool:
    if status not in FEEDBACK_STATUSES:
        raise ValueError(f"unknown feedback status: {status}")
    async with get_session() as s:
        f = await s.get(Feedback, feedback_id)
        if not f:
            return False
        f.status = status
    return True


async def feedback_reply_saved(feedback_id: str, text: str, replied_by: str) -> bool:
    """Фіксує вже надіслану відповідь: статус replied + текст + хто і коли."""
    async with get_session() as s:
        f = await s.get(Feedback, feedback_id)
        if not f:
            return False
        f.status = "replied"
        f.reply_text = text
        f.replied_at = _naive_utc(_now())
        f.replied_by = replied_by
    return True


async def feedback_user_history(telegram_id: int, limit: int = 20) -> list[dict]:
    async with get_session() as s:
        rows = (await s.execute(
            select(Feedback).where(Feedback.telegram_id == telegram_id)
            .order_by(Feedback.created_at.desc(), Feedback.id.desc()).limit(limit)
        )).scalars().all()
    return [_feedback_row(f) for f in rows]


# ─── Журнали з пейджингом (сповіщення, перевірки цін) ────────────

PAGE_SIZE = 25

NOTIF_EVENT_LABELS = {
    "decrease": "Зниження ціни", "target_reached": "Досягнута ціль",
    "restock": "Знову в наявності", "increase": "Підвищення",
}


async def notifications_page(page: int = 1, per_page: int = PAGE_SIZE, *, event: str = "all",
                             status: str = "all", search: str = "") -> tuple[list[dict], int]:
    """Сторінка сповіщень від найновіших. Повертає (рядки, всього після фільтрів)."""
    page = max(1, int(page or 1))
    base = (
        select(Notification, PriceTracker.title, PriceTracker.currency, PriceTracker.source,
               PriceTracker.canonical_url, User.first_name, User.username, User.telegram_id, User.id)
        .join(PriceTracker, PriceTracker.id == Notification.tracker_id)
        .join(User, User.id == Notification.user_id)
    )
    conds = []
    if event != "all":
        conds.append(Notification.event_type == event)
    if status != "all":
        conds.append(Notification.status == status)
    if search:
        q = f"%{search.strip()}%"
        sub = [PriceTracker.title.ilike(q), User.first_name.ilike(q), User.username.ilike(q)]
        if search.strip().isdigit():
            sub.append(User.telegram_id == int(search.strip()))
        conds.append(or_(*sub))
    if conds:
        base = base.where(and_(*conds))

    async with get_session() as s:
        total = (await s.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
        rows = (await s.execute(
            base.order_by(Notification.created_at.desc(), Notification.id.desc())
            .offset((page - 1) * per_page).limit(per_page)
        )).all()

    out = []
    for nf, title, currency, source, url, name, uname, tid, uid in rows:
        out.append({
            "id": nf.id,
            "when": fmt(nf.created_at, "%d.%m.%Y %H:%M", "—"),
            "event": nf.event_type,
            "event_label": NOTIF_EVENT_LABELS.get(nf.event_type, nf.event_type),
            "status": nf.status,
            "old": float(nf.old_price) if nf.old_price is not None else None,
            "new": float(nf.new_price) if nf.new_price is not None else None,
            "currency": currency or "UAH",
            "title": (title or "Без назви")[:80],
            "source": source,
            "url": url,
            "user": name or "—",
            "username": uname or "",
            "telegram_id": tid,
            "user_id": uid,
            "error": nf.error_message or "",
        })
    return out, int(total)


async def checks_page(page: int = 1, per_page: int = PAGE_SIZE, *, source: str = "all",
                      search: str = "") -> tuple[list[dict], int]:
    """Сторінка перевірок цін (price_history) від найновіших."""
    page = max(1, int(page or 1))
    base = (
        select(PriceHistory, PriceTracker.title, PriceTracker.currency, PriceTracker.source,
               PriceTracker.canonical_url, PriceTracker.user_id, User.first_name, User.username)
        .join(PriceTracker, PriceTracker.id == PriceHistory.tracker_id)
        .join(User, User.id == PriceTracker.user_id)
    )
    conds = []
    if source != "all":
        conds.append(PriceTracker.source == source)
    if search:
        q = f"%{search.strip()}%"
        conds.append(or_(PriceTracker.title.ilike(q), User.first_name.ilike(q), User.username.ilike(q)))
    if conds:
        base = base.where(and_(*conds))

    async with get_session() as s:
        total = (await s.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
        rows = (await s.execute(
            base.order_by(PriceHistory.recorded_at.desc(), PriceHistory.id.desc())
            .offset((page - 1) * per_page).limit(per_page)
        )).all()
        sources = [r[0] for r in (await s.execute(
            select(PriceTracker.source).distinct().order_by(PriceTracker.source)
        )).all()]

    out = [{
        "id": h.id,
        "when": fmt(h.recorded_at, "%d.%m.%Y %H:%M", "—"),
        "price": float(h.price),
        "currency": currency or "UAH",
        "available": h.is_available,
        "title": (title or "Без назви")[:80],
        "source": source_,
        "url": url,
        "user_id": uid,
        "user": f"{name or ''} @{uname}".strip() if uname else (name or "—"),
    } for h, title, currency, source_, url, uid, name, uname in rows]
    return out, int(total), sources
