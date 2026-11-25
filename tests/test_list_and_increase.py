"""
/list показує, чого саме чекаємо по кожному товару (ціль, напрямок, аукціон, наявність),
і перемикач «Про підвищення ціни» на картці товару (INCREASE ≥3%, за замовчуванням вимкнено).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from app.bot.handlers import main as h
from app.bot.keyboards import tracker_settings_keyboard
from app.database.models import PriceEventType, PriceTracker, TrackerStatus
from app.database.session import get_session
from app.i18n import t
from app.scheduler import price_checker as pc
from tests.helpers import capture_bot, make_bot, make_callback, make_message
from tests.test_scheduler import load_tracker, notifications, patch_scrape, product, seed_tracker, seed_user

NOW = datetime(2026, 10, 19, 12, 0)          # UTC; Київ = UTC+3


def _tr(**kw) -> PriceTracker:
    base = dict(current_price=D("37000"), currency="UAH", source="rozetka.com.ua", is_available=True,
                status=TrackerStatus.ACTIVE, notify_drop=True, notify_restock=True, notify_increase=False,
                target_price=None, target_percent=None, target_direction=None, ends_at=None, remind_before_minutes=None)
    base.update(kw)
    base.setdefault("canonical_url", f"https://{base['source']}/p/1")
    return PriceTracker(**base)


# ─── /list: рядки під назвою товару ──────────────────────────────

@pytest.mark.parametrize("kw, expected", [
    (dict(target_price=D("40000")),
     ["💰 37 000 грн · <a href='https://rozetka.com.ua/p/1'>rozetka.com.ua</a>", "🎯 Чекаю: ціна ≤ 40 000 грн"]),
    (dict(target_price=D("3825"), target_percent=D("15"), current_price=D("4500")),
     ["💰 4 500 грн · <a href='https://rozetka.com.ua/p/1'>rozetka.com.ua</a>", "🎯 Чекаю: −15% (ціна ≤ 3 825 грн)"]),
    (dict(),
     ["💰 37 000 грн · <a href='https://rozetka.com.ua/p/1'>rozetka.com.ua</a>", "📉 Чекаю: знижку від 3% (ціль не задана)"]),
    (dict(notify_increase=True),
     ["💰 37 000 грн · <a href='https://rozetka.com.ua/p/1'>rozetka.com.ua</a>", "📉 Чекаю: знижку від 3% (ціль не задана) · 📈 подорожчання від 3%"]),
    (dict(notify_drop=False),
     ["💰 37 000 грн · <a href='https://rozetka.com.ua/p/1'>rozetka.com.ua</a>", "🔕 Сповіщення про ціну вимкнені"]),
    (dict(is_available=False, current_price=None),
     ["💰 — · <a href='https://rozetka.com.ua/p/1'>rozetka.com.ua</a>", "❌ Немає в наявності — скажу, коли з'явиться"]),
])
def test_list_lines_shop(kw, expected):
    assert h.list_item_lines(_tr(**kw), "uk", now=NOW) == expected


def test_list_lines_auction_above_with_reminder():
    tr = _tr(source="violity.com", current_price=D("1250"), target_price=D("2000"), target_direction="above",
             ends_at=datetime(2026, 10, 21, 15, 0), remind_before_minutes=60)
    assert h.list_item_lines(tr, "uk", now=NOW) == [
        "💰 1 250 грн · <a href='https://violity.com/p/1'>violity.com</a>",
        "🔨 Торги до 21.10 18:00 · ⏰ нагадаю за 1 год",
        "📈 Чекаю: ставка ≥ 2 000 грн",
    ]


def test_list_lines_auction_without_target_and_ended():
    open_lot = _tr(source="violity.com", current_price=D("1250"), ends_at=datetime(2026, 10, 21, 15, 0))
    assert h.list_item_lines(open_lot, "uk", now=NOW)[1:] == [
        "🔨 Торги до 21.10 18:00",
        "👀 Ціль не задана — лише стежу за лотом",
    ]
    ended = _tr(source="violity.com", current_price=D("1250"), ends_at=NOW - timedelta(hours=1),
                target_price=D("2000"), target_direction="above")
    assert h.list_item_lines(ended, "uk", now=NOW)[1:] == ["🏁 Торги завершено"]


def test_list_lines_en():
    tr = _tr(source="violity.com", current_price=D("1250"), target_price=D("2000"), target_direction="above",
             ends_at=datetime(2026, 10, 21, 15, 0), remind_before_minutes=30)
    assert h.list_item_lines(tr, "en", now=NOW)[1:] == [
        "🔨 Bidding ends 21.10 18:00 · ⏰ reminder 30 min before",
        "📈 Waiting for: bid ≥ ₴2,000",
    ]


async def test_list_message_uses_new_lines(db):
    user = await seed_user(1)
    await seed_tracker(user, "https://rozetka.com.ua/p/1", title="EcoFlow", price="37000", target="40000")
    with capture_bot() as sent:
        await h.cmd_list_msg(make_message(1, "/list"))
    text = sent[0].text
    assert "1. ✅ <b>EcoFlow</b>\n   💰 37 000 грн · <a href='https://rozetka.com.ua/p/1'>rozetka.com.ua</a>\n   🎯 Чекаю: ціна ≤ 40 000 грн\n" in text


# ─── Подорожчання: подія, придушення, сповіщення ─────────────────

@pytest.mark.parametrize("old, new, target, direction, expected", [
    (D("100"), D("103"), None, None, PriceEventType.INCREASE),              # рівно 3%
    (D("100"), D("102.99"), None, None, None),                             # менше 3%
    (D("100"), D("150"), D("80"), None, PriceEventType.INCREASE),          # ціль «нижче» не заважає
    (D("1000"), D("1100"), D("2000"), "above", PriceEventType.INCREASE),   # аукціон: ставка росте, межу не досягли
    (D("1000"), D("900"), D("2000"), "above", None),                       # для «перевищить» зниження — не подія
    (D("1000"), D("2100"), D("2000"), "above", PriceEventType.TARGET_REACHED),  # ціль важливіша
])
def test_detect_increase(old, new, target, direction, expected):
    assert pc._detect_event(old_price=old, new_price=new, target_price=target, old_available=True,
                            new_available=True, target_direction=direction) == expected


def test_increase_suppressed_unless_enabled():
    now = datetime.utcnow()
    assert pc._suppression_reason(_tr(), PriceEventType.INCREASE, D("100"), D("110"), now) == "muted_increase"
    assert pc._suppression_reason(_tr(notify_increase=True), PriceEventType.INCREASE, D("100"), D("110"), now) is None
    # «зниження» вимкнене не глушить подорожчання і навпаки
    assert pc._suppression_reason(_tr(notify_drop=False, notify_increase=True), PriceEventType.INCREASE,
                                  D("100"), D("110"), now) is None


async def _set(tracker_id: str, **kw) -> None:
    async with get_session() as s:
        row = await s.get(PriceTracker, tracker_id)
        for k, v in kw.items():
            setattr(row, k, v)


async def test_increase_notification_only_when_enabled(db, monkeypatch):
    user = await seed_user(1)
    tr = await seed_tracker(user, price="1000")
    patch_scrape(monkeypatch, product(price="1100"))
    bot = make_bot()
    await pc.check_single_price(tr, bot)
    bot.send_message.assert_not_awaited()                     # за замовчуванням вимкнено — нікого не турбуємо
    assert await notifications() == []

    await _set(tr.id, notify_increase=True, current_price=D("1000"))
    await pc.check_single_price(tr, bot)
    kw = bot.send_message.await_args.kwargs
    assert "Ціна зросла на 10%" in kw["text"] and "1 100 грн" in kw["text"]
    callbacks = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row]
    assert callbacks == [f"mute:increase:{tr.id}", f"tsettings:{tr.id}"]
    assert [n.event_type for n in await notifications()] == [PriceEventType.INCREASE]


# ─── Картка товару: перемикач і кнопка під сповіщенням ───────────

@pytest.mark.parametrize("auction", [False, True])
def test_settings_keyboard_has_increase_toggle(auction):
    kb = tracker_settings_keyboard("uk", "T1", notify_drop=True, notify_restock=True, notify_increase=False,
                                   auction=auction)
    buttons = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
    assert buttons["ttoggle:increase:T1"] == f"☐ {t('uk', 'toggle_increase')}"


async def test_toggle_increase_on_card_and_mute_from_notification(db):
    user = await seed_user(7)
    tr = await seed_tracker(user)
    with capture_bot() as sent:
        await h.toggle_tracker_setting(make_callback(7, f"ttoggle:increase:{tr.id}"))
    assert (await load_tracker(tr.id)).notify_increase is True
    assert any(c == f"ttoggle:increase:{tr.id}" for s in sent for c in (s.callbacks or []))

    with capture_bot() as sent:
        await h.toggle_mute_from_notification(make_callback(7, f"mute:increase:{tr.id}"))
    fresh = await load_tracker(tr.id)
    assert fresh.notify_increase is False and fresh.notify_drop is True
    assert sent[0].callbacks == [f"unmute:increase:{tr.id}", f"tsettings:{tr.id}"]
    assert sent[1].text == t("uk", "mute_toast_increase")


async def test_new_tracker_has_increase_off(db):
    user = await seed_user(8)
    tr = await seed_tracker(user)
    assert (await load_tracker(tr.id)).notify_increase is False



async def test_list_shows_error_items_last(db):
    """20.10: позиції, що стали ERROR, мовчки зникали з /list — юзер бачив лише ті, що на паузі."""
    user = await seed_user(1)
    await seed_tracker(user, "https://rozetka.com.ua/p/1", title="Зламаний", status=TrackerStatus.ERROR,
                       created_at=datetime.utcnow() - timedelta(days=3))
    await seed_tracker(user, "https://rozetka.com.ua/p/2", title="Активний", created_at=datetime.utcnow() - timedelta(days=2))
    await seed_tracker(user, "https://rozetka.com.ua/p/3", title="Пауза", status=TrackerStatus.PAUSED,
                       created_at=datetime.utcnow() - timedelta(days=1))
    with capture_bot() as sent:
        await h.cmd_list_msg(make_message(1, "/list"))
    text = sent[0].text
    assert "Твої товари (3)" in text
    assert text.index("Активний") < text.index("Пауза") < text.index("Зламаний")
    assert "3. ⚠️ <b>Зламаний</b>" in text and "⚠️ Не вдається перевірити — перевірки зупинено" in text



def test_list_shop_link_opens_the_item_not_the_site():
    """20.10: «rozetka.com.ua» звичайним текстом Telegram сам робив посиланням на головну магазину."""
    tr = _tr(canonical_url="https://rozetka.com.ua/ua/apple-iphone-15/p395460480/?a=1&b='2'")
    assert h.list_item_lines(tr, "uk")[0] == (
        "💰 37 000 грн · <a href='https://rozetka.com.ua/ua/apple-iphone-15/p395460480/?a=1&amp;b=&#x27;2&#x27;'>"
        "rozetka.com.ua</a>")
