"""
Під сповіщенням «ціль досягнута» (ставка перевищила межу / ціна впала до цілі) — кнопка «🎯 Нова межа/ціль»:
юзер одразу задає нову суму або відсоток, і бот стежить уже за нею.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from app.bot.handlers import main as h
from app.database.models import Notification, NotificationStatus, PriceEventType, PriceTracker, TrackerStatus
from app.database.session import get_session
from app.i18n import t
from app.scheduler import price_checker as pc
from tests.helpers import capture_bot, fsm_for, fsm_storage, make_bot, make_callback, make_message
from tests.test_auction import LOT, lot, seed_lot
from tests.test_scheduler import load_tracker, patch_scrape, seed_tracker, seed_user


@pytest.fixture(autouse=True)
def _fsm():
    fsm_storage()
    yield


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _texts(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


# ─── Кнопка під сповіщенням ──────────────────────────────────────

@pytest.mark.parametrize("direction, event, button", [
    ("above", PriceEventType.TARGET_REACHED, "btn_new_target_above"),
    (None, PriceEventType.TARGET_REACHED, "btn_new_target_below"),
    (None, PriceEventType.DECREASE, None),
    (None, PriceEventType.INCREASE, None),
])
async def test_target_notification_has_new_target_button(db, direction, event, button):
    user = await seed_user(1)
    tr = await seed_tracker(user, target="2000")
    bot = make_bot()
    async with get_session() as s:
        row = await s.get(PriceTracker, tr.id)
        row.target_direction = direction
        await pc._send_notification(bot=bot, session=s, tracker=row, event=event, old_price=D("1900"), new_price=D("2100"))
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    if button:
        assert f"newtarget:{tr.id}" in _callbacks(markup) and t("uk", button) in _texts(markup)
    else:
        assert not any(c.startswith("newtarget:") for c in _callbacks(markup))


async def test_mute_toggle_keeps_new_target_button(db):
    user = await seed_user(1)
    tr = await seed_tracker(user, target="2000")
    cb = make_callback(1, f"mute:drop:{tr.id}")
    from app.bot.keyboards import notification_keyboard
    object.__setattr__(cb.message, "reply_markup", notification_keyboard("uk", "drop", tr.id, retarget="below"))
    with capture_bot() as sent:
        await h.toggle_mute_from_notification(cb)
    assert sent[0].callbacks == [f"unmute:drop:{tr.id}", f"newtarget:{tr.id}", f"tsettings:{tr.id}"]


# ─── Діалог нової межі ───────────────────────────────────────────

async def _ask(uid: int, tracker_id: str):
    state = fsm_for(uid)
    with capture_bot() as sent:
        await h.ask_new_target(make_callback(uid, f"newtarget:{tracker_id}"), state)
    return state, sent


async def test_new_bid_limit_above(db):
    user = await seed_user(1)
    tr = await seed_lot(user, price="2100", target="2000", direction="above",
                        ends_at=datetime.utcnow() + timedelta(days=2))
    state, sent = await _ask(1, tr.id)
    assert await state.get_state() == h.NewTargetStates.waiting.state
    ask = next(s for s in sent if s.kind == "answer")
    assert "Ставка зараз: <b>2 100 грн</b>" in ask.text and "Межа була: 2 000 грн" in ask.text

    with capture_bot() as sent:
        await h.process_new_target(make_message(1, "2000"), state)          # не вище поточної ставки
    assert sent[0].text == t("uk", "auction_above_invalid", price="2 100 грн")
    assert (await load_tracker(tr.id)).target_price == D("2000") and await state.get_state() is not None

    with capture_bot() as sent:
        await h.process_new_target(make_message(1, "2 500"), state)
    fresh = await load_tracker(tr.id)
    assert fresh.target_price == D("2500") and fresh.target_direction == "above" and fresh.target_percent is None
    assert sent[0].text == t("uk", "new_target_saved_above", price="2 500 грн")
    assert await state.get_state() is None


async def test_new_bid_limit_in_percent(db):
    user = await seed_user(1)
    tr = await seed_lot(user, price="2000", target="1900", direction="above",
                        ends_at=datetime.utcnow() + timedelta(days=2))
    state, _ = await _ask(1, tr.id)
    with capture_bot():
        await h.process_new_target(make_message(1, "20%"), state)
    fresh = await load_tracker(tr.id)
    assert (fresh.target_price, fresh.target_percent, fresh.baseline_price) == (D("2400"), D("20"), D("2000"))


@pytest.mark.parametrize("text, target, pct", [("3500", D("3500"), None), ("15%", D("3400"), D("15"))])
async def test_new_target_below_for_shop(db, text, target, pct):
    user = await seed_user(1)
    tr = await seed_tracker(user, price="4000", target="4200")
    state, sent = await _ask(1, tr.id)
    assert "Ціна зараз: <b>4 000 грн</b>" in next(s for s in sent if s.kind == "answer").text
    with capture_bot() as sent:
        await h.process_new_target(make_message(1, "4100"), state)          # не нижче поточної ціни
    assert sent[0].text == t("uk", "auction_below_invalid", price="4 000 грн")
    with capture_bot() as sent:
        await h.process_new_target(make_message(1, text), state)
    fresh = await load_tracker(tr.id)
    assert (fresh.target_price, fresh.target_percent) == (target, pct) and fresh.target_direction is None
    assert sent[0].text == t("uk", "new_target_saved_below", price=h.fmt_money(target, "UAH", "uk"))


async def test_new_target_rejects_garbage_and_foreign_and_ended(db):
    owner = await seed_user(1)
    tr = await seed_tracker(owner, price="4000", target="4200")
    state, _ = await _ask(1, tr.id)
    with capture_bot() as sent:
        await h.process_new_target(make_message(1, "дешевше"), state)
    assert sent[0].text == t("uk", "target_invalid") and await state.get_state() is not None   # чекаємо далі
    await state.clear()

    await seed_user(2)
    state2, sent = await _ask(2, tr.id)                                     # чужий трекер
    assert await state2.get_state() is None and any(s.text == t("uk", "no_access") for s in sent)

    ended = await seed_lot(owner, price="2100", target="2000", direction="above",
                           ends_at=datetime.utcnow() - timedelta(hours=1))
    state3, sent = await _ask(1, ended.id)
    assert await state3.get_state() is None and any(s.text == t("uk", "new_target_ended") for s in sent)


async def test_cancel_new_target(db):
    user = await seed_user(1)
    tr = await seed_tracker(user, price="4000", target="4200")
    state, _ = await _ask(1, tr.id)
    with capture_bot():
        await h.cancel_new_target(make_callback(1, "newtarget_cancel"), state)
    assert await state.get_state() is None and (await load_tracker(tr.id)).target_price == D("4200")


# ─── Наскрізно: нова межа знову спрацьовує ───────────────────────

async def test_new_limit_fires_again_after_previous_target_was_notified(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_lot(user, price="2100", target="2000", direction="above",
                        ends_at=datetime.utcnow() + timedelta(days=2))
    async with get_session() as s:                                          # стару межу вже сповістили
        s.add(Notification(user_id=user.id, tracker_id=tr.id, event_type=PriceEventType.TARGET_REACHED,
                           old_price=D("1900"), new_price=D("2100"), status=NotificationStatus.SENT))
    state, _ = await _ask(1, tr.id)
    with capture_bot():
        await h.process_new_target(make_message(1, "2500"), state)

    bot = make_bot()
    patch_scrape(monkeypatch, lot("2300"))
    await pc.check_single_price(await load_tracker(tr.id), bot)
    bot.send_message.assert_not_awaited()                                   # +9,5%, межу ще не перетнули
    patch_scrape(monkeypatch, lot("2600"))
    await pc.check_single_price(await load_tracker(tr.id), bot)
    assert "Ставка перевищила твою межу" in bot.send_message.await_args.kwargs["text"]
    assert (await load_tracker(tr.id)).status == TrackerStatus.ACTIVE and LOT
