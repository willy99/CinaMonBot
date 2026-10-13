"""
Тести CLI адмінки (python -m app.admin.cli create-admin / list-admins).

cli.main() сам викликає asyncio.run(), тому тести синхронні й без фікстури db (вона асинхронна):
базу чистимо через власний asyncio.run, а після кожного запуску диспозимо engine, щоб з'єднання
aiosqlite не лишались прив'язаними до вже закритого циклу.

    venv/bin/python -m pytest tests/test_admin_cli.py -q
"""
from __future__ import annotations

import asyncio
import getpass
import sys
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import select

from app.admin import cli
from botkit.admin import auth
from app.database.models import Admin, Base
from app.database.session import engine, get_session


def _run(fn: Callable[[], Awaitable]):
    async def wrapped():
        try:
            return await fn()
        finally:
            await engine.dispose()
    return asyncio.run(wrapped())


async def _reset_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def _admins() -> list[Admin]:
    async with get_session() as s:
        return (await s.execute(select(Admin).order_by(Admin.username))).scalars().all()


@pytest.fixture
def cli_db():
    _run(_reset_db)
    yield
    _run(_reset_db)


def _main(monkeypatch, argv: list[str], passwords: list[str] | None = None) -> int:
    monkeypatch.setattr(sys, "argv", ["python -m app.admin.cli", *argv])
    prompts: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        if not passwords:
            raise AssertionError("пароль не мав запитуватись")
        return passwords.pop(0)

    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    asyncio.run(engine.dispose())
    return exc.value.code


