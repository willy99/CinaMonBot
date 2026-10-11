"""
Щотижневий дайджест "що подешевшало за тиждень" (app.scheduler.digest).

Що доводиться:
 - build_digest: падіння відсортовані за %, топ-5, подорожчання рахуються одним рядком,
   мінімум за 30 днів, тихий тиждень → None, < 2 записів історії → трекер пропущено,
   валюта береться з трекера, обидві мови, лише ACTIVE-трекери, лише свої трекери;
 - send_weekly_digests: шле лише opt-in і незаблокованим; рівно одне повідомлення на юзера
   на його telegram_id; ставить last_digest_at; 6-денний захист; помилка в одного не зупиняє
   решту; "bot was blocked" → is_blocked;
 - callback'и digest:off / digest:toggle, рядок у /settings, реєстрація job'а, CLI digest-preview.

    venv/bin/python -m pytest tests/test_digest.py -q
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.admin import cli
from app.bot.handlers import main as h
from app.database.models import Base, PriceHistory, PriceTracker, TrackerStatus, User
from app.database.session import engine, get_session
from app.i18n import t
from app.scheduler import digest as dg
from app.scheduler import price_checker as pc
from tests.helpers import capture_bot, fsm_storage, make_bot, make_callback, make_message

D = Decimal
NOW = datetime(2026, 10, 12, 7, 0, 0)   # понеділок 10:00 за Києвом = 07:00 UTC (naive UTC, як у базі)


@pytest.fixture(autouse=True)
def _isolate():
    fsm_storage()
    yield


# ─── Сіди ────────────────────────────────────────────────────────

async def seed_user(telegram_id: int, lang: str = "uk", *, digest: bool = True, blocked: bool = False,
                    last_digest: datetime | None = None) -> User:
    async with get_session() as s:
        user = User(telegram_id=telegram_id, username=f"u{telegram_id}", first_name=f"User{telegram_id}",
                    language_code=lang, digest_enabled=digest, is_blocked=blocked, last_digest_at=last_digest)
        s.add(user)
    return user


async def seed_tracker(user: User, url: str, *, title: str, price: str | None, currency: str = "UAH",
                       status: str = TrackerStatus.ACTIVE,
                       history: list[tuple[int, str]] | None = None) -> PriceTracker:
    """history: [(днів тому, ціна), ...] — рядки PriceHistory відносно NOW."""
    async with get_session() as s:
        tr = PriceTracker(user_id=user.id, url=url, canonical_url=url, source="shop.example", title=title,
                          current_price=D(price) if price else None, currency=currency, status=status)
        s.add(tr)
        await s.flush()
        for days_ago, p in (history or []):
            s.add(PriceHistory(tracker_id=tr.id, price=D(p), recorded_at=NOW - timedelta(days=days_ago)))
    return tr


async def load_user(telegram_id: int) -> User:
    async with get_session() as s:
        return (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()


# ─── build_digest ────────────────────────────────────────────────

async def test_digest_drops_sorted_by_percent_with_ups_and_30d_min(db):
    user = await seed_user(1)
    # −9.2 % (3249 → 2949), мін. за 30 дн.: 2649 (запис 20 днів тому); запис 40 днів тому (2000) поза вікном
    await seed_tracker(user, "https://shop.example/p/1", title="Marshall Major IV", price="2949",
                       history=[(40, "2000"), (20, "2649"), (10, "3249"), (8, "3249"), (3, "3100"), (0, "2949")])
    # −20 % (1000 → 800)
    await seed_tracker(user, "https://shop.example/p/2", title="Big drop", price="800",
                       history=[(9, "1000"), (1, "800")])
    # подорожчало
    await seed_tracker(user, "https://shop.example/p/3", title="Up", price="1200",
                       history=[(9, "1000"), (1, "1200")])
    await seed_tracker(user, "https://shop.example/p/4", title="Up2", price="505",
                       history=[(9, "500"), (1, "505")])
    # без змін — ні туди, ні сюди
    await seed_tracker(user, "https://shop.example/p/5", title="Flat", price="700",
                       history=[(9, "700"), (1, "700")])

    data = await dg.collect_digest(user.id, now=NOW)
    assert [d.title for d in data.drops] == ["Big drop", "Marshall Major IV"]
    assert data.ups == 2
    m = data.drops[1]
    assert (m.old_price, m.new_price, m.drop_pct, m.min_30d) == (D("3249"), D("2949"), D("9.2"), D("2649"))

    text = await dg.build_digest(await load_user(1), now=NOW)
    assert text.startswith(t("uk", "digest_header"))
    assert "📉 Big drop: 1 000 → 800 грн (−20 %) · мін. за 30 дн.: 800" in text
    assert "📉 Marshall Major IV: 3 249 → 2 949 грн (−9,2 %) · мін. за 30 дн.: 2 649" in text
    assert text.index("Big drop") < text.index("Marshall")
    assert "📈 Подорожчало товарів: 2" in text
    assert text.rstrip().endswith(t("uk", "digest_hint").strip())
    assert "Flat" not in text and "Up" not in text.replace("Подорожчало", "")


async def test_digest_top_5_cap(db):
    user = await seed_user(2)
    for i in range(1, 8):   # падіння 1 %..7 %
        await seed_tracker(user, f"https://shop.example/p/{i}", title=f"T{i}", price=str(1000 - i * 10),
                           history=[(9, "1000"), (0, str(1000 - i * 10))])
    data = await dg.collect_digest(user.id, now=NOW)
    assert len(data.drops) == dg.DIGEST_TOP_N == 5
    assert [d.title for d in data.drops] == ["T7", "T6", "T5", "T4", "T3"]
    text = await dg.build_digest(await load_user(2), now=NOW)
    assert "T3" in text and "T2" not in text and "T1:" not in text


async def test_digest_quiet_week_returns_none(db):
    """Нічого не подешевшало (лише подорожчання/без змін) → None, навіть якщо ups > 0."""
    user = await seed_user(3)
    await seed_tracker(user, "https://shop.example/p/1", title="Up", price="1200", history=[(9, "1000"), (1, "1200")])
    await seed_tracker(user, "https://shop.example/p/2", title="Flat", price="500", history=[(9, "500"), (1, "500")])
    data = await dg.collect_digest(user.id, now=NOW)
    assert data.drops == [] and data.ups == 1
    assert await dg.build_digest(await load_user(3), now=NOW) is None
    assert dg.render_digest(dg.DigestData(), "uk") is None


async def test_digest_no_trackers_and_no_history_skipped(db):
    user = await seed_user(4)
    assert await dg.build_digest(await load_user(4), now=NOW) is None
    # < 2 записів історії → трекер пропускається, хоч current_price і нижчий
    await seed_tracker(user, "https://shop.example/p/1", title="One row", price="500", history=[(3, "1000")])
    await seed_tracker(user, "https://shop.example/p/2", title="No rows", price="500")
    assert await dg.build_digest(await load_user(4), now=NOW) is None


async def test_digest_reference_price_fallback_to_earliest_within_week(db):
    """Немає запису ≥ 7 днів тому → порівнюємо з найпершим записом за останній тиждень."""
    user = await seed_user(5)
    await seed_tracker(user, "https://shop.example/p/1", title="New item", price="900",
                       history=[(5, "1000"), (3, "950"), (0, "900")])
    data = await dg.collect_digest(user.id, now=NOW)
    (d,) = data.drops
    assert d.old_price == D("1000") and d.new_price == D("900") and d.drop_pct == D("10")


async def test_digest_reference_price_is_latest_row_at_or_before_week_ago(db):
    user = await seed_user(6)
    # Записи 30, 12, 7 днів тому: беремо той, що рівно 7 днів тому (останній <= now−7д), а не 30-денний
    await seed_tracker(user, "https://shop.example/p/1", title="X", price="900",
                       history=[(30, "2000"), (12, "1500"), (7, "1000"), (0, "900")])
    data = await dg.collect_digest(user.id, now=NOW)
    (d,) = data.drops
    assert d.old_price == D("1000") and d.min_30d == D("900")


async def test_digest_only_active_trackers_and_only_own(db):
    user = await seed_user(7)
    other = await seed_user(8)
    await seed_tracker(user, "https://shop.example/p/1", title="Paused", price="800", status=TrackerStatus.PAUSED,
                       history=[(9, "1000"), (0, "800")])
    await seed_tracker(user, "https://shop.example/p/2", title="Limit", price="800", status=TrackerStatus.PAUSED_LIMIT,
                       history=[(9, "1000"), (0, "800")])
    await seed_tracker(other, "https://shop.example/p/3", title="Foreign", price="800",
                       history=[(9, "1000"), (0, "800")])
    assert await dg.build_digest(await load_user(7), now=NOW) is None
    text = await dg.build_digest(await load_user(8), now=NOW)
    assert "Foreign" in text and "Paused" not in text


async def test_digest_currency_per_tracker_and_english(db):
    user = await seed_user(9, "en")
    await seed_tracker(user, "https://shop.example/p/1", title="Dollar <thing>", price="90", currency="USD",
                       history=[(9, "100"), (0, "90")])
    await seed_tracker(user, "https://shop.example/p/2", title="Hryvnia", price="4500", currency="UAH",
                       history=[(9, "5000"), (0, "4500")])
    text = await dg.build_digest(await load_user(9), now=NOW)
    assert text.startswith(t("en", "digest_header"))
    assert "📉 Dollar &lt;thing&gt;: 100 → $90 (−10 %) · 30-day low: 90" in text
    assert "📉 Hryvnia: 5,000 → ₴4,500 (−10 %) · 30-day low: 4,500" in text
    assert "pricier" not in text          # подорожчань немає → рядка немає
    assert t("en", "digest_hint").strip() in text


async def test_digest_uses_history_when_current_price_missing(db):
    user = await seed_user(10)
    await seed_tracker(user, "https://shop.example/p/1", title="NoCurrent", price=None,
                       history=[(9, "1000"), (0, "900")])
    data = await dg.collect_digest(user.id, now=NOW)
    assert [(d.title, d.new_price) for d in data.drops] == [("NoCurrent", D("900"))]


# ─── send_weekly_digests ─────────────────────────────────────────

async def _seed_dropper(telegram_id: int, lang: str = "uk", **user_kw) -> User:
    user = await seed_user(telegram_id, lang, **user_kw)
    await seed_tracker(user, f"https://shop.example/p/{telegram_id}", title=f"Item{telegram_id}", price="900",
                       history=[(9, "1000"), (0, "900")])
    return user


async def test_send_weekly_digests_only_opted_in_unblocked_one_message_each(db):
    await _seed_dropper(11)                       # шлемо
    await _seed_dropper(12, "en")                 # шлемо
    await _seed_dropper(13, digest=False)         # вимкнув
    await _seed_dropper(14, blocked=True)         # заблокував бота
    quiet = await seed_user(15)                   # тихий тиждень
    await seed_tracker(quiet, "https://shop.example/p/15", title="Up", price="1100", history=[(9, "1000"), (0, "1100")])
    await seed_user(16)                           # без трекерів
    bot = make_bot()

    assert await dg.send_weekly_digests(bot, now=NOW) == 2

    calls = bot.send_message.await_args_list
    assert sorted(c.kwargs["chat_id"] for c in calls) == [11, 12]
    by_chat = {c.kwargs["chat_id"]: c.kwargs for c in calls}
    assert "Item11" in by_chat[11]["text"] and "Item12" not in by_chat[11]["text"]
    assert "Item12" in by_chat[12]["text"] and "Item11" not in by_chat[12]["text"]
    assert by_chat[11]["text"].startswith(t("uk", "digest_header"))
    assert by_chat[12]["text"].startswith(t("en", "digest_header"))
    assert by_chat[11]["parse_mode"] == "HTML"
    # Одна кнопка "🔕 Вимкнути дайджест" → digest:off, мовою юзера
    for uid, lang in [(11, "uk"), (12, "en")]:
        kb = by_chat[uid]["reply_markup"].inline_keyboard
        assert [[b.callback_data for b in row] for row in kb] == [["digest:off"]]
        assert kb[0][0].text == t(lang, "btn_digest_off")

    # last_digest_at — лише у тих, кому надіслано
    assert (await load_user(11)).last_digest_at == NOW and (await load_user(12)).last_digest_at == NOW
    for uid in (13, 14, 15, 16):
        assert (await load_user(uid)).last_digest_at is None


async def test_send_weekly_digests_six_day_guard(db):
    await _seed_dropper(21, last_digest=NOW - timedelta(days=4))             # < 6 днів → пропустити
    await _seed_dropper(22, last_digest=NOW - timedelta(days=6, minutes=1))  # ≥ 6 днів → слати
    await _seed_dropper(23, last_digest=NOW - timedelta(days=7))             # звичайний тиждень → слати
    bot = make_bot()
    assert await dg.send_weekly_digests(bot, now=NOW) == 2
    assert sorted(c.kwargs["chat_id"] for c in bot.send_message.await_args_list) == [22, 23]
    assert (await load_user(21)).last_digest_at == NOW - timedelta(days=4)

    # Повторний запуск одразу (рестарт) — нікому нічого
    bot2 = make_bot()
    assert await dg.send_weekly_digests(bot2, now=NOW + timedelta(hours=1)) == 0
    bot2.send_message.assert_not_awaited()


async def test_send_weekly_digests_failure_isolation_and_blocked_marking(db):
    await _seed_dropper(31)
    await _seed_dropper(32)
    await _seed_dropper(33)
    bot = make_bot()

    async def send(chat_id, **kw):
        if chat_id == 31:
            raise RuntimeError("network down")
        if chat_id == 32:
            raise Exception("Forbidden: bot was blocked by the user")
        return AsyncMock(message_id=1)
    bot.send_message = AsyncMock(side_effect=send)

    assert await dg.send_weekly_digests(bot, now=NOW) == 1
    assert sorted(c.kwargs["chat_id"] for c in bot.send_message.await_args_list) == [31, 32, 33]
    u31, u32, u33 = await load_user(31), await load_user(32), await load_user(33)
    assert u31.last_digest_at is None and u31.is_blocked is False
    assert u32.last_digest_at is None and u32.is_blocked is True
    assert u33.last_digest_at == NOW and u33.is_blocked is False


async def test_send_weekly_digests_build_error_for_one_user_does_not_stop_others(db):
    await _seed_dropper(41)
    await _seed_dropper(42)
    bot = make_bot()
    real = dg.build_digest

    async def flaky(user, now=None):
        if user.telegram_id == 41:
            raise ValueError("corrupt history")
        return await real(user, now=now)
    with patch.object(dg, "build_digest", flaky):
        assert await dg.send_weekly_digests(bot, now=NOW) == 1
    assert [c.kwargs["chat_id"] for c in bot.send_message.await_args_list] == [42]


async def test_send_weekly_digests_nothing_to_send_touches_nothing(db):
    await seed_user(51)
    bot = make_bot()
    assert await dg.send_weekly_digests(bot, now=NOW) == 0
    bot.send_message.assert_not_awaited()


# ─── Callback'и digest:off / digest:toggle і /settings ───────────

async def test_digest_off_callback_disables_and_removes_button(db):
    await _seed_dropper(61)
    with capture_bot() as sent:
        await h.digest_off(make_callback(61, "digest:off"))
    assert (await load_user(61)).digest_enabled is False
    assert [s.kind for s in sent] == ["markup", "callback"]
    assert sent[0].reply_markup is None
    assert sent[1].text == t("uk", "digest_off_toast")

    # Після вимкнення дайджест не приходить
    bot = make_bot()
    assert await dg.send_weekly_digests(bot, now=NOW) == 0


async def test_digest_off_survives_keyboard_edit_failure(db):
    await seed_user(62, "en")
    from aiogram.types import Message
    with patch.object(Message, "edit_reply_markup", AsyncMock(side_effect=RuntimeError("can't be edited"))), \
         capture_bot() as sent:
        await h.digest_off(make_callback(62, "digest:off", "en"))
    assert (await load_user(62)).digest_enabled is False
    assert sent[-1].kind == "callback" and sent[-1].text == t("en", "digest_off_toast")


async def test_digest_off_for_unknown_user_creates_user_and_disables(db):
    """Натиснув той, кого ще немає в базі (теоретично) — створюємо і вимикаємо, без винятків."""
    with capture_bot() as sent:
        await h.digest_off(make_callback(63, "digest:off"))
    assert (await load_user(63)).digest_enabled is False
    assert sent[-1].text == t("uk", "digest_off_toast")


async def test_settings_row_and_toggle_round_trip(db):
    user = await seed_user(71, "en")
    tr = await seed_tracker(user, "https://shop.example/p/1", title="Alpha", price="100")

    with capture_bot() as sent:
        await h.cmd_settings_msg(make_message(71, "/settings", "en"))
    assert sent[0].buttons[0] == f"{t('en', 'settings_digest')} ✅"
    assert sent[0].callbacks == ["digest:toggle", f"tset:{tr.id}", "my_list"]

    with capture_bot() as sent:
        await h.digest_toggle(make_callback(71, "digest:toggle", "en"))
    assert (await load_user(71)).digest_enabled is False
    assert [s.kind for s in sent] == ["edit", "callback"]
    assert sent[0].buttons[0] == f"{t('en', 'settings_digest')} ☐"
    assert sent[0].callbacks == ["digest:toggle", f"tset:{tr.id}", "my_list"]
    assert sent[1].text == t("en", "digest_off_toast")

    with capture_bot() as sent:
        await h.digest_toggle(make_callback(71, "digest:toggle", "en"))
    assert (await load_user(71)).digest_enabled is True
    assert sent[0].buttons[0] == f"{t('en', 'settings_digest')} ✅"
    assert sent[1].text == t("en", "digest_on_toast")


async def test_settings_row_reflects_disabled_state_uk(db):
    user = await seed_user(72, digest=False)
    await seed_tracker(user, "https://shop.example/p/1", title="Beta", price="100")
    with capture_bot() as sent:
        await h.cmd_settings_msg(make_message(72, "/settings"))
    assert sent[0].buttons[0] == "📬 Щотижневий дайджест ☐"


async def test_toggle_only_changes_the_presser(db):
    a = await seed_user(73)
    await seed_tracker(a, "https://shop.example/p/1", title="A", price="100")
    await seed_user(74)
    with capture_bot():
        await h.digest_toggle(make_callback(73, "digest:toggle"))
    assert (await load_user(73)).digest_enabled is False
    assert (await load_user(74)).digest_enabled is True


# ─── Планувальник ────────────────────────────────────────────────

def test_weekly_digest_job_registered_monday_10_kyiv(monkeypatch):
    monkeypatch.setattr(pc.scheduler, "start", lambda *a, **k: None)
    bot = make_bot()
    try:
        pc.setup_scheduler(bot)
        job = pc.scheduler.get_job("weekly_digest")
        assert job is not None and job.func is dg.send_weekly_digests and job.args == (bot,)
        assert isinstance(job.trigger, CronTrigger)
        assert "day_of_week='mon'" in str(job.trigger) and "hour='10'" in str(job.trigger)
        assert str(job.trigger.timezone) == "Europe/Kyiv"
    finally:
        pc.scheduler.remove_all_jobs()


def test_digest_constants():
    assert (dg.DIGEST_TOP_N, dg.DIGEST_WINDOW_DAYS, dg.DIGEST_MIN_WINDOW_DAYS, dg.DIGEST_GUARD_DAYS) == (5, 7, 30, 6)


# ─── CLI digest-preview ──────────────────────────────────────────

async def test_cli_digest_preview_prints_text_without_sending(db, capsys):
    user = await _seed_dropper(81)
    with patch.object(dg, "_now_naive", lambda: NOW):
        assert await cli.digest_preview(81) == 0
    out = capsys.readouterr().out
    assert "81" in out and "дайджест: увімкнено" in out and "Item81" in out and "(−10 %)" in out
    assert (await load_user(81)).last_digest_at is None     # нічого не надсилали і не позначали
    assert user.telegram_id == 81


async def test_cli_digest_preview_quiet_week_and_unknown_user(db, capsys):
    await seed_user(82, digest=False)
    assert await cli.digest_preview(82) == 0
    out = capsys.readouterr().out
    assert "дайджест: вимкнено" in out and "НЕ буде надіслано" in out

    assert await cli.digest_preview(999999) == 1
    assert "не знайдено" in capsys.readouterr().err


def test_cli_digest_preview_argparse(monkeypatch, capsys):
    """Повний шлях через main(): підкоманда розбирається і завершується кодом digest_preview."""
    def reset():
        async def go():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
            await engine.dispose()
        asyncio.run(go())
    reset()
    try:
        monkeypatch.setattr(sys, "argv", ["python -m app.admin.cli", "digest-preview", "424242"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        asyncio.run(engine.dispose())
        assert exc.value.code == 1
        assert "424242" in capsys.readouterr().err
    finally:
        reset()
