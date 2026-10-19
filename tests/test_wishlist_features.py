"""
«Список бажань» без інтеграцій із магазинами (19.10):
1) посилання з будь-якого тексту і без /add; 2) масове додавання (текст і файл); 3) список посиланням /share;
попутно — джерело з ?start=<позначка>.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Document, Message, MessageEntity, Update
from aiogram.types import User as TgUser
from sqlalchemy import select

from app.bot import bulk as bulk_mod
from app.bot import links
from app.bot.handlers import inbox as inbox_mod
from app.bot.handlers import main as h
from app.bot.handlers import wishlist as wl
from app.database.models import Feedback, PriceTracker, TrackerStatus, User
from app.database.session import get_session
from app.services.scrapers import registry
from app.services.scrapers.registry import Platform
from app.services.scrapers.scraper import GenericScraper, ProductData, ScrapeError
from tests.helpers import capture_bot, fsm_for, fsm_storage, make_bot, make_callback, make_message

LIMIT = 5


def product(url: str, title: str = "Товар", price: str = "1000", currency: str = "UAH") -> ProductData:
    return ProductData(title=title, price=Decimal(price), old_price=None, currency=currency, is_available=True,
                       image_url=None, canonical_url=url, strategy="json-ld")


def fake_scrape(table: dict[str, object]):
    """GenericScraper.scrape → результат за URL (ProductData / ScrapeError); невідомі — parse_failed."""
    async def scrape(self, url):
        r = table.get(url, ScrapeError(url=url, reason="parse_failed"))
        return r
    return scrape


async def load_user(tid: int) -> User | None:
    async with get_session() as s:
        return (await s.execute(select(User).where(User.telegram_id == tid))).scalar_one_or_none()


async def trackers_of(tid: int) -> list[PriceTracker]:
    async with get_session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == tid))).scalar_one_or_none()
        if not u:
            return []
        return (await s.execute(select(PriceTracker).where(PriceTracker.user_id == u.id)
                                .order_by(PriceTracker.created_at))).scalars().all()


async def seed(tid: int, n: int = 0, *, first_name: str = "Оля", username: str = "olia_secret",
               lang: str = "uk", token: str | None = None) -> User:
    async with get_session() as s:
        u = User(telegram_id=tid, first_name=first_name, username=username, language_code=lang, share_token=token)
        s.add(u); await s.flush()
        base = datetime.utcnow() - timedelta(days=1)
        for i in range(n):
            s.add(PriceTracker(user_id=u.id, url=f"https://shop.example/p/{tid}-{i}", canonical_url=f"https://shop.example/p/{tid}-{i}",
                               source="shop.example", title=f"Товар {i}", current_price=Decimal(100 + i), currency="UAH",
                               target_price=Decimal(50), status=TrackerStatus.ACTIVE, created_at=base + timedelta(minutes=i)))
    return u


@pytest.fixture(autouse=True)
def _reset():
    fsm_storage()
    bulk_mod._RUNNING.clear()
    h._NEW_SITE_NOTIFIED.clear()
    yield


# ─── 1. Розбір посилань ──────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("https://rozetka.com.ua/ua/x/p1/", ["https://rozetka.com.ua/ua/x/p1/"]),
    ("Дивись, що знайшов на ROZETKA: https://rozetka.com.ua/ua/x/p1/.", ["https://rozetka.com.ua/ua/x/p1/"]),
    ("Biorepair (https://www.notino.ua/biorepair/x/)", ["https://www.notino.ua/biorepair/x/"]),
    ("«https://comfy.ua/a.html», https://allo.ua/b/!", ["https://comfy.ua/a.html", "https://allo.ua/b/"]),
    ("https://a.ua/1 https://a.ua/1 https://b.ua/2", ["https://a.ua/1", "https://b.ua/2"]),
    ("https://en.wikipedia.org/wiki/X_(Y)", ["https://en.wikipedia.org/wiki/X_(Y)"]),
    ("канал https://t.me/deals і товар https://kasta.ua/p/9", ["https://kasta.ua/p/9"]),
    ("без посилань", []),
    ("", []),
])
def test_extract_urls(text, expected):
    assert links.extract_urls(text) == expected


def test_extract_urls_entities_hidden_link_and_schemeless():
    text = "Ось товар і ще rozetka.com.ua/ua/y/p2/"
    hidden = MessageEntity(type="text_link", offset=4, length=5, url="https://comfy.ua/hidden")
    bare = MessageEntity(type="url", offset=text.index("rozetka"), length=len("rozetka.com.ua/ua/y/p2/"))
    assert links.extract_urls(text, [hidden, bare]) == ["https://comfy.ua/hidden", "https://rozetka.com.ua/ua/y/p2/"]


def test_extract_urls_limit():
    text = " ".join(f"https://s{i}.ua/p" for i in range(30))
    assert len(links.extract_urls(text, limit=7)) == 7


@pytest.mark.parametrize("name, mime, size, ok", [
    ("wishlist.txt", "text/plain", 500, True), ("list.csv", "text/csv", 10, True),
    ("x.TXT", "", 10, True), ("report.pdf", "application/pdf", 10, False),
    ("huge.txt", "text/plain", 300 * 1024, False), ("empty.txt", "text/plain", 0, False),
])
def test_is_link_list_file(name, mime, size, ok):
    doc = Document(file_id="f", file_unique_id="u", file_name=name, mime_type=mime, file_size=size)
    assert links.is_link_list_file(doc) is ok


@pytest.mark.parametrize("text, payload", [
    ("/start", ""), ("/start fb", "fb"), ("/start wl_Abc-123_x", "wl_Abc-123_x"),
    ("/start bad payload!", ""), ("/start " + "a" * 65, ""),
])
def test_start_payload(text, payload):
    assert h.start_payload(text) == payload


# ─── 1. Посилання без /add і зі «Поділитися» (через диспетчер) ────

@pytest.fixture
def dispatch():
    """Справжній Dispatcher із роутерами в тому ж порядку, що й bot.py."""
    dp = Dispatcher(storage=MemoryStorage())
    for r in (h.router, wl.router, inbox_mod.router):
        r._parent_router = None
        dp.include_router(r)
    bot = Bot(token="123456:test-token")
    counter = {"n": 0}

    async def send(uid: int, text: str | None = None, *, photo_caption: str | None = None) -> None:
        counter["n"] += 1
        msg = {"message_id": counter["n"], "date": int(datetime.now(timezone.utc).timestamp()),
               "chat": {"id": uid, "type": "private"},
               "from": {"id": uid, "is_bot": False, "first_name": "Tester", "language_code": "uk"}}
        if photo_caption is not None:
            msg["photo"] = [{"file_id": "p", "file_unique_id": "pu", "width": 10, "height": 10}]
            msg["caption"] = photo_caption
        else:
            msg["text"] = text
        await dp.feed_update(bot, Update.model_validate({"update_id": counter["n"], "message": msg}, context={"bot": bot}))

    send.dp, send.bot = dp, bot
    yield send
    for r in (h.router, wl.router, inbox_mod.router):
        r._parent_router = None


async def test_plain_link_without_add_opens_product_card(db, dispatch, monkeypatch):
    url = "https://shop.example/p/1"
    monkeypatch.setattr(GenericScraper, "scrape", fake_scrape({url: product(url, "Чайник")}))
    with capture_bot() as sent:
        await dispatch(1, f"Дивись, що знайшов: Чайник {url}")
    card = [x for x in sent if x.kind == "edit"][-1]
    assert "Чайник" in card.text and "Встановити цільову ціну" in card.text
    state = await dispatch.dp.fsm.get_context(dispatch.bot, 1, 1).get_state()
    assert state == h.AddTrackerStates.waiting_for_target_price.state


async def test_photo_with_link_in_caption(db, dispatch, monkeypatch):
    url = "https://shop.example/p/2"
    monkeypatch.setattr(GenericScraper, "scrape", fake_scrape({url: product(url, "Фен")}))
    with capture_bot() as sent:
        await dispatch(1, photo_caption=f"Фен {url}")
    assert any("Фен" in x.text for x in sent if x.kind == "edit")


async def test_feedback_with_link_stays_feedback(db, dispatch, monkeypatch):
    """Режим фідбеку має пріоритет: посилання в тексті відгуку — це відгук, а не новий товар."""
    scrape = AsyncMock()
    monkeypatch.setattr(GenericScraper, "scrape", scrape)
    ctx = dispatch.dp.fsm.get_context(dispatch.bot, 1, 1)
    await ctx.set_state(h.FeedbackState.waiting)
    with capture_bot(), patch.object(Bot, "send_message", AsyncMock(return_value=MagicMock(message_id=1))):
        await dispatch(1, "Бот не бачить ціну тут https://shop.example/p/3")
    scrape.assert_not_awaited()
    async with get_session() as s:
        assert (await s.execute(select(Feedback))).scalars().all()
    assert await trackers_of(1) == []


async def test_target_step_not_intercepted(db, dispatch, monkeypatch):
    url = "https://shop.example/p/4"
    monkeypatch.setattr(GenericScraper, "scrape", fake_scrape({url: product(url, "Пилосос", "2000")}))
    with capture_bot():
        await dispatch(1, url)
    with capture_bot() as sent:
        await dispatch(1, "15%")
    assert any("Додано" in x.text for x in sent)
    (tr,) = await trackers_of(1)
    assert tr.target_percent == Decimal("15")


async def test_text_without_link_gets_hint_and_unknown_command_is_silent(db, dispatch):
    with capture_bot() as sent:
        await dispatch(1, "привіт")
    assert sent and "надішли посилання" in sent[-1].text
    with capture_bot() as sent:
        await dispatch(1, "/foo")
    assert sent == []


async def test_inbox_router_is_last_and_stateless():
    from aiogram.filters import StateFilter
    for handler in inbox_mod.router.message.handlers:
        assert any(isinstance(f.callback, StateFilter) for f in handler.filters)
    src = open("bot.py", encoding="utf-8").read()
    assert src.index("include_router(router)") < src.index("include_router(wishlist_router)") < src.index("include_router(inbox_router)")


# ─── 2. Масове додавання ─────────────────────────────────────────

async def test_bulk_mixed_results_single_progress_message(db, monkeypatch):
    await seed(1, 0)
    ok1, ok2, dup, bad = ("https://shop.example/a", "https://other.example/b", "https://shop.example/dup", "https://shop.example/x")
    async with get_session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == 1))).scalar_one()
        s.add(PriceTracker(user_id=u.id, url=dup, canonical_url=dup, source="shop.example", title="Вже є", status="active"))
    monkeypatch.setattr(GenericScraper, "scrape", fake_scrape({
        ok1: product(ok1, "Перший", "250"), ok2: product(ok2, "Другий", "99.90", "EUR"),
        dup: product(dup, "Вже є"), bad: ScrapeError(url=bad, reason="parse_failed"),
    }))
    monkeypatch.setattr(registry, "PLATFORMS", [Platform("blocked.example", "Blocky", "UA", "blocked", "")])
    bot = make_bot()
    text = f"мій список: {ok1} {ok2}\n{dup}\n{bad}\nhttps://blocked.example/z"
    with capture_bot() as sent:
        await h.process_url(make_message(1, text, bot=bot), fsm_for(1))
    answers = [x for x in sent if x.kind == "answer"]
    assert len(answers) == 1 and "Додаю товари: 0 з 5" in answers[0].text        # одне повідомлення-прогрес
    summary = sent[-1].text
    assert sent[-1].kind == "edit" and "Готово" in summary
    assert "Додано: 2" in summary and "Перший — 250 грн" in summary and "Другий — €99,90" in summary
    assert "Вже стежу: 1" in summary and "Не вдалося: 2" in summary
    assert "не знайшов ціну" in summary and "Blocky — сайт блокує" in summary
    titles = sorted(tr.title for tr in await trackers_of(1))
    assert titles == ["Вже є", "Другий", "Перший"]
    # нові сайти — одне зведене повідомлення адміну
    bot.send_message.assert_awaited_once()
    assert "shop.example" in bot.send_message.await_args.kwargs["text"] and "other.example" in bot.send_message.await_args.kwargs["text"]


async def test_bulk_cap_and_free_limit(db, monkeypatch):
    await seed(1, 4)
    urls = [f"https://shop.example/n{i}" for i in range(bulk_mod.BULK_MAX_ITEMS + 3)]
    monkeypatch.setattr(GenericScraper, "scrape", fake_scrape({u: product(u, f"N{i}") for i, u in enumerate(urls)}))
    user = await load_user(1)
    with capture_bot() as sent:
        res = await bulk_mod.bulk_add(make_message(1, "x"), user, urls)
    assert len(res.added) == bulk_mod.BULK_MAX_ITEMS and res.skipped == 3
    statuses = [tr.status for tr in await trackers_of(1)]
    assert statuses.count(TrackerStatus.ACTIVE) == LIMIT and statuses.count(TrackerStatus.PAUSED_LIMIT) == 4 + 20 - LIMIT
    assert "ще 3 не додано" in sent[-1].text and "На паузі через ліміт" in sent[-1].text


async def test_bulk_one_at_a_time_per_user(db):
    user = await seed(1)
    bulk_mod._RUNNING.add(1)
    with capture_bot() as sent:
        assert await bulk_mod.bulk_add(make_message(1, "x"), user, ["https://a.ua/1", "https://b.ua/2"]) is None
    assert "Ще додаю попередній список" in sent[-1].text


async def test_bulk_item_crash_does_not_stop_import(db, monkeypatch):
    user = await seed(1)
    good = "https://shop.example/good"
    async def scrape(self, url):
        if "boom" in url:
            raise RuntimeError("kaboom")
        return product(url, "Добрий")
    monkeypatch.setattr(GenericScraper, "scrape", scrape)
    with capture_bot() as sent:
        res = await bulk_mod.bulk_add(make_message(1, "x"), user, ["https://shop.example/boom", good])
    assert len(res.added) == 1 and res.failed == [("shop.example", "network")]
    assert "сайт не відповів" in sent[-1].text


async def test_file_with_links(db, dispatch, monkeypatch):
    urls = ["https://shop.example/f1", "https://shop.example/f2"]
    monkeypatch.setattr(GenericScraper, "scrape", fake_scrape({u: product(u, u[-2:]) for u in urls}))

    async def download(self, file, destination=None, **kw):
        destination.write(("Мій список\n" + "\n".join(urls) + "\n").encode())
        return destination
    monkeypatch.setattr(Bot, "download", download)
    upd = {"update_id": 99, "message": {
        "message_id": 99, "date": int(datetime.now(timezone.utc).timestamp()), "chat": {"id": 1, "type": "private"},
        "from": {"id": 1, "is_bot": False, "first_name": "T", "language_code": "uk"},
        "document": {"file_id": "d", "file_unique_id": "du", "file_name": "wishlist.txt", "mime_type": "text/plain", "file_size": 120}}}
    with capture_bot() as sent:
        await dispatch.dp.feed_update(dispatch.bot, Update.model_validate(upd, context={"bot": dispatch.bot}))
    assert "Додано: 2" in sent[-1].text
    assert len(await trackers_of(1)) == 2


# ─── 3. Список бажань посиланням ─────────────────────────────────

def bot_with_name() -> MagicMock:
    bot = make_bot()
    bot.me = AsyncMock(return_value=MagicMock(username="cina_mon_bot"))
    return bot


async def test_share_empty_list_issues_no_token(db):
    await seed(1, 0)
    with capture_bot() as sent:
        await wl.cmd_share(make_message(1, "/share", bot=bot_with_name()))
    assert "Список бажань порожній" in sent[-1].text
    assert (await load_user(1)).share_token is None


async def test_share_issues_token_link_and_keyboard(db):
    await seed(1, 2)
    with capture_bot() as sent:
        await wl.cmd_share(make_message(1, "/share", bot=bot_with_name()))
    token = (await load_user(1)).share_token
    assert token and len(token) == 12
    msg = sent[-1]
    assert f"https://t.me/cina_mon_bot?start=wl_{token}" in msg.text and "2 товар" in msg.text
    kb = msg.reply_markup.inline_keyboard
    assert kb[0][0].url.startswith("https://t.me/share/url?url=") and msg.callbacks[1:] == ["share:new", "share:off"]
    # Повторний /share — те саме посилання
    with capture_bot():
        await wl.cmd_share(make_message(1, "/share", bot=bot_with_name()))
    assert (await load_user(1)).share_token == token


async def test_viewer_sees_list_without_private_data(db):
    await seed(1, 3, first_name="Оля", username="olia_secret", token="TOKEN123abcd")
    with capture_bot() as sent:
        await h.cmd_start(make_message(2, "/start wl_TOKEN123abcd", lang="uk"))
    text = sent[-1].text
    assert "Список бажань: Оля" in text and "Товар 0" in text and "100 грн" in text and "shop.example" in text
    assert "olia_secret" not in text and "50" not in text.split("Список бажань")[1].split("Товар 0")[0]
    assert "Ціль" not in text and str(1) not in sent[-1].callbacks[-1].split(":", 1)[0]
    assert sent[-1].callbacks[:3] == [f"wla:{tr.id}" for tr in await trackers_of(1)]
    assert sent[-1].callbacks[-1] == "wlall:TOKEN123abcd"
    viewer = await load_user(2)
    assert viewer.source == "wishlist"


async def test_owner_opening_own_link_and_invalid_token(db):
    await seed(1, 1, token="TOKEN123abcd")
    with capture_bot() as sent:
        await h.cmd_start(make_message(1, "/start wl_TOKEN123abcd"))
    assert "твій власний список" in sent[-1].text
    with capture_bot() as sent:
        await h.cmd_start(make_message(2, "/start wl_nope_nope_no"))
    assert "більше недоступний" in sent[-1].text


async def test_rotate_and_turn_off(db):
    await seed(1, 1, token="OLDtoken1234")
    with capture_bot() as sent:
        await wl.share_action_cb(make_callback(1, "share:new", bot=bot_with_name()))
    new = (await load_user(1)).share_token
    assert new != "OLDtoken1234" and f"wl_{new}" in [x for x in sent if x.kind == "edit"][-1].text
    with capture_bot() as sent:
        await h.cmd_start(make_message(2, "/start wl_OLDtoken1234"))
    assert "більше недоступний" in sent[-1].text
    with capture_bot():
        await wl.share_action_cb(make_callback(1, "share:off"))
    assert (await load_user(1)).share_token is None
    with capture_bot() as sent:
        await h.cmd_start(make_message(2, f"/start wl_{new}"))
    assert "більше недоступний" in sent[-1].text


async def test_add_one_from_shared_list_copies_without_target(db):
    owner = await seed(1, 2, token="TOKEN123abcd")
    tr = (await trackers_of(1))[0]
    await seed(2, 0, first_name="Гість", username="guest")
    bot = make_bot()
    with capture_bot() as sent:
        await wl.shared_add_one_cb(make_callback(2, f"wla:{tr.id}", bot=bot))
    (copy,) = await trackers_of(2)
    assert (copy.canonical_url, copy.title, copy.current_price, copy.currency) == (tr.canonical_url, tr.title, tr.current_price, "UAH")
    assert copy.target_price is None and "Стежу: Товар 0" in sent[-1].text
    with capture_bot() as sent:
        await wl.shared_add_one_cb(make_callback(2, f"wla:{tr.id}", bot=bot))
    assert "вже відстежується" in sent[-1].text.lower() or "already" in sent[-1].text.lower()
    bot.send_message.assert_not_awaited()                     # власнику нічого не надсилаємо


async def test_cannot_copy_when_sharing_off_or_error_tracker(db):
    await seed(1, 2, token=None)
    tr = (await trackers_of(1))[0]
    with capture_bot() as sent:
        await wl.shared_add_one_cb(make_callback(2, f"wla:{tr.id}"))
    assert "більше недоступний" in sent[-1].text and await trackers_of(2) == []
    async with get_session() as s:
        (await s.get(User, tr.user_id)).share_token = "TOKEN123abcd"
        (await s.get(PriceTracker, tr.id)).status = TrackerStatus.ERROR
    with capture_bot() as sent:
        await wl.shared_add_one_cb(make_callback(2, f"wla:{tr.id}"))
    assert await trackers_of(2) == []


async def test_add_all_respects_free_limit_and_duplicates(db):
    await seed(1, 7, token="TOKEN123abcd")
    await seed(2, 0)
    with capture_bot() as sent:
        await wl.shared_add_all_cb(make_callback(2, "wlall:TOKEN123abcd"))
    assert "Додано: 7 · вже стежу: 0" in sent[-2].text or "Додано: 7 · вже стежу: 0" in sent[-1].text
    statuses = [t.status for t in await trackers_of(2)]
    assert statuses.count(TrackerStatus.ACTIVE) == LIMIT and statuses.count(TrackerStatus.PAUSED_LIMIT) == 2
    with capture_bot() as sent:
        await wl.shared_add_all_cb(make_callback(2, "wlall:TOKEN123abcd"))
    assert any("Додано: 0 · вже стежу: 7" in x.text for x in sent)


# ─── Джерела ─────────────────────────────────────────────────────

async def test_start_source_saved_only_for_new_users(db):
    with capture_bot():
        await h.cmd_start(make_message(5, "/start fb"))
    assert (await load_user(5)).source == "fb"
    with capture_bot():
        await h.cmd_start(make_message(5, "/start linkedin"))
    assert (await load_user(5)).source == "fb"            # перше джерело не переписується
    with capture_bot():
        await h.cmd_start(make_message(6, "/start <script>"))
    assert (await load_user(6)).source is None


# ─── Файли: гілки помилок ────────────────────────────────────────

def _doc_update(name: str, mime: str, size: int, caption: str | None = None, uid: int = 1) -> dict:
    msg = {"message_id": 77, "date": int(datetime.now(timezone.utc).timestamp()), "chat": {"id": uid, "type": "private"},
           "from": {"id": uid, "is_bot": False, "first_name": "T", "language_code": "uk"},
           "document": {"file_id": "d", "file_unique_id": "du", "file_name": name, "mime_type": mime, "file_size": size}}
    if caption:
        msg["caption"] = caption
    return {"update_id": 77, "message": msg}


async def _feed(dispatch, upd: dict) -> None:
    await dispatch.dp.feed_update(dispatch.bot, Update.model_validate(upd, context={"bot": dispatch.bot}))


async def test_file_download_failure_and_empty_file(db, dispatch, monkeypatch):
    async def broken(self, file, destination=None, **kw):
        raise RuntimeError("network")
    monkeypatch.setattr(Bot, "download", broken)
    with capture_bot() as sent:
        await _feed(dispatch, _doc_update("list.txt", "text/plain", 50))
    assert "Не вдалося прочитати файл" in sent[-1].text

    async def empty(self, file, destination=None, **kw):
        destination.write("тут немає посилань".encode()); return destination
    monkeypatch.setattr(Bot, "download", empty)
    with capture_bot() as sent:
        await _feed(dispatch, _doc_update("list.txt", "text/plain", 50))
    assert "не знайшов жодного посилання" in sent[-1].text


async def test_non_list_document_uses_caption_link_or_stays_silent(db, dispatch, monkeypatch):
    url = "https://shop.example/pdf-item"
    monkeypatch.setattr(GenericScraper, "scrape", fake_scrape({url: product(url, "Каталог")}))
    download = AsyncMock()
    monkeypatch.setattr(Bot, "download", download)
    with capture_bot() as sent:
        await _feed(dispatch, _doc_update("specs.pdf", "application/pdf", 5000, caption=f"ось {url}"))
    assert any("Каталог" in x.text for x in sent) and download.await_count == 0
    with capture_bot() as sent:
        await _feed(dispatch, _doc_update("specs.pdf", "application/pdf", 5000))
    assert sent == []


async def test_admin_blocked_user_is_ignored_by_inbox(db, dispatch, monkeypatch):
    async with get_session() as s:
        s.add(User(telegram_id=9, language_code="uk", is_blocked=True))
    scrape = AsyncMock()
    monkeypatch.setattr(GenericScraper, "scrape", scrape)
    with capture_bot() as sent:
        await dispatch(9, "https://shop.example/p/9")
    assert sent == [] and scrape.await_count == 0
