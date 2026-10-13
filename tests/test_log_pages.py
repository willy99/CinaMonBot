"""Сторінки журналів /notifications і /checks: серверний пейджинг по 25, сортування, фільтри, посилання з дашборду."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.admin import queries as q
from botkit.admin import queries as aq
from app.database.models import Notification, NotificationStatus, PriceHistory, PriceTracker, TrackerStatus, User
from app.database.session import get_session


async def _seed(n_notif: int = 60, n_checks: int = 30):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with get_session() as s:
        u1 = User(telegram_id=1, first_name="Оля", username="olia", language_code="uk")
        u2 = User(telegram_id=2, first_name="Bob", username="bob", language_code="en")
        s.add_all([u1, u2]); await s.flush()
        t1 = PriceTracker(user_id=u1.id, url="https://rozetka.com.ua/p/1", canonical_url="https://rozetka.com.ua/p/1",
                          source="rozetka.com.ua", title="Чайник Tefal", current_price=Decimal("999"), currency="UAH",
                          status=TrackerStatus.ACTIVE)
        t2 = PriceTracker(user_id=u2.id, url="https://nike.com/p/2", canonical_url="https://nike.com/p/2",
                          source="nike.com", title="Nike Pegasus", current_price=Decimal("120"), currency="USD",
                          status=TrackerStatus.ACTIVE)
        s.add_all([t1, t2]); await s.flush()
        for i in range(n_notif):
            tr, u = (t1, u1) if i % 2 == 0 else (t2, u2)
            s.add(Notification(
                user_id=u.id, tracker_id=tr.id,
                event_type="decrease" if i % 3 else "target_reached",
                old_price=Decimal("100") + i, new_price=Decimal("90") + i,
                status=NotificationStatus.SENT if i % 5 else NotificationStatus.FAILED,
                error_message=None if i % 5 else "bot was blocked",
                created_at=now - timedelta(minutes=i),
            ))
        for i in range(n_checks):
            tr = t1 if i % 2 == 0 else t2
            s.add(PriceHistory(tracker_id=tr.id, price=Decimal("50") + i, is_available=bool(i % 4),
                               recorded_at=now - timedelta(minutes=i)))
    return u1, u2


async def test_notifications_page_paging_and_order(db):
    await _seed()
    rows, total = await q.notifications_page(1)
    assert total == 60 and len(rows) == 25
    assert rows[0]["new"] == 90.0            # найновіший (i=0) перший
    assert rows[0]["currency"] == "UAH" and rows[1]["currency"] == "USD"
    rows3, _ = await q.notifications_page(3)
    assert len(rows3) == 10                  # 60 = 25 + 25 + 10
    rows9, _ = await q.notifications_page(9)
    assert rows9 == []


async def test_notifications_filters(db):
    await _seed()
    rows, total = await q.notifications_page(1, status="failed")
    assert total == 12 and all(r["status"] == "failed" for r in rows) and rows[0]["error"] == "bot was blocked"
    _, total = await q.notifications_page(1, event="target_reached")
    assert total == 20
    _, total = await q.notifications_page(1, search="bob")
    assert total == 30
    _, total = await q.notifications_page(1, search="Tefal")
    assert total == 30
    _, total = await q.notifications_page(1, search="2")     # telegram id
    assert total == 30


async def test_checks_page(db):
    await _seed()
    rows, total, sources = await q.checks_page(1)
    assert total == 30 and len(rows) == 25 and sources == ["nike.com", "rozetka.com.ua"]
    assert rows[0]["price"] == 50.0 and rows[0]["available"] is False
    rows2, total, _ = await q.checks_page(2)
    assert len(rows2) == 5
    _, total, _ = await q.checks_page(1, source="nike.com")
    assert total == 15
    _, total, _ = await q.checks_page(1, search="Оля")
    assert total == 15


async def _login(user):
    from botkit.admin import auth
    # Одразу owner: інакше startup-хук ensure_owner() може підвищити роль уже після входу → сесію відкличуть
    await aq.create_admin("tester", auth.hash_password("test-pass-2026"), 1, role=auth.OWNER)
    await user.open("/login")
    user.find("Логін").type("tester")
    user.find("Пароль").type("test-pass-2026")
    user.find("Увійти").click()
    await user.should_see("Дашборд")


async def test_dashboard_cards_link_to_logs(db, user):
    await _login(user)
    await user.open("/")
    await user.should_see("Надіслані сповіщення →")
    await user.should_see("Перевірки цін →")
    await user.should_see("Нові користувачі →")


async def test_notifications_page_renders_with_pager(db, user):
    await _seed()
    await _login(user)
    await user.open("/notifications")
    await user.should_see("Сповіщення")
    await user.should_see("Показано 1–25 з 60")
    await user.should_see("стор. 1 з 3")
    await user.open("/notifications?page=3&status=failed")
    await user.should_see("з 12")


async def test_checks_page_renders(db, user):
    await _seed()
    await _login(user)
    await user.open("/checks")
    await user.should_see("Перевірки цін")
    await user.should_see("Показано 1–25 з 30")
    await user.should_see("стор. 1 з 2")