def test_create_admin_success(cli_db, monkeypatch, capsys):
    code = _main(monkeypatch, ["create-admin", "NewAdmin", "--telegram-id", "42"], ["strong-pass-1", "strong-pass-1"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Створено адміна 'newadmin'" in out and "Telegram ID 42" in out
    (a,) = _run(_admins)
    assert a.username == "newadmin" and a.telegram_id == 42 and a.is_active
    assert auth.verify_password("strong-pass-1", a.password_hash)


def test_create_admin_without_telegram(cli_db, monkeypatch, capsys):
    assert _main(monkeypatch, ["create-admin", "solo"], ["strong-pass-1", "strong-pass-1"]) == 0
    assert "без 2FA Telegram ID" in capsys.readouterr().out
    (a,) = _run(_admins)
    assert a.telegram_id is None


def test_create_admin_password_mismatch(cli_db, monkeypatch, capsys):
    assert _main(monkeypatch, ["create-admin", "x1"], ["strong-pass-1", "strong-pass-2"]) == 1
    assert "не збігаються" in capsys.readouterr().err
    assert _run(_admins) == []


def test_create_admin_weak_password(cli_db, monkeypatch, capsys):
    assert _main(monkeypatch, ["create-admin", "x1"], ["short1", "short1"]) == 1
    assert "10 символів" in capsys.readouterr().err
    assert _run(_admins) == []


def test_create_admin_duplicate(cli_db, monkeypatch, capsys):
    assert _main(monkeypatch, ["create-admin", "dup"], ["strong-pass-1", "strong-pass-1"]) == 0
    capsys.readouterr()
    # пароль не запитується — перевірка дубліката йде раніше
    assert _main(monkeypatch, ["create-admin", "DUP"], None) == 1
    assert "уже існує" in capsys.readouterr().err
    assert len(_run(_admins)) == 1


def test_list_admins(cli_db, monkeypatch, capsys):
    assert _main(monkeypatch, ["create-admin", "alpha", "--telegram-id", "7"], ["strong-pass-1", "strong-pass-1"]) == 0
    assert _main(monkeypatch, ["create-admin", "beta"], ["strong-pass-1", "strong-pass-1"]) == 0

    async def deactivate_beta():
        async with get_session() as s:
            b = (await s.execute(select(Admin).where(Admin.username == "beta"))).scalar_one()
            b.is_active = False
    _run(deactivate_beta)
    capsys.readouterr()

    assert _main(monkeypatch, ["list-admins"], None) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert lines[0].startswith("alpha") and "tg=7" in lines[0] and "активний" in lines[0] and "ніколи" in lines[0]
    assert lines[1].startswith("beta") and "tg=—" in lines[1] and "вимкнений" in lines[1]


def test_purge_bogus_failed_notifications_cli_dry_run_and_apply_on_empty_db(cli_db, monkeypatch, capsys):
    """Парсинг аргументів: без --apply і з --apply обидва завершуються 0; на порожній базі — «не знайдено»."""
    assert _main(monkeypatch, ["purge-bogus-failed-notifications"], None) == 0
    assert "не знайдено" in capsys.readouterr().out
    assert _main(monkeypatch, ["purge-bogus-failed-notifications", "--apply"], None) == 0
    assert "не знайдено" in capsys.readouterr().out


def test_unknown_command_exits(cli_db, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["python -m app.admin.cli", "nope"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


# ─── fix-kopecks-prices ──────────────────────────────────────────

async def test_fix_kopecks_artifacts_dry_run_and_apply(db, capsys):
    from datetime import datetime, timedelta
    from decimal import Decimal
    from sqlalchemy import select
    from app.admin.cli import find_kopecks_artifacts, fix_kopecks_artifacts
    from app.database.models import Notification, NotificationStatus, PriceHistory, PriceTracker, User
    from app.database.session import get_session

    async with get_session() as s:
        u = User(telegram_id=1, language_code="uk"); s.add(u); await s.flush()
        tr = PriceTracker(user_id=u.id, url="https://prom.ua/p/1", canonical_url="https://prom.ua/p/1", source="prom.ua",
                          title="Плоскогубці", current_price=Decimal("581.66"), currency="UAH", status="active")
        ok_tr = PriceTracker(user_id=u.id, url="https://rozetka.com.ua/p/2", canonical_url="https://rozetka.com.ua/p/2",
                             source="rozetka.com.ua", title="Дорогий", current_price=Decimal("70000"), currency="UAH", status="active")
        s.add_all([tr, ok_tr]); await s.flush()
        base = datetime.utcnow() - timedelta(days=10)
        for i, price in enumerate(["369", "389", "389", "64833", "389", "58166", "581.66"]):
            s.add(PriceHistory(tracker_id=tr.id, price=Decimal(price), recorded_at=base + timedelta(hours=i)))
        for i, price in enumerate(["69999", "66999", "67999"]):           # справжні великі ціни — не чіпати
            s.add(PriceHistory(tracker_id=ok_tr.id, price=Decimal(price), recorded_at=base + timedelta(hours=i)))
        s.add(Notification(user_id=u.id, tracker_id=tr.id, event_type="decrease", old_price=Decimal("64833"),
                           new_price=Decimal("389"), status=NotificationStatus.SENT))
        s.add(Notification(user_id=u.id, tracker_id=tr.id, event_type="decrease", old_price=Decimal("389"),
                           new_price=Decimal("369"), status=NotificationStatus.SENT))          # справжнє — лишається

    rows, notifs = await find_kopecks_artifacts()
    assert sorted(float(r.price) for r in rows) == [58166.0, 64833.0] and len(notifs) == 1

    assert await fix_kopecks_artifacts(apply=False) == 2
    assert "Dry-run" in capsys.readouterr().out
    async with get_session() as s:
        assert (await s.execute(select(PriceHistory).where(PriceHistory.price > 10000))).scalars().all().__len__() == 2 + 3

    await fix_kopecks_artifacts(apply=True)
    async with get_session() as s:
        prices = sorted(float(p) for p in (await s.execute(
            select(PriceHistory.price).where(PriceHistory.tracker_id == tr.id))).scalars().all())
        assert prices == [369.0, 389.0, 389.0, 389.0, 581.66, 581.66, 648.33]
        left = (await s.execute(select(Notification))).scalars().all()
        assert len(left) == 1 and float(left[0].old_price) == 389.0
        big = (await s.execute(select(PriceHistory).where(PriceHistory.tracker_id == ok_tr.id))).scalars().all()
        assert sorted(float(p.price) for p in big) == [66999.0, 67999.0, 69999.0]
