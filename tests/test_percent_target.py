"""
Ціль у відсотках ("скажи, коли впаде на 15%"): парсинг, округлення, збереження, /list,
текст сповіщення TARGET_REACHED, форма рядка в адмінці.

    venv/bin/python -m pytest tests/test_percent_target.py -q
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.admin import queries as q
from app.bot.handlers import main as h
from app.database.models import PriceEventType, PriceTracker, TrackerStatus, User
from app.database.session import get_session
from app.i18n import fmt_percent, t
from app.scheduler import price_checker as pc
from app.services.scrapers import scraper as scraper_mod
from app.services.scrapers.scraper import ProductData
from tests.helpers import capture_bot, fsm_for, fsm_storage, make_bot, make_callback, make_message

D = Decimal


@pytest.fixture(autouse=True)
def _isolate():
    fsm_storage()
    h._NEW_SITE_NOTIFIED.clear()
    yield
    h._NEW_SITE_NOTIFIED.clear()


def product(price: str | None = "4500", currency: str = "UAH", url: str = "https://shop.example/p/1") -> ProductData:
    return ProductData(title="Marshall Major IV", price=D(price) if price else None, old_price=None,
                       currency=currency, is_available=True, image_url=None, canonical_url=url, strategy="json-ld")


async def prime_add(uid: int, monkeypatch, lang: str = "uk", **overrides) -> list:
    monkeypatch.setattr(scraper_mod.AbstractScraper, "scrape", AsyncMock(return_value=product(**overrides)))
    with capture_bot() as sent:
        await h.cmd_add(make_message(uid, "/add", lang), fsm_for(uid))
        await h.process_url(make_message(uid, overrides.get("url", "https://shop.example/p/1"), lang), fsm_for(uid))
    return sent


async def trackers_of(uid: int) -> list[PriceTracker]:
    async with get_session() as s:
        user = (await s.execute(select(User).where(User.telegram_id == uid))).scalar_one()
        return list((await s.execute(select(PriceTracker).where(PriceTracker.user_id == user.id))).scalars().all())


# ─── parse_target: таблиця ───────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("15%", ("percent", D("15"))),
    ("15 %", ("percent", D("15"))),
    ("-15%", ("percent", D("15"))),
    ("−15%", ("percent", D("15"))),           # типографський мінус
    ("15.5%", ("percent", D("15.5"))),
    ("15,5%", ("percent", D("15.5"))),
    ("  20%  ", ("percent", D("20"))),
    ("1%", ("percent", D("1"))),
    ("90%", ("percent", D("90"))),
    ("0.5%", ("percent", D("0.5"))),           # поза межами — але це відсоток; межі перевіряє хендлер
    ("95%", ("percent", D("95"))),
    ("4500", ("amount", D("4500"))),
    ("4 500,50", ("amount", D("4500.50"))),
    ("249.99", ("amount", D("249.99"))),
])
def test_parse_target_valid(text, expected):
    assert h.parse_target(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "%", "abc%", "-5", "0", "0%", "-0%", "12abc", "1e", "∞", "%15", "15%%", "nan%"])
def test_parse_target_invalid(text):
    assert h.parse_target(text) is None


# ─── Округлення цілі ─────────────────────────────────────────────

@pytest.mark.parametrize("baseline, pct, expected", [
    ("4500", "15", "3825.00"),
    ("3249", "9", "2956.59"),
    ("299.99", "15", "254.99"),      # 254.9915 → 254.99
    ("100", "33.33", "66.67"),
    ("10.01", "50", "5.01"),         # 5.005 → 5.01 (HALF_UP, не banker's)
    ("1000", "1", "990.00"),
    ("1000", "90", "100.00"),
])
def test_percent_target_price_rounding(baseline, pct, expected):
    got = h.percent_target_price(D(baseline), D(pct))
    assert got == D(expected) and got.as_tuple().exponent == -2


@pytest.mark.parametrize("pct, lang, expected", [
    (D("15"), "uk", "15"), (D("15.00"), "en", "15"), (D("15.50"), "uk", "15,5"),
    (D("15.5"), "en", "15.5"), (D("0.25"), "en", "0.25"), (None, "uk", "—"),
])
def test_fmt_percent(pct, lang, expected):
    assert fmt_percent(pct, lang) == expected


# ─── process_target: відсоток зберігається як baseline + target_price ──

async def test_percent_target_saves_baseline_percent_and_absolute_target(db, monkeypatch):
    await prime_add(1, monkeypatch, "uk")
    state = fsm_for(1)
    with capture_bot() as sent:
        await h.process_target(make_message(1, "15%", "uk"), state)
    assert await state.get_state() is None
    (tr,) = await trackers_of(1)
    assert tr.target_percent == D("15") and tr.baseline_price == D("4500") and tr.target_price == D("3825.00")
    assert tr.status == TrackerStatus.ACTIVE
    assert "🎯 Ціль: −15 % (≤ 3 825 грн)" in sent[-1].text


async def test_percent_target_added_text_en(db, monkeypatch):
    await prime_add(2, monkeypatch, "en", currency="USD", price="299.99")
    with capture_bot() as sent:
        await h.process_target(make_message(2, "15.5 %", "en"), fsm_for(2))
    (tr,) = await trackers_of(2)
    assert tr.target_percent == D("15.5") and tr.target_price == D("253.49")
    assert "🎯 Target: −15.5 % (≤ $253.49)" in sent[-1].text


async def test_absolute_target_leaves_percent_fields_empty(db, monkeypatch):
    await prime_add(3, monkeypatch, "uk")
    with capture_bot() as sent:
        await h.process_target(make_message(3, "4000", "uk"), fsm_for(3))
    (tr,) = await trackers_of(3)
    assert tr.target_price == D("4000") and tr.target_percent is None and tr.baseline_price is None
    assert "🎯 Ціль: 4 000 грн" in sent[-1].text


@pytest.mark.parametrize("text", ["0.5%", "0%", "91%", "150%"])
async def test_percent_out_of_range_keeps_waiting(db, monkeypatch, text):
    await prime_add(4, monkeypatch, "en")
    state = fsm_for(4)
    with capture_bot() as sent:
        await h.process_target(make_message(4, text, "en"), state)
    if text in ("0%",):
        assert sent[-1].text == t("en", "target_invalid")
    else:
        assert sent[-1].text == t("en", "target_percent_range")
    assert await state.get_state() == h.AddTrackerStates.waiting_for_target_price.state
    assert await trackers_of(4) == []


async def test_percent_without_known_price_asks_for_amount_and_keeps_state(db, monkeypatch):
    """Ціна не спарсилась (None) → відсоток неможливий: локалізована помилка, стан лишається, сума приймається."""
    await prime_add(5, monkeypatch, "uk", price=None)
    state = fsm_for(5)
    with capture_bot() as sent:
        await h.process_target(make_message(5, "15%", "uk"), state)
    assert sent[-1].text == t("uk", "target_percent_no_price")
    assert await state.get_state() == h.AddTrackerStates.waiting_for_target_price.state
    assert await trackers_of(5) == []

    with capture_bot() as sent:
        await h.process_target(make_message(5, "3000", "uk"), state)
    (tr,) = await trackers_of(5)
    assert tr.target_price == D("3000") and tr.target_percent is None and tr.current_price is None
    assert "Додано" in sent[-1].text


async def test_percent_without_known_price_en(db, monkeypatch):
    await prime_add(6, monkeypatch, "en", price=None)
    with capture_bot() as sent:
        await h.process_target(make_message(6, "20%", "en"), fsm_for(6))
    assert sent[-1].text == t("en", "target_percent_no_price")


async def test_ask_target_mentions_both_forms(db, monkeypatch):
    for uid, lang, frag in [(7, "uk", "15%"), (8, "en", "15%")]:
        sent = await prime_add(uid, monkeypatch, lang)
        assert frag in sent[-1].text and "4500" in sent[-1].text
    assert "Введи суму в UAH" in t("uk", "ask_target", title="x", price="1", avail="", currency="UAH")
    assert "Enter an amount in UAH" in t("en", "ask_target", title="x", price="1", avail="", currency="UAH")


async def test_invalid_target_text_mentions_percent(db, monkeypatch):
    await prime_add(9, monkeypatch, "uk")
    with capture_bot() as sent:
        await h.process_target(make_message(9, "abc", "uk"), fsm_for(9))
    assert "15%" in sent[-1].text and "4500" in sent[-1].text


# ─── /list ───────────────────────────────────────────────────────

async def seed_user(telegram_id: int, lang: str = "uk") -> User:
    async with get_session() as s:
        user = User(telegram_id=telegram_id, username="u", first_name="U", language_code=lang)
        s.add(user)
    return user


async def seed_tracker(user: User, url: str, *, title: str, price: str = "4500", target: str | None = None,
                       pct: str | None = None, baseline: str | None = None, currency: str = "UAH") -> PriceTracker:
    async with get_session() as s:
        tr = PriceTracker(user_id=user.id, url=url, canonical_url=url, source="shop.example", title=title,
                          current_price=D(price), currency=currency, target_price=D(target) if target else None,
                          target_percent=D(pct) if pct else None, baseline_price=D(baseline) if baseline else None,
                          status=TrackerStatus.ACTIVE)
        s.add(tr)
    return tr


@pytest.mark.parametrize("lang, pct_line, abs_line", [
    ("uk", "🎯 Чекаю: −15% (ціна ≤ 3 825 грн)", "🎯 Чекаю: ціна ≤ 4 000 грн"),
    ("en", "🎯 Waiting for: −15% (price ≤ ₴3,825)", "🎯 Waiting for: price ≤ ₴4,000"),
])
async def test_list_renders_percent_and_absolute_targets(db, lang, pct_line, abs_line):
    uid = 100 if lang == "uk" else 101
    user = await seed_user(uid, lang)
    await seed_tracker(user, "https://shop.example/p/pct", title="Pct", target="3825", pct="15", baseline="4500")
    await seed_tracker(user, "https://shop.example/p/abs", title="Abs", target="4000")
    await seed_tracker(user, "https://shop.example/p/none", title="NoTarget")
    with capture_bot() as sent:
        await h.cmd_list_msg(make_message(uid, "/list", lang))
    text = sent[0].text
    assert pct_line in text and abs_line in text
    none_block = text.split("NoTarget")[1].split("\n")[2]
    assert "🎯" not in none_block and "📉" in none_block


def test_list_expect_line_targets():
    def line(lang, **kw):
        tr = PriceTracker(currency=kw.pop("currency", "UAH"), notify_drop=True, notify_increase=False,
                          is_available=True, source="shop.example", **kw)
        return h.list_item_lines(tr, lang)[-1]
    assert line("uk", target_price=None) == "📉 Чекаю: знижку від 3% (ціль не задана)"
    assert line("uk", target_price=D("3825"), target_percent=D("15.5")) == "🎯 Чекаю: −15,5% (ціна ≤ 3 825 грн)"
    assert line("en", target_price=D("3825"), target_percent=D("15.5")) == "🎯 Waiting for: −15.5% (price ≤ ₴3,825)"
    assert line("en", target_price=D("3825"), currency="USD") == "🎯 Waiting for: price ≤ $3,825"


# ─── Сповіщення TARGET_REACHED ───────────────────────────────────

async def test_target_notification_mentions_percent_when_present(db):
    user = await seed_user(200, "uk")
    tr = await seed_tracker(user, "https://shop.example/p/1", title="Marshall Major IV",
                            target="3825", pct="15", baseline="4500")
    bot = make_bot()
    async with get_session() as s:
        t_ = await s.get(PriceTracker, tr.id)
        await pc._send_notification(bot=bot, session=s, tracker=t_, event=PriceEventType.TARGET_REACHED,
                                    old_price=D("4500"), new_price=D("3800"))
    kw = bot.send_message.await_args.kwargs
    assert kw["chat_id"] == 200
    assert kw["text"] == t("uk", "notif_target_percent", pct="15", title="Marshall Major IV", price="3 800 грн",
                           baseline="4 500 грн", target="3 825 грн", url="https://shop.example/p/1")
    assert "впала на 15 %" in kw["text"] and "≤ 3 825 грн" in kw["text"]


async def test_target_notification_absolute_unchanged(db):
    user = await seed_user(201, "en")
    tr = await seed_tracker(user, "https://shop.example/p/1", title="Thing", target="4000")
    bot = make_bot()
    async with get_session() as s:
        t_ = await s.get(PriceTracker, tr.id)
        await pc._send_notification(bot=bot, session=s, tracker=t_, event=PriceEventType.TARGET_REACHED,
                                    old_price=D("4500"), new_price=D("3900"))
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == t("en", "notif_target", title="Thing", price="₴3,900", target="₴4,000", url="https://shop.example/p/1")


async def test_check_single_price_fires_target_reached_for_percent_target(db, monkeypatch):
    """Планувальник не змінювався: new_price <= target_price (порахований від відсотка) → TARGET_REACHED."""
    user = await seed_user(202, "uk")
    tr = await seed_tracker(user, "https://shop.example/p/1", title="Marshall", target="3825", pct="15", baseline="4500")
    monkeypatch.setattr(scraper_mod.AbstractScraper, "scrape", AsyncMock(return_value=product(price="3825")))
    bot = make_bot()
    await pc.check_single_price(tr, bot)
    bot.send_message.assert_awaited_once()
    assert "впала на 15 %" in bot.send_message.await_args.kwargs["text"]

    # 3 826 — ще не ціль: звичайне DECREASE (15% > 3%)
    bot2 = make_bot()
    tr2 = await seed_tracker(user, "https://shop.example/p/2", title="Other", target="3825", pct="15", baseline="4500")
    monkeypatch.setattr(scraper_mod.AbstractScraper, "scrape",
                        AsyncMock(return_value=product(price="3826", url="https://shop.example/p/2")))
    await pc.check_single_price(tr2, bot2)
    assert "знизилась на" in bot2.send_message.await_args.kwargs["text"]


# ─── Адмінка ─────────────────────────────────────────────────────

async def test_admin_user_detail_carries_percent_fields_and_cell(db):
    user = await seed_user(300)
    pct_tr = await seed_tracker(user, "https://shop.example/p/1", title="Pct", target="3825", pct="15", baseline="4500")
    abs_tr = await seed_tracker(user, "https://shop.example/p/2", title="Abs", target="4000")
    none_tr = await seed_tracker(user, "https://shop.example/p/3", title="None")
    d = await q.user_detail(user.id)
    rows = {r["id"]: r for r in d["trackers"]}
    assert rows[pct_tr.id]["target_percent"] == D("15") and rows[pct_tr.id]["baseline_price"] == D("4500")
    assert rows[pct_tr.id]["target"] == D("3825")
    assert rows[abs_tr.id]["target_percent"] is None and rows[abs_tr.id]["baseline_price"] is None
    # Помічник живе в app.admin.theme (не в модулі сторінки: імпорт @ui.page-модуля поза фікстурою
    # `user` ламає перереєстрацію маршрутів у NiceGUI-плагіні)
    from app.admin.theme import target_label
    cell = lambda r: target_label(r["target"], r["currency"], r.get("target_percent"))  # noqa: E731
    assert cell(rows[pct_tr.id]) == "−15 % (≤ 3 825 грн)"
    assert cell(rows[abs_tr.id]) == "4 000 грн"
    assert cell(rows[none_tr.id]) == "—"
    assert target_label(D("3825"), "USD", D("15.5")) == "−15,5 % (≤ $3 825)"
    assert d["digest_enabled"] is True and d["last_digest_at"] is None


async def test_admin_user_card_shows_percent_target(db, user):
    from tests.admin_helpers import login, make_admin, reset_auth_state, table_with
    reset_auth_state()
    owner = await seed_user(301)
    await seed_tracker(owner, "https://shop.example/p/1", title="Pct", target="3825", pct="15", baseline="4500")
    await make_admin()
    await login(user)
    await user.open(f"/users/{owner.id}")
    await user.should_see("📬 дайджест: увімкнено")
    # ui.table не індексується should_see — дивимось у рядки таблиці товарів
    row = table_with(user, "target_str").rows[0]
    assert row["target_str"] == "−15 % (≤ 3 825 грн)" and row["target_percent"] == D("15")
