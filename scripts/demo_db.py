"""
Демо-база для скріншотів і презентацій: вигадані користувачі, справжні магазини й товари.

    DATABASE_URL=sqlite+aiosqlite:////tmp/demo.db DEMO_ADMIN_PASSWORD=... \
        venv/bin/python scripts/demo_db.py

Жодних реальних людей: імена, юзернейми й Telegram ID згенеровані. Ціни й форми
історій — за мотивами справжніх спостережень (кермо MOZA, віскі, BioRepair на Notino).
Детерміновано (random.seed), тож скріншоти відтворювані.
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DAYS = 42
random.seed(20261019)
# Дані — до кінця поточної київської доби: інакше на денних графіках останній день
# (неповний на момент знімка) виглядає як обвал. Для синтетичної демо-бази це прийнятно.
_KYIV = __import__("zoneinfo").ZoneInfo("Europe/Kyiv")
_end_local = datetime.now(_KYIV).replace(hour=23, minute=30, second=0, microsecond=0)
NOW = _end_local.astimezone(timezone.utc).replace(tzinfo=None)
START = NOW - timedelta(days=DAYS)


@dataclass
class Product:
    source: str
    title: str
    price: float
    currency: str = "UAH"
    path: list[tuple[int, float]] | None = None   # (день від START, ціна) — фіксована історія
    flaky_stock: bool = False


CATALOG = [
    # Справжні форми історій (стиснуто в 42 дні)
    Product("rozetka.com.ua", "Кермо і педалі MOZA R5 Racing Bundle for PC", 19499,
            path=[(0, 19499), (9, 25999), (11, 26999), (27, 20999), (36, 26999)]),
    Product("rozetka.com.ua", "Віскі Hankey Bannister 1 л 40%", 449,
            path=[(0, 449), (5, 599), (8, 639), (15, 499), (21, 559), (30, 459), (39, 679)]),
    Product("notino.ua", "Biorepair Total Protective Repair · 75 мл", 294,
            path=[(0, 294), (6, 299), (12, 294), (24, 199), (32, 250)], flaky_stock=True),
    Product("rozetka.com.ua", "Навушники Marshall Major IV Bluetooth Black", 2949,
            path=[(0, 2949), (10, 2861), (18, 2649), (29, 2949), (38, 3249), (41.6, 2899)]),
    Product("rozetka.com.ua", "Кава в зернах Ferarra Caffe 100% Arabica 1 кг", 819,
            path=[(0, 819), (4, 850), (10, 779), (11, 850), (22, 739), (28, 659), (33, 799), (40, 829)]),
    # Решта — генеровані історії
    Product("rozetka.com.ua", "Apple iPhone 17 Pro Max 256GB Deep Blue", 69999),
    Product("rozetka.com.ua", "Електросамокат KUGOO KIRIN G2 2026", 28865),
    Product("notino.ua", "DIOR Sauvage · туалетна вода 60 мл", 2970),
    Product("notino.ua", "TePe Original · 0,6 mm 8 кс", 190),
    Product("notino.ua", "La Roche-Posay Effaclar Duo+ · 40 мл", 859),
    Product("comfy.ua", "Робот-пилосос Xiaomi Robot Vacuum X20+", 21999),
    Product("comfy.ua", "Телевізор Samsung 55\" QE55Q60D", 27999),
    Product("foxtrot.com.ua", "Мультипіч Philips Airfryer XXL HD9285/96", 8999),
    Product("allo.ua", "Смартфон Samsung Galaxy S25 Ultra 256GB", 52999),
    Product("moyo.ua", "Ноутбук ASUS Vivobook 16 X1605VA", 26999),
    Product("brain.com.ua", "SSD Samsung 990 PRO 2TB", 7499),
    Product("citrus.ua", "Apple AirPods Pro 2 USB-C", 9499),
    Product("kasta.ua", "Кросівки New Balance 574", 4290),
    Product("jysk.ua", "Матрац пружинний GOLD S35 160x200", 18999),
    Product("eldorado.ua", "Кавомашина De'Longhi Magnifica S ECAM22.110", 13999),
    Product("prom.ua", "Двигун для БПЛА DLE222", 132123),
    Product("olx.ua", "Ремінець шкіряний для Garmin Fenix", 999),
    Product("ikea.com", "KALLAX стелаж 77x147 см", 2999),
    Product("nike.com", "Nike Pegasus 41", 140, "USD"),
    Product("zalando.de", "Adidas Samba OG", 120, "EUR"),
    Product("ebay.com", "Sony WH-1000XM5 (renewed)", 229, "USD"),
    Product("morele.net", "Monitor Dell S2725DS 27\"", 1099, "PLN"),
    Product("apple.com", "AirTag 4-pack", 99, "USD"),
]

FIRST_UK = ["Олена", "Андрій", "Марія", "Тарас", "Ірина", "Олег", "Наталія", "Дмитро", "Катерина", "Богдан",
            "Юлія", "Сергій", "Оксана", "Максим", "Анна", "Віктор", "Софія", "Роман", "Галина", "Ігор",
            "Христина", "Назар", "Леся", "Артем", "Вікторія", "Павло", "Діана", "Євген", "Тетяна", "Остап"]
FIRST_EN = ["Alex", "Kate", "Mark", "Emma", "Lukas", "Anna", "Tom", "Sofia", "Daniel", "Mia", "Jan", "Eva"]
LAST_INIT = "АБВГДЗКЛМНОПРСТФШ"

FEEDBACK = [
    ("Додайте, будь ласка, Епіцентр — там ціни на побутову техніку змінюються щодня", "new", None),
    ("Чи можна стежити одразу за кількома розмірами взуття?", "new", None),
    ("Дякую! Зловив знижку на кермо MOZA, яку б сам пропустив 🙏", "read", None),
    ("Can you add Allegro? I buy a lot from Poland", "replied", "Allegro поки блокує автоматичні запити — працюємо над цим"),
    ("Сповіщення про наявність приходить надто часто, вимкнула для одного товару — дуже зручно", "replied",
     "Дякуємо! Саме для цього кнопка й з'явилась 🙂"),
    ("Бот не бачить ціну на одному магазині з Хорошопом, скріншот додаю", "new", None),
]


def _history_path(p: Product) -> list[tuple[int, float]]:
    """Фіксована або згенерована форма історії ціни (день, ціна)."""
    if p.path:
        return p.path
    pts = [(0, p.price)]
    price = p.price
    day = 0
    pattern = random.random()
    while day < DAYS:
        day += random.randint(3, 9)
        if day >= DAYS:
            break
        if pattern < 0.3 and 12 <= day <= 22:
            price = round(price * random.uniform(1.18, 1.32), -1 if price > 500 else 0)   # «підготовка до знижки»
        elif pattern < 0.3 and day > 22:
            price = round(price * random.uniform(0.78, 0.86), -1 if price > 500 else 0)   # «знижка»
        else:
            price = round(price * random.uniform(0.9, 1.08), -1 if price > 500 else 0)
        pts.append((day, max(price, 1)))
    if random.random() < 0.2:   # свіжі знижки в останні дні — щоб «за 24 год» був живим, без різкого сплеску
        pts.append((DAYS - random.uniform(0.2, 3.0), round(pts[-1][1] * random.uniform(0.88, 0.96), 0)))
    return pts


def _price_at(path: list[tuple[int, float]], day_float: float) -> float:
    price = path[0][1]
    for d, v in path:
        if d <= day_float:
            price = v
    return price


async def main() -> None:
    from sqlalchemy import select

    from app.database.models import (
        Admin, AdminAction, Feedback, Notification, NotificationStatus, PriceHistory, PriceTracker,
        ScrapeLog, TrackerStatus, User,
    )
    from app.database.session import get_session
    from app.services.scrapers.registry import platform_for
    from botkit.admin.auth import hash_password

    async with get_session() as s:
        if (await s.execute(select(User).limit(1))).scalar_one_or_none():
            print("База не порожня — використовуй новий DATABASE_URL", file=sys.stderr)
            raise SystemExit(1)

    users: list[User] = []
    ids: set[int] = set()
    # Зростання аудиторії: щодня більше нових юзерів
    for day in range(DAYS):
        n = max(0, int(round(0.6 + day / 9 + random.uniform(-0.8, 1.6))))
        for _ in range(n):
            en = random.random() < 0.18
            first = random.choice(FIRST_EN if en else FIRST_UK)
            tid = random.randint(150_000_000, 7_900_000_000)
            while tid in ids:
                tid = random.randint(150_000_000, 7_900_000_000)
            ids.add(tid)
            uname = None if random.random() < 0.3 else (
                f"{first.lower()}_{random.choice('abcdefghkmnoprst')}{random.randint(1, 99)}" if en
                else f"user{random.randint(1000, 99999)}")
            created = START + timedelta(days=day, hours=random.randint(7, 22), minutes=random.randint(0, 59))
            premium = random.random() < 0.13 and day < DAYS - 3
            users.append(User(
                telegram_id=tid, username=uname,
                first_name=first if en else f"{first} {random.choice(LAST_INIT)}.",
                language_code="en" if en else random.choice(["uk", "uk", "uk", "ru"]),
                tier="premium" if premium else "free",
                premium_until=(NOW + timedelta(days=random.choice([3, 6, 11, 18, 25, 29]))) if premium else None,
                created_at=created, updated_at=created,
            ))

    async with get_session() as s:
        s.add_all(users)
        await s.flush()

        paths = {i: _history_path(p) for i, p in enumerate(CATALOG)}
        tracker_rows = history_rows = notif_rows = log_rows = 0
        for u in users:
            k = random.choices([1, 2, 3, 4, 5, 7], weights=[22, 26, 20, 14, 10, 8])[0]
            if u.tier == "premium":
                k += random.randint(2, 6)
            picks = random.sample(range(len(CATALOG)), min(k, len(CATALOG)))
            for idx in picks:
                p = CATALOG[idx]
                path = paths[idx]
                t_created = u.created_at + timedelta(minutes=random.randint(2, 180))
                if t_created >= NOW:
                    continue
                start_day = (t_created - START).total_seconds() / 86400
                baseline = _price_at(path, start_day)
                target = None
                target_pct = None
                if random.random() < 0.35:
                    target_pct = Decimal(random.choice([10, 15, 20]))
                    target = Decimal(str(round(baseline * (1 - float(target_pct) / 100), 2)))
                url = f"https://{'www.' if p.source in ('notino.ua', 'ikea.com', 'nike.com') else ''}{p.source}/p/{idx}{random.randint(100, 999)}/"
                tr = PriceTracker(
                    user_id=u.id, url=url, canonical_url=url, source=p.source, title=p.title,
                    currency=p.currency, status=TrackerStatus.ACTIVE, target_price=target,
                    target_percent=target_pct, baseline_price=Decimal(str(baseline)) if target_pct else None,
                    notify_restock=not (p.flaky_stock and random.random() < 0.5),
                    created_at=t_created, updated_at=t_created,
                )
                s.add(tr)
                await s.flush()
                tracker_rows += 1

                plat = platform_for(p.source)
                strategy = (plat.strategy if plat and plat.strategy else "json-ld") + ("+render" if plat and plat.render_js else "")
                prev_price = None
                prev_avail = True
                t = t_created
                step = timedelta(hours=1 if u.tier == "premium" else 6)
                while t < NOW:
                    day_f = (t - START).total_seconds() / 86400
                    price = _price_at(path, day_f)
                    avail = not (p.flaky_stock and 26 <= day_f <= 29)
                    s.add(PriceHistory(tracker_id=tr.id, price=Decimal(str(price)), is_available=avail, recorded_at=t))
                    history_rows += 1
                    if t >= NOW - timedelta(days=30):
                        fail = p.source == "prom.ua" and random.random() < 0.12
                        s.add(ScrapeLog(domain=p.source, ok=not fail, strategy=None if fail else strategy,
                                        reason="parse_failed" if fail else None,
                                        healed=(not fail and p.source == "prom.ua" and random.random() < 0.08),
                                        duration_ms=random.randint(350, 1400) + (2200 if "render" in strategy else 0),
                                        created_at=t))
                        log_rows += 1
                    event = None
                    if prev_price is not None:
                        if not prev_avail and avail and tr.notify_restock:
                            event = "restock"
                        elif target is not None and price <= float(target) < prev_price:
                            event = "target_reached"
                        elif price < prev_price * 0.97:
                            event = "decrease"
                    if event:
                        failed = random.random() < 0.03
                        s.add(Notification(
                            user_id=u.id, tracker_id=tr.id, event_type=event,
                            old_price=Decimal(str(prev_price)), new_price=Decimal(str(price)),
                            status=NotificationStatus.FAILED if failed else NotificationStatus.SENT,
                            error_message="Forbidden: bot was blocked by the user" if failed else None,
                            telegram_message_id=None if failed else random.randint(1000, 99999),
                            created_at=t + timedelta(seconds=3), updated_at=t + timedelta(seconds=3),
                        ))
                        notif_rows += 1
                    prev_price, prev_avail = price, avail
                    t += step
                tr.current_price = Decimal(str(prev_price))
                tr.is_available = prev_avail
                tr.last_checked_at = t - step
            if tracker_rows % 60 == 0:
                await s.flush()

        # Кілька проблемних трекерів для блоку «Проблемні трекери»
        prom = (await s.execute(select(PriceTracker).where(PriceTracker.source == "prom.ua").limit(2))).scalars().all()
        for tr in prom:
            tr.consecutive_errors = 3

        for i, (text, status, reply) in enumerate(FEEDBACK):
            u = random.choice(users)
            created = NOW - timedelta(hours=6 + i * 19)
            s.add(Feedback(
                user_id=u.id, telegram_id=u.telegram_id, username=u.username, first_name=u.first_name,
                language_code=u.language_code, text=text, content_type="photo" if "скріншот" in text else "text",
                telegram_message_id=500 + i, chat_id=u.telegram_id, status=status,
                reply_text=reply, replied_at=created + timedelta(hours=2) if reply else None,
                replied_by="demo" if reply else None, created_at=created,
            ))

        password = os.environ.get("DEMO_ADMIN_PASSWORD")
        if password:
            s.add(Admin(username="demo", password_hash=hash_password(password), telegram_id=None,
                        is_active=True, role="owner"))
        s.add(AdminAction(admin_username="demo", action="premium_activate", target="—",
                          details="+30 днів", created_at=NOW - timedelta(days=1)))

    print(f"users={len(users)} trackers={tracker_rows} history={history_rows} "
          f"notifications={notif_rows} scrape_log={log_rows}")


if __name__ == "__main__":
    from app.database.migrate import migrate
    migrate()            # синхронна: сама запускає свій event loop — до asyncio.run
    asyncio.run(main())
