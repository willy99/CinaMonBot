"""
CLI для адмінки.

    python -m app.admin.cli create-admin <логін> [--telegram-id 123456789]
    python -m app.admin.cli list-admins
    python -m app.admin.cli purge-bogus-failed-notifications [--apply]
    python -m app.admin.cli digest-preview <telegram_id>   # як виглядатиме дайджест зараз (без відправки)

Пароль запитується інтерактивно (не йде в історію шелу).
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from datetime import timedelta

from sqlalchemy import delete, select

from app.admin import auth
from app.admin import queries as q
from app.database.models import Notification, NotificationStatus, PriceHistory, PriceTracker, User
from app.database.migrate import ensure_schema, migrate
from app.database.session import get_session

# Старий баг логування: кожен SENT-рядок мав FAILED-двійника з такою помилкою
BOGUS_ERROR_MARKER = "_make_filtering_bound_logger"
# "Двійник" — FAILED поруч із SENT по тому ж трекеру і типу події в межах N секунд
BOGUS_TWIN_WINDOW_SECONDS = 10


async def find_bogus_failed_notifications() -> list[Notification]:
    """
    FAILED-рядки з error_message LIKE '%_make_filtering_bound_logger%', у яких є SENT-двійник
    (той самий tracker_id + event_type, created_at у межах ±10 с). Лише такі — справжні
    невдалі відправки (інша помилка або без SENT поруч) не чіпаємо.
    """
    async with get_session() as s:
        failed = (await s.execute(
            select(Notification).where(
                Notification.status == NotificationStatus.FAILED,
                Notification.error_message.like(f"%{BOGUS_ERROR_MARKER}%"),
            ).order_by(Notification.created_at)
        )).scalars().all()
        sent = (await s.execute(
            select(Notification).where(Notification.status == NotificationStatus.SENT)
        )).scalars().all()

    sent_by_key: dict[tuple[str, str], list] = {}
    for n in sent:
        sent_by_key.setdefault((n.tracker_id, n.event_type), []).append(n.created_at)

    window = timedelta(seconds=BOGUS_TWIN_WINDOW_SECONDS)
    twins: list[Notification] = []
    for f in failed:
        # LIKE трактує "_" як будь-який символ — перевіряємо маркер ще й у Python
        if BOGUS_ERROR_MARKER not in (f.error_message or ""):
            continue
        for sent_at in sent_by_key.get((f.tracker_id, f.event_type), []):
            if f.created_at is not None and sent_at is not None and abs(f.created_at - sent_at) <= window:
                twins.append(f)
                break
    return twins


async def purge_bogus_failed_notifications(apply: bool) -> int:
    """Dry-run (apply=False) лише рахує; --apply видаляє рівно знайдені двійники. Повертає їх кількість."""
    await ensure_schema()
    twins = await find_bogus_failed_notifications()
    if not twins:
        print("Хибних FAILED-сповіщень (двійників SENT) не знайдено")
        return 0
    for n in twins:
        print(f"  {n.created_at}  tracker={n.tracker_id}  {n.event_type}  id={n.id}")
    if not apply:
        print(f"Знайдено {len(twins)} хибних FAILED-рядків. Це dry-run — додай --apply, щоб видалити")
        return len(twins)
    ids = [n.id for n in twins]
    async with get_session() as s:
        result = await s.execute(delete(Notification).where(Notification.id.in_(ids)))
    print(f"Видалено {result.rowcount} хибних FAILED-рядків")
    return len(twins)


# ─── Артефакти старого бага парсингу копійок ("581,66 грн" → 58166) ──

KOPECKS_FACTOR = 100


async def find_kopecks_artifacts() -> tuple[list[PriceHistory], list[Notification]]:
    """
    Рядки price_history, де ціна у ~100 разів більша за "нормальну" для цього трекера
    (медіана решти рядів), і сповіщення, у яких old_price або new_price дорівнює такому
    значенню. Критерій навмисно жорсткий: ціна ≥ 50× медіани І ціна/100 у межах
    [0.5×мін; 2×макс] нормальних значень.
    """
    from statistics import median
    async with get_session() as s:
        trackers = (await s.execute(select(PriceTracker))).scalars().all()
        bad_rows: list[PriceHistory] = []
        bad_values: dict[str, set] = {}
        for tr in trackers:
            rows = (await s.execute(
                select(PriceHistory).where(PriceHistory.tracker_id == tr.id).order_by(PriceHistory.recorded_at)
            )).scalars().all()
            prices = [float(r.price) for r in rows]
            if len(prices) < 3:
                continue
            med = median(prices)
            normal = [p for p in prices if p < med * 50]
            if not normal or len(normal) == len(prices):
                continue
            lo, hi = min(normal) * 0.5, max(normal) * 2
            for r in rows:
                v = float(r.price)
                if v >= med * 50 and lo <= v / KOPECKS_FACTOR <= hi:
                    bad_rows.append(r)
                    bad_values.setdefault(tr.id, set()).add(round(v, 2))
        bad_notifs: list[Notification] = []
        if bad_values:
            notifs = (await s.execute(
                select(Notification).where(Notification.tracker_id.in_(list(bad_values)))
            )).scalars().all()
            for n in notifs:
                vals = bad_values[n.tracker_id]
                if (n.old_price is not None and round(float(n.old_price), 2) in vals) or \
                   (n.new_price is not None and round(float(n.new_price), 2) in vals):
                    bad_notifs.append(n)
    return bad_rows, bad_notifs


async def fix_kopecks_artifacts(apply: bool) -> int:
    """Dry-run показує; --apply ділить ціну на 100 у price_history і видаляє сповіщення, породжені цими значеннями."""
    await ensure_schema()
    rows, notifs = await find_kopecks_artifacts()
    if not rows:
        print("Артефактів бага з копійками не знайдено")
        return 0
    for r in rows:
        print(f"  history {r.recorded_at:%d.%m.%Y %H:%M}  {float(r.price):>12.2f} → {float(r.price) / KOPECKS_FACTOR:.2f}")
    for n in notifs:
        print(f"  notification {n.created_at:%d.%m.%Y %H:%M}  {n.event_type}  {n.old_price} → {n.new_price}  [{n.status}]  — видалити")
    if not apply:
        print(f"Знайдено: {len(rows)} рядків історії, {len(notifs)} сповіщень. Dry-run — нічого не змінено. Додай --apply.")
        return len(rows)
    async with get_session() as s:
        for r in rows:
            db_row = await s.get(PriceHistory, r.id)
            if db_row is not None:
                db_row.price = (db_row.price / KOPECKS_FACTOR).quantize(db_row.price.__class__("0.01"))
        if notifs:
            await s.execute(delete(Notification).where(Notification.id.in_([n.id for n in notifs])))
    print(f"Виправлено {len(rows)} рядків історії, видалено {len(notifs)} сповіщень")
    return len(rows)


async def digest_preview(telegram_id: int) -> int:
    """
    Друкує дайджест "що подешевшало за тиждень" для юзера так, як його надіслав би планувальник
    у понеділок (HTML-розмітка як є). Нічого не надсилає і last_digest_at не чіпає.
    Повертає 0 (є що показати або тихий тиждень), 1 — юзера не знайдено.
    """
    from app.scheduler.digest import build_digest

    await ensure_schema()
    async with get_session() as s:
        user = (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    if not user:
        print(f"Юзера з Telegram ID {telegram_id} не знайдено", file=sys.stderr)
        return 1
    flag = "увімкнено" if user.digest_enabled else "вимкнено"
    blocked = " · ЗАБЛОКОВАНИЙ — дайджест не отримає" if user.is_blocked else ""
    print(f"Юзер {user.first_name or '—'} (@{user.username or '—'}, {telegram_id}, lang={user.lang}) · "
          f"дайджест: {flag}{blocked} · останній: {user.last_digest_at or 'ніколи'}")
    text = await build_digest(user)
    if not text:
        print("Тихий тиждень — нічого не подешевшало, повідомлення НЕ буде надіслано")
        return 0
    print("─" * 40)
    print(text)
    print("─" * 40)
    return 0


async def _create(username: str, telegram_id: int | None, owner: bool = False) -> int:
    await ensure_schema()
    username = username.strip().lower()
    if await q.admin_exists(username):
        print(f"Адмін {username!r} уже існує", file=sys.stderr)
        return 1
    pw = getpass.getpass("Пароль: ")
    pw2 = getpass.getpass("Ще раз: ")
    if pw != pw2:
        print("Паролі не збігаються", file=sys.stderr)
        return 1
    problem = auth.validate_password_strength(pw)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    role = "owner" if owner or await q.admins_count_active() == 0 else "admin"   # перший адмін — owner
    await q.create_admin(username, auth.hash_password(pw), telegram_id, role=role)
    print(f"Створено адміна {username!r}" + (f" з Telegram ID {telegram_id}" if telegram_id else " без 2FA Telegram ID"))
    return 0


async def _list() -> int:
    await ensure_schema()
    for a in await q.admins_list():
        flag = "активний" if a["active"] else "вимкнений"
        print(f"{a['username']:<20} tg={a['telegram_id'] or '—':<12} {flag:<10} останній вхід: {a['last_login']}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.admin.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-admin", help="створити адміна")
    c.add_argument("username")
    c.add_argument("--telegram-id", type=int, default=None, help="Telegram ID для 2FA-кодів")
    c.add_argument("--owner", action="store_true", help="роль owner (керує адмінами); перший адмін — завжди owner")
    sub.add_parser("list-admins", help="список адмінів")
    p = sub.add_parser("purge-bogus-failed-notifications",
                       help="прибрати FAILED-двійники SENT-сповіщень від старого бага логування (dry-run без --apply)")
    p.add_argument("--apply", action="store_true", help="справді видалити (без прапорця — лише показати)")
    k = sub.add_parser("fix-kopecks-prices",
                       help="виправити ціни ×100 від старого бага парсингу копійок і прибрати породжені ними сповіщення (dry-run без --apply)")
    k.add_argument("--apply", action="store_true")
    d = sub.add_parser("digest-preview", help="показати, яким був би щотижневий дайджест для юзера зараз (без відправки)")
    d.add_argument("telegram_id", type=int)
    args = parser.parse_args()
    migrate()                    # CLI запускають вручну й поодинці — можна одразу довести схему

    if args.cmd == "create-admin":
        sys.exit(asyncio.run(_create(args.username, args.telegram_id, args.owner)))
    if args.cmd == "list-admins":
        sys.exit(asyncio.run(_list()))
    if args.cmd == "purge-bogus-failed-notifications":
        asyncio.run(purge_bogus_failed_notifications(apply=args.apply))
        sys.exit(0)
    if args.cmd == "fix-kopecks-prices":
        asyncio.run(fix_kopecks_artifacts(apply=args.apply))
        sys.exit(0)
    if args.cmd == "digest-preview":
        sys.exit(asyncio.run(digest_preview(args.telegram_id)))


if __name__ == "__main__":
    main()
