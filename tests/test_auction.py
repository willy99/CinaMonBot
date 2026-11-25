"""
Аукціони (Violity, 19.10): ціль «перевищить»/«впаде нижче» і разове нагадування перед кінцем торгів.

- ціль «перевищить»/«впаде нижче»: як і для магазинів, повтор лише при новому перетині межі;
- на аукціоні немає загальних «−3%» сповіщень — лише ціль, яку юзер задав сам;
- нагадування: час кінця береться зі сторінки при додаванні/перевірці, сайт для нагадування не смикаємо;
  один раз на лот, лише власнику, не заблокованим, не після кінця; перенесли торги на >2 год — нагадаємо ще раз;
- при додаванні лота питаємо напрямок; під «Додано!» кнопки «1 год · 30 хв · 15 хв · 5 хв» (типово 1 год),
  ті самі в налаштуваннях товару + «не нагадувати».
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.bot.handlers import main as h
from app.database.models import Notification, PriceEventType, PriceTracker, TrackerStatus, User
from app.database.session import get_session
from app.scheduler import price_checker as pc
from app.scheduler import reminders
from app.services.scrapers.scraper import ProductData, ViolityScraper
from tests.helpers import capture_bot, fsm_for, fsm_storage, make_bot, make_callback, make_message
from tests.test_scheduler import dispatch_spies, load_tracker, patch_scrape, seed_tracker, seed_user  # noqa: F401

LOT = "https://violity.com/ua/124877135"


def lot(price: str = "451153", *, ends_in: timedelta | None = timedelta(days=5), available: bool = True) -> ProductData:
    end = (datetime.now(timezone.utc) + ends_in) if ends_in is not None else None
    return ProductData(title="Орден", price=D(price), old_price=None, currency="UAH", is_available=available,
                       image_url=None, canonical_url=LOT, strategy="violity-lot", ends_at=end)


async def seed_lot(user: User, *, price: str = "1000", target: str | None = None, direction: str | None = None,
                   ends_at: datetime | None = None, remind: int | None = 60, notified: datetime | None = None,
                   status: str = TrackerStatus.ACTIVE) -> PriceTracker:
    tr = await seed_tracker(user, LOT, price=price, target=target, status=status)
    async with get_session() as s:
        row = await s.get(PriceTracker, tr.id)
        row.target_direction, row.ends_at = direction, ends_at
        row.remind_before_minutes, row.ending_notified_at = remind, notified
    return await load_tracker(tr.id)


async def notes() -> list[Notification]:
    async with get_session() as s:
        return (await s.execute(select(Notification))).scalars().all()


@pytest.fixture(autouse=True)
def _reset():
    fsm_storage()
    h._NEW_SITE_NOTIFIED.clear()
    yield


# ─── Перетин межі ────────────────────────────────────────────────

@pytest.mark.parametrize("old, new, target, direction, notified, expected", [
    # «перевищить»: перетин знизу вгору — завжди; уже за межею — лише якщо ще не сповіщали
    (D(900), D(1000), D(1000), "above", True, PriceEventType.TARGET_REACHED),
    (None, D(1200), D(1000), "above", False, PriceEventType.TARGET_REACHED),
    (D(1100), D(1200), D(1000), "above", True, PriceEventType.INCREASE),  # ціль не повторюємо; +9% — подорожчання (opt-in)
    (D(1100), D(1200), D(1000), "above", False, PriceEventType.TARGET_REACHED),
    (D(900), D(950), D(1000), "above", False, PriceEventType.INCREASE),  # межу не досягли; +5,6% — подорожчання (opt-in)
    (D(900), D(920), D(1000), "above", False, None),           # +2,2% — не подія
    (D(1000), D(500), D(2000), "above", False, None),         # падіння для «перевищить» — не подія
    # «впаде нижче» / звичайна ціль
    (D(1100), D(1000), D(1000), "below", True, PriceEventType.TARGET_REACHED),
    (D(1100), D(900), D(1000), None, True, PriceEventType.TARGET_REACHED),
    (D(900), D(899), D(1000), None, True, None),               # уже сповіщали, крок <3% — тиша
    (D(900), D(800), D(1000), None, True, PriceEventType.DECREASE),   # уже під ціллю, але −11% — звичайне падіння
])
def test_target_direction_and_repeat(old, new, target, direction, notified, expected):
    assert pc._detect_event(old_price=old, new_price=new, target_price=target, old_available=True,
                            new_available=True, target_notified=notified, target_direction=direction) == expected


async def test_no_generic_drop_alerts_on_auction(db):
    user = await seed_user(1)
    tr = await seed_lot(user, price="1000")
    now = datetime.now(timezone.utc)
    assert pc._suppression_reason(tracker=tr, event=PriceEventType.DECREASE, old_price=D(1000), new_price=D(500),
                                  now=now) == "auction_no_generic_drop"
    assert pc._suppression_reason(tracker=tr, event=PriceEventType.TARGET_REACHED, old_price=D(1000),
                                  new_price=D(500), now=now) is None


async def test_check_updates_end_and_sends_above_alert(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_lot(user, price="1000", target="1500", direction="above")
    patch_scrape(monkeypatch, lot("1600", ends_in=timedelta(hours=3)))
    bot = make_bot()
    await pc.check_single_price(tr, bot)
    text = bot.send_message.await_args.kwargs["text"]
    assert "Ставка перевищила" in text and "1 600" in text and "1 500" in text
    fresh = await load_tracker(tr.id)
    assert fresh.ends_at is not None and fresh.ends_at.tzinfo is None
    assert abs(fresh.ends_at - (datetime.utcnow() + timedelta(hours=3))) < timedelta(minutes=1)

    bot.send_message.reset_mock()          # наступна перевірка, ставка ще вища — не повторюємо
    patch_scrape(monkeypatch, lot("1700", ends_in=timedelta(hours=3)))
    await pc.check_single_price(fresh, bot)
    bot.send_message.assert_not_awaited()


async def test_extension_far_later_rearms_reminder(db, monkeypatch):
    user = await seed_user(1)
    now = datetime.utcnow()
    tr = await seed_lot(user, ends_at=now + timedelta(minutes=30), notified=now)
    patch_scrape(monkeypatch, lot(ends_in=timedelta(days=2)))           # лот переподали
    await pc.check_single_price(tr, make_bot())
    assert (await load_tracker(tr.id)).ending_notified_at is None

    tr2 = await load_tracker(tr.id)
    async with get_session() as s:
        (await s.get(PriceTracker, tr.id)).ending_notified_at = now
    patch_scrape(monkeypatch, lot(ends_in=timedelta(days=2, minutes=5)))  # +5 хв за пізню ставку
    await pc.check_single_price(tr2, make_bot())
    assert (await load_tracker(tr.id)).ending_notified_at is not None


async def test_dispatcher_skips_long_ended_lots(db, dispatch_spies):
    checked, _, _ = dispatch_spies
    user = await seed_user(1, tier="premium")
    now = datetime.utcnow()
    ended = await seed_lot(user, ends_at=now - timedelta(hours=7))
    just_ended = await seed_tracker(user, "https://violity.com/ua/124877136")
    async with get_session() as s:
        (await s.get(PriceTracker, just_ended.id)).ends_at = now - timedelta(hours=1)   # ще забираємо фінальну ціну
    await pc.dispatch_price_checks(make_bot())
    ids = {t.id for t in checked}
    assert just_ended.id in ids and ended.id not in ids


# ─── Нагадування ─────────────────────────────────────────────────

async def test_reminder_sent_once_to_owner_in_window(db):
    owner = await seed_user(1)
    other = await seed_user(2)
    now = datetime.utcnow()
    tr = await seed_lot(owner, price="451153", ends_at=now + timedelta(minutes=45))
    await seed_tracker(other, "https://violity.com/ua/124877137")       # не аукціонне вікно — нічого
    bot = make_bot()
    assert await reminders.send_ending_reminders(bot, now) == 1
    bot.send_message.assert_awaited_once()
    kw = bot.send_message.await_args.kwargs
    assert kw["chat_id"] == 1 and "До кінця торгів 45 хв" in kw["text"] and "451 153" in kw["text"]
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == f"tsettings:{tr.id}"
    (n,) = await notes()
    assert n.event_type == PriceEventType.ENDING and n.tracker_id == tr.id

    assert await reminders.send_ending_reminders(bot, now + timedelta(minutes=10)) == 0   # разово
    bot.send_message.assert_awaited_once()


@pytest.mark.parametrize("ends_in, remind, expected", [
    (timedelta(minutes=61), 60, 0),     # ще рано
    (timedelta(minutes=59), 60, 1),
    (timedelta(minutes=45), 30, 0),     # налаштовано «за 30 хв»
    (timedelta(minutes=29), 30, 1),
    (timedelta(minutes=10), None, 0),   # вимкнено
    (timedelta(minutes=-1), 60, 0),     # торги вже завершились
])
async def test_reminder_window(db, ends_in, remind, expected):
    user = await seed_user(1)
    now = datetime.utcnow()
    await seed_lot(user, ends_at=now + ends_in, remind=remind)
    assert await reminders.send_ending_reminders(make_bot(), now) == expected


async def test_reminder_skips_blocked_paused_and_marks_bot_blocked(db):
    now = datetime.utcnow()
    end = now + timedelta(minutes=20)
    blocked = await seed_user(1, blocked=True)
    await seed_lot(blocked, ends_at=end)
    paused_owner = await seed_user(2)
    await seed_lot(paused_owner, ends_at=end, status=TrackerStatus.PAUSED)
    gone = await seed_user(3)
    tr = await seed_lot(gone, ends_at=end)
    bot = make_bot()
    bot.send_message.side_effect = Exception("Forbidden: bot was blocked by the user")
    assert await reminders.send_ending_reminders(bot, now) == 0
    assert [c.kwargs["chat_id"] for c in bot.send_message.await_args_list] == [3]
    async with get_session() as s:
        assert (await s.get(User, gone.id)).bot_blocked is True
    assert (await load_tracker(tr.id)).ending_notified_at is not None     # не ретраїмо щохвилини


def test_reminder_job_registered(monkeypatch):
    monkeypatch.setattr(pc.scheduler, "start", lambda *a, **k: None)
    pc.setup_scheduler(make_bot())
    try:
        job = pc.scheduler.get_job("ending_reminders")
        assert job.func is reminders.send_ending_reminders and job.max_instances == 1 and job.coalesce
        assert job.trigger.interval == timedelta(minutes=1)
    finally:
        pc.scheduler.remove_all_jobs()


# ─── Додавання лота ──────────────────────────────────────────────

async def _open_lot(monkeypatch, result: ProductData, uid: int = 1):
    async def scrape(self, url):
        return result
    monkeypatch.setattr(ViolityScraper, "scrape", scrape)
    state = fsm_for(uid)
    await state.set_state(h.AddTrackerStates.waiting_for_url)
    with capture_bot() as sent:
        await h.process_url(make_message(uid, LOT), state)
    return state, sent


async def _tracker(uid: int = 1) -> PriceTracker | None:
    async with get_session() as s:
        return (await s.execute(select(PriceTracker).join(User).where(User.telegram_id == uid))).scalar_one_or_none()


async def test_add_lot_asks_direction_then_saves_above_target_and_reminder(db, monkeypatch):
    state, sent = await _open_lot(monkeypatch, lot("451153"))
    card = sent[-1]
    assert "Поточна ставка" in card.text and "Торги до" in card.text
    assert card.callbacks == ["tdir:above", "tdir:below", "skip_target"]

    with capture_bot() as sent:
        await h.choose_target_direction(make_callback(1, "tdir:above"), state)
    assert "вищу за поточну" in sent[0].text

    with capture_bot() as sent:
        await h.process_target(make_message(1, "400000"), state)      # нижче за ставку — помилка
    assert "більшою за поточну ставку" in sent[-1].text and await _tracker() is None

    with capture_bot() as sent:
        await h.process_target(make_message(1, "500000"), state)
    added = sent[-1]
    assert "перевищить 500 000" in added.text and "Коли нагадати" in added.text
    tr = await _tracker()
    assert tr.target_direction == "above" and tr.target_price == D("500000")
    assert tr.remind_before_minutes == 60 and tr.ends_at is not None and tr.ends_at.tzinfo is None
    assert added.buttons == ["✅ 1 год", "30 хв", "15 хв", "5 хв"]
    assert added.callbacks == [f"rem:{m}:{tr.id}" for m in (60, 30, 15, 5)]


async def test_add_lot_percent_up_and_down(db, monkeypatch):
    state, _ = await _open_lot(monkeypatch, lot("1000"))
    with capture_bot():
        await h.choose_target_direction(make_callback(1, "tdir:above"), state)
        await h.process_target(make_message(1, "150%"), state)
    tr = await _tracker()
    assert tr.target_price == D("2500.00") and tr.target_percent == D("150") and tr.target_direction == "above"

    state, _ = await _open_lot(monkeypatch, lot("1000"), uid=2)
    with capture_bot() as sent:
        await h.choose_target_direction(make_callback(2, "tdir:below"), state)
        await h.process_target(make_message(2, "1200"), state)
    assert "меншою за поточну" in sent[-1].text
    with capture_bot():
        await h.process_target(make_message(2, "20%"), state)
    tr = await _tracker(2)
    assert tr.target_price == D("800.00") and tr.target_direction == "below"


async def test_amount_before_direction_asks_direction(db, monkeypatch):
    state, _ = await _open_lot(monkeypatch, lot("1000"))
    with capture_bot() as sent:
        await h.process_target(make_message(1, "2000"), state)
    assert "Спершу обери" in sent[-1].text and sent[-1].callbacks[:2] == ["tdir:above", "tdir:below"]
    assert await _tracker() is None


async def test_skip_on_lot_keeps_reminder(db, monkeypatch):
    state, _ = await _open_lot(monkeypatch, lot("1000"))
    with capture_bot() as sent:
        await h.skip_target(make_callback(1, "skip_target"), state)
    tr = await _tracker()
    assert tr.target_price is None and tr.target_direction is None and tr.remind_before_minutes == 60
    assert any("Коли нагадати" in x.text and x.callbacks[0] == f"rem:60:{tr.id}" for x in sent)


async def test_ended_lot_is_not_added(db, monkeypatch):
    state, sent = await _open_lot(monkeypatch, lot("750", ends_in=timedelta(days=-3), available=False))
    assert "Торги вже завершились" in sent[-1].text
    assert await state.get_state() is None


# ─── Налаштування й /list ────────────────────────────────────────

async def test_reminder_buttons_after_add(db):
    owner = await seed_user(1)
    await seed_user(2)
    now = datetime.utcnow()
    tr = await seed_lot(owner, ends_at=now + timedelta(days=1), notified=now)
    for m in (15, 5, 30):
        with capture_bot() as sent:
            await h.choose_reminder(make_callback(1, f"rem:{m}:{tr.id}"))
        assert (await load_tracker(tr.id)).remind_before_minutes == m
    marks = next(x for x in sent if x.kind == "markup")
    assert marks.buttons == ["1 год", "✅ 30 хв", "15 хв", "5 хв"]
    assert any(x.kind == "callback" and "за 30 хв" in x.text for x in sent)
    assert (await load_tracker(tr.id)).ending_notified_at is None         # новий час — нагадаємо знову

    for data in (f"rem:5:{tr.id}", f"rem:45:{tr.id}", "rem:x:y"):        # чужий / не з меню / сміття
        with capture_bot():
            await h.choose_reminder(make_callback(2 if data.startswith("rem:5") else 1, data))
    assert (await load_tracker(tr.id)).remind_before_minutes == 30


async def test_reminder_buttons_in_settings_incl_off(db):
    owner = await seed_user(1)
    tr = await seed_lot(owner, ends_at=datetime.utcnow() + timedelta(days=1))
    with capture_bot() as sent:
        await h.open_tracker_settings_new(make_callback(1, f"tsettings:{tr.id}"))
    card = next(x for x in sent if x.reply_markup)
    assert [f"trem:{m}:{tr.id}" for m in (60, 30, 15, 5, 0)] == [c for c in card.callbacks if c.startswith("trem:")]
    assert "✅ 1 год" in card.buttons
    assert not any((c or "").startswith("ttoggle:restock") for c in card.callbacks)

    with capture_bot() as sent:
        await h.choose_reminder_in_settings(make_callback(1, f"trem:0:{tr.id}"))
    assert (await load_tracker(tr.id)).remind_before_minutes is None
    card = next(x for x in sent if x.reply_markup)
    assert "✅ 🔕 Не нагадувати про кінець торгів" in card.buttons
    with capture_bot():
        await h.choose_reminder_in_settings(make_callback(1, f"trem:5:{tr.id}"))
    assert (await load_tracker(tr.id)).remind_before_minutes == 5


@pytest.mark.parametrize("ends_in, remind, expected", [
    (timedelta(minutes=16), 15, 0), (timedelta(minutes=14), 15, 1),
    (timedelta(minutes=6), 5, 0), (timedelta(minutes=4), 5, 1),
])
async def test_short_reminder_windows(db, ends_in, remind, expected):
    user = await seed_user(1)
    now = datetime.utcnow()
    await seed_lot(user, ends_at=now + ends_in, remind=remind)
    assert await reminders.send_ending_reminders(make_bot(), now) == expected


async def test_shop_settings_have_no_reminder_row(db):
    user = await seed_user(1)
    tr = await seed_tracker(user)
    with capture_bot() as sent:
        await h.open_tracker_settings_new(make_callback(1, f"tsettings:{tr.id}"))
    card = next(x for x in sent if x.reply_markup)
    assert not any((c or "").startswith("trem:") for c in card.callbacks)


async def test_list_shows_above_target(db):
    user = await seed_user(1)
    tr = await seed_lot(user, target="5000", direction="above")
    assert h.list_item_lines(tr, "uk")[-1] == "📈 Чекаю: ставка ≥ 5 000 грн"


# ─── Результат торгів: продано → пауза, не продано → стежимо далі ─

def ended_lot(outcome: str | None, price: str = "220") -> ProductData:
    return ProductData(title="ЖД Вагон", price=D(price), old_price=None, currency="UAH", is_available=False,
                       image_url=None, canonical_url=LOT, strategy="violity-lot",
                       ends_at=datetime.now(timezone.utc) - timedelta(minutes=5), lot_outcome=outcome)


async def test_sold_lot_is_paused_on_scheduled_check(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_lot(user, price="200", ends_at=datetime.utcnow() + timedelta(minutes=1))
    patch_scrape(monkeypatch, ended_lot("sold"))
    await pc.check_single_price(tr, make_bot())
    fresh = await load_tracker(tr.id)
    assert fresh.status == TrackerStatus.PAUSED and fresh.lot_outcome == "sold"
    assert h.list_item_lines(fresh, "uk")[1:] == ["🏁 Лот продано"]
    assert h.list_item_lines(fresh, "en")[1:] == ["🏁 Lot sold"]


async def test_unsold_lot_stays_active_and_relist_clears_outcome(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_lot(user, price="200", ends_at=datetime.utcnow() + timedelta(minutes=1))
    patch_scrape(monkeypatch, ended_lot("unsold", price="200"))
    await pc.check_single_price(tr, make_bot())
    fresh = await load_tracker(tr.id)
    assert fresh.status == TrackerStatus.ACTIVE and fresh.lot_outcome == "unsold"   # можуть виставити знову
    assert h.list_item_lines(fresh, "uk")[1:] == ["🏁 Торги завершено, лот не продано"]

    patch_scrape(monkeypatch, lot("250", ends_in=timedelta(days=3)))                 # перевиставили
    await pc.check_single_price(fresh, make_bot())
    again = await load_tracker(tr.id)
    assert again.lot_outcome is None and again.is_available is True


async def test_ended_without_known_outcome_is_just_ended(db):
    user = await seed_user(1)
    tr = await seed_lot(user, ends_at=datetime.utcnow() - timedelta(minutes=1))
    assert h.list_item_lines(tr, "uk")[1:] == ["🏁 Торги завершено"]


# ─── Під кінець торгів — частіше (Premium) ───────────────────────
# Ставка в останні хвилини продовжує лот на +5 хв: за AUCTION_END_WINDOW_MINUTES (20) до кінця
# Premium-лот перевіряємо раз на AUCTION_END_INTERVAL_MINUTES (2); магазини — як і раніше, раз на 60 хв.

async def _lot_checked(tier: str, *, ends_in: timedelta, checked_ago: timedelta, available: bool = True) -> PriceTracker:
    user = await seed_user(1, tier=tier)
    tr = await seed_lot(user, ends_at=datetime.utcnow() + ends_in)
    async with get_session() as s:
        row = await s.get(PriceTracker, tr.id)
        row.last_checked_at = datetime.utcnow() - checked_ago
        row.is_available = available
    return tr


@pytest.mark.parametrize("tier, ends_in, checked_ago, available, due", [
    ("premium", timedelta(minutes=10), timedelta(minutes=2), True, True),       # у вікні — раз на 2 хв
    ("premium", timedelta(minutes=10), timedelta(seconds=100), True, True),     # допуск 30 с: тік щохвилини
    ("premium", timedelta(minutes=10), timedelta(minutes=1), True, False),
    ("premium", timedelta(minutes=19), timedelta(minutes=2), True, True),       # межа вікна (20 хв)
    ("premium", timedelta(minutes=40), timedelta(minutes=30), True, False),     # до вікна — раз на 60 хв
    ("premium", timedelta(minutes=-5), timedelta(minutes=2), True, True),       # щойно скінчилось: +5 хв / результат
    ("premium", timedelta(minutes=-30), timedelta(minutes=30), True, False),
    ("premium", timedelta(minutes=10), timedelta(minutes=2), False, False),     # сторінка вже каже «завершено»
    ("free", timedelta(minutes=10), timedelta(minutes=30), True, False),        # безкоштовний — раз на 6 год
])
async def test_premium_lot_checked_every_2_min_near_end(db, dispatch_spies, tier, ends_in, checked_ago, available, due):
    checked, _, _ = dispatch_spies
    tr = await _lot_checked(tier, ends_in=ends_in, checked_ago=checked_ago, available=available)
    await pc.dispatch_price_checks(make_bot())
    assert ([t.id for t in checked] == [tr.id]) is due


async def test_shops_stay_hourly_for_premium(db, dispatch_spies):
    checked, _, _ = dispatch_spies
    user = await seed_user(1, tier="premium")
    await seed_tracker(user, "https://rozetka.com.ua/p/1", last_checked=datetime.utcnow() - timedelta(minutes=50))
    due = await seed_tracker(user, "https://rozetka.com.ua/p/2", last_checked=datetime.utcnow() - timedelta(minutes=61))
    await pc.dispatch_price_checks(make_bot())
    assert [t.id for t in checked] == [due.id]
