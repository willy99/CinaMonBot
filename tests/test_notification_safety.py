"""
Безпека сповіщень: юзер ніколи не отримує повідомлення, якого не просив.

Що тут доводиться:
 (a) відповідь на фідбек із дашборду йде рівно одній людині — автору фідбеку;
 (b) /reply <id> текст шле рівно одне повідомлення саме цьому id;
 (c) статично: масової розсилки (/broadcast) немає — у циклі по юзерах шле лише дайджест (opt-in);
     усі інші call-sites send_message/copy_message/forward_message/send_telegram_message
     перелічені явно, і новий масовий розсил не пройде непомітно;
 (d) _send_notification шле лише власнику трекера (tracker.user_id → telegram_id);
 (e) notify_drop / notify_restock і кулдаун RESTOCK придушують відправку і не створюють рядків Notification;
 (f) callback'и mute/unmute/ttoggle/tset/tsettings відкидають чужі трекери;
 (g) CLI purge-bogus-failed-notifications: dry-run нічого не видаляє, --apply видаляє лише двійники.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.admin import cli
from botkit.admin import auth
from app.admin import queries as q
from botkit.admin import queries as aq
from app.bot.handlers import main as h
from app.config import settings
from app.database.models import (
    Feedback, Notification, NotificationStatus, PriceEventType, PriceTracker, TrackerStatus, User,
)
from app.database.session import get_session
from app.i18n import t
from app.scheduler import price_checker as pc
from app.services.scrapers import scraper as scraper_mod
from app.services.scrapers.scraper import ProductData
from tests.helpers import ADMIN_ID, capture_bot, fsm_for, fsm_storage, make_bot, make_callback, make_message

D = Decimal
ROOT = Path(__file__).resolve().parents[1]


# ─── Сіди ────────────────────────────────────────────────────────

async def seed_user(telegram_id: int, lang: str = "uk") -> User:
    async with get_session() as s:
        user = User(telegram_id=telegram_id, username=f"u{telegram_id}", first_name=f"User{telegram_id}",
                    language_code=lang)
        s.add(user)
    return user


async def seed_tracker(user: User, url: str = "https://shop.example/p/1", *, title: str = "Товар",
                       price: str | None = "1000", target: str | None = None, available: bool = True,
                       status: str = TrackerStatus.ACTIVE, notify_drop: bool = True, notify_restock: bool = True,
                       last_restock: datetime | None = None) -> PriceTracker:
    async with get_session() as s:
        tr = PriceTracker(
            user_id=user.id, url=url, canonical_url=url, source="shop.example", title=title,
            current_price=D(price) if price else None, currency="UAH",
            target_price=D(target) if target else None, is_available=available, status=status,
            notify_drop=notify_drop, notify_restock=notify_restock, last_restock_notified_at=last_restock,
        )
        s.add(tr)
    return tr


def product(price: str | None = "900", available: bool = True, url: str = "https://shop.example/p/1") -> ProductData:
    return ProductData(title="Товар", price=D(price) if price else None, old_price=None, currency="UAH",
                       is_available=available, image_url=None, canonical_url=url, strategy="json-ld")


def patch_scrape(monkeypatch, result) -> AsyncMock:
    mock = AsyncMock(return_value=result)
    monkeypatch.setattr(scraper_mod.AbstractScraper, "scrape", mock)
    return mock


async def load_tracker(tracker_id: str) -> PriceTracker | None:
    async with get_session() as s:
        return await s.get(PriceTracker, tracker_id)


async def notifications() -> list[Notification]:
    async with get_session() as s:
        return list((await s.execute(select(Notification))).scalars().all())


async def set_flags(tracker_id: str, **values) -> None:
    async with get_session() as s:
        tr = await s.get(PriceTracker, tracker_id)
        for k, v in values.items():
            setattr(tr, k, v)


def now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _send_feedback(uid: int, text: str, *, lang: str = "uk") -> None:
    msg = make_message(uid, text, lang=lang)
    state = fsm_for(uid)
    await state.set_state(h.FeedbackState.waiting)
    with capture_bot():
        await h.process_feedback(msg, state)


async def _feedback_rows() -> list[Feedback]:
    async with get_session() as s:
        return list((await s.execute(select(Feedback).order_by(Feedback.created_at))).scalars().all())


async def _login(user) -> None:
    await aq.create_admin("tester", auth.hash_password("correct-horse-9"), telegram_id=None)
    await user.open("/login")
    user.find("Логін").type("tester")
    user.find("Пароль").type("correct-horse-9")
    user.find("Увійти").click()
    await user.should_see("Дашборд")


# ─── (a) Дашборд: відповідь на фідбек — рівно одному автору ──────

async def test_dashboard_reply_targets_only_the_feedback_author(db, user):
    """Кілька юзерів і фідбеків: відповідь на фідбек 102 → один виклик send_telegram_message з chat_id=102."""
    fsm_storage()
    for uid, text in [(101, "перший"), (102, "другий"), (103, "третій"), (102, "ще від другого"), (101, "ще від першого")]:
        await _send_feedback(uid, text)
    rows = await _feedback_rows()
    target = next(r for r in rows if r.telegram_id == 102 and r.text == "другий")
    assert len({r.telegram_id for r in rows}) == 3
    await _login(user)

    from app.admin.pages import feedback as page

    async def refresh():
        pass

    with patch.object(auth, "send_telegram_message", AsyncMock(return_value=True)) as send:
        await user.open("/feedback")
        with user.client:
            await page.open_feedback_dialog(await q.feedback_get(target.id), refresh)
        user.find("Текст відповіді").type("Тільки тобі")
        user.find("Відповісти").click()
        await user.should_see("Відповідь надіслано")

    send.assert_awaited_once()
    chat_ids = [c.args[0] for c in send.await_args_list]
    assert chat_ids == [102]
    assert 101 not in chat_ids and 103 not in chat_ids and ADMIN_ID not in chat_ids
    assert send.await_args.args[1] == t("uk", "feedback_reply_header", text="Тільки тобі")

    # У базі "replied" лише цей один фідбек; решта — як були
    fresh = await _feedback_rows()
    assert [r.status for r in fresh if r.id == target.id] == ["replied"]
    assert all(r.status in ("new",) for r in fresh if r.id != target.id)


async def test_dashboard_reply_not_sent_when_text_empty(db, user):
    fsm_storage()
    await _send_feedback(104, "привіт")
    fb = (await _feedback_rows())[0]
    await _login(user)
    from app.admin.pages import feedback as page

    async def refresh():
        pass

    with patch.object(auth, "send_telegram_message", AsyncMock(return_value=True)) as send:
        await user.open("/feedback")
        with user.client:
            await page.open_feedback_dialog(await q.feedback_get(fb.id), refresh)
        user.find("Відповісти").click()
        await user.should_see("Введіть текст відповіді")
    send.assert_not_awaited()


# ─── (b) /reply — рівно одне повідомлення саме цьому id ──────────

async def test_cmd_reply_sends_exactly_one_message_to_target_id(db):
    fsm_storage()
    await _send_feedback(201, "a")
    await _send_feedback(202, "b", lang="en")
    await _send_feedback(203, "c")

    admin_msg = make_message(ADMIN_ID, "/reply 202 Hello there", username="admin")
    with capture_bot() as sent:
        await h.cmd_reply(admin_msg)

    admin_msg.bot.send_message.assert_awaited_once()
    kw = admin_msg.bot.send_message.await_args.kwargs
    assert kw["chat_id"] == 202
    assert kw["text"] == t("en", "feedback_reply_header", text="Hello there")
    admin_msg.bot.copy_message.assert_not_awaited()
    admin_msg.bot.forward_message.assert_not_awaited()
    assert sent[-1].text.startswith("✅ Відповідь надіслано")
    statuses = {r.telegram_id: r.status for r in await _feedback_rows()}
    assert statuses == {201: "new", 202: "replied", 203: "new"}


async def test_cmd_reply_from_non_admin_sends_nothing(db):
    fsm_storage()
    await _send_feedback(204, "x")
    stranger = make_message(205, "/reply 204 Пиши мені")
    with capture_bot() as sent:
        await h.cmd_reply(stranger)
    stranger.bot.send_message.assert_not_awaited()
    assert sent == []


# ─── (c) Статично: усі місця відправки перелічені, у циклі по юзерах — лише дайджест ──

SEND_NAMES = {"send_message", "copy_message", "forward_message", "send_telegram_message"}
LOOP_NODES = (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

# (файл, функція) → кому йде повідомлення. Новий call-site = свідоме рішення + рядок тут.
ALLOWED_SEND_SITES: dict[tuple[str, str], str] = {
    ("app/bot/handlers/main.py", "_notify_admin_new_site"): "адмін (ADMIN_TELEGRAM_ID)",
    ("app/bot/handlers/main.py", "process_feedback"): "адмін (ADMIN_TELEGRAM_ID): шапка + copy_message медіа",
    ("app/bot/handlers/main.py", "cmd_reply"): "один юзер — target_id з команди адміна",
    # cmd_broadcast вимкнено (аудит 2026-10-13, N3): масової розсилки всім більше немає
    ("app/bot/handlers/main.py", "cmd_pay_premium"): "адмін (ADMIN_TELEGRAM_ID)",
    ("app/bot/handlers/main.py", "cmd_activate"): "один юзер — target_telegram_id з команди адміна",
    ("app/scheduler/price_checker.py", "_pause_excess_trackers"): "один юзер — власник трекерів (user.telegram_id)",
    ("app/scheduler/price_checker.py", "_check_domain_health"): "адмін (admin_id) — цикл по доменах, не по юзерах",
    ("app/scheduler/price_checker.py", "_send_notification"): "один юзер — власник трекера (tracker.user_id)",
    # Щотижневий дайджест: цикл по юзерах, але КОЖЕН із них явно підписаний (User.digest_enabled=True,
    # вимикається кнопкою під дайджестом або в /settings), не заблокований, і отримує рівно одне
    # повідомлення на свій telegram_id лише зі своїми трекерами. Тихий тиждень → нічого не шлеться.
    ("app/scheduler/digest.py", "send_weekly_digests"): "opt-in юзери (digest_enabled) — кожен лише свої дані на свій telegram_id",
    ("botkit/admin/auth.py", "start_2fa"): "адмін дашборду (admin.telegram_id)",
    ("app/admin/pages/feedback.py", "send_reply"): "один юзер — автор фідбеку (d['telegram_id'])",
}
# Відправка всередині циклу/comprehension дозволена лише тут
ALLOWED_LOOP_SEND_SITES = {
    ("app/scheduler/price_checker.py", "_check_domain_health"),  # цикл по доменах, адресат завжди admin_id
    # цикл по юзерах з digest_enabled=True: масовий шлях лише за згодою, кожному — його власні дані
    # (гарантії перевіряє tests/test_digest.py: тільки opt-in і незаблоковані, один send на юзера, свій telegram_id)
    ("app/scheduler/digest.py", "send_weekly_digests"),
}
# Цикли по юзерах (не по доменах) — лише дайджест (opt-in); адресат = user.telegram_id з циклу
USER_LOOP_SEND_SITES = [
    ("app/scheduler/digest.py", "send_weekly_digests"),
]


def _send_call_sites(path: Path) -> list[tuple[str, bool, ast.Call]]:
    """[(функція, у_циклі, вузол Call)] для кожного виклику send_*/copy_*/forward_* у файлі."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, bool, ast.Call]] = []

    def visit(node: ast.AST, func: str, loop_depth: int) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func, loop_depth = node.name, 0   # вкладена функція — свій контекст
        elif isinstance(node, LOOP_NODES):
            loop_depth += 1
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in SEND_NAMES:
                found.append((func, loop_depth > 0, node))
        for child in ast.iter_child_nodes(node):
            visit(child, func, loop_depth)

    visit(tree, "<module>", 0)
    return found


def _all_send_sites() -> dict[tuple[str, str], list[tuple[bool, ast.Call]]]:
    sites: dict[tuple[str, str], list[tuple[bool, ast.Call]]] = {}
    # app/ і спільний пакет botkit/ (2FA-код, алерти healthcheck, попередження про ліміт живуть там)
    paths = [*(ROOT / "app").rglob("*.py"), *(p for p in (ROOT / "botkit").rglob("*.py") if "tests" not in p.parts)]
    for path in sorted(paths):
        rel = path.relative_to(ROOT).as_posix()
        for func, in_loop, call in _send_call_sites(path):
            sites.setdefault((rel, func), []).append((in_loop, call))
    return sites


def _kw(call: ast.Call, name: str) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def test_every_send_call_site_is_explicitly_allowed():
    sites = _all_send_sites()
    unexpected = set(sites) - set(ALLOWED_SEND_SITES)
    assert not unexpected, f"нові місця відправки повідомлень — додай у ALLOWED_SEND_SITES свідомо: {sorted(unexpected)}"
    stale = set(ALLOWED_SEND_SITES) - set(sites)
    assert not stale, f"у списку дозволених є неіснуючі call-sites: {sorted(stale)}"


def test_only_allowed_sends_inside_loops():
    sites = _all_send_sites()
    in_loop = {key for key, calls in sites.items() if any(flag for flag, _ in calls)}
    assert in_loop == ALLOWED_LOOP_SEND_SITES, f"відправка в циклі поза дозволеними: {sorted(in_loop - ALLOWED_LOOP_SEND_SITES)}"

    # _check_domain_health: адресат — літерально admin_id, не юзер
    for _, call in sites[("app/scheduler/price_checker.py", "_check_domain_health")]:
        chat = _kw(call, "chat_id")
        assert isinstance(chat, ast.Name) and chat.id == "admin_id"

    # Цикл по юзерах — лише дайджест (opt-in). /broadcast вимкнено (N3). Інших немає.
    user_loops = sorted(key for key in in_loop if key != ("app/scheduler/price_checker.py", "_check_domain_health"))
    assert user_loops == sorted(USER_LOOP_SEND_SITES)

    # send_weekly_digests: рівно один send_message, адресат — user.telegram_id з циклу по opt-in юзерах,
    # і сам запит юзерів фільтрує digest_enabled + is_blocked (статично, щоб фільтр не зник непомітно)
    digest_calls = sites[("app/scheduler/digest.py", "send_weekly_digests")]
    assert len(digest_calls) == 1
    (_, call), = digest_calls
    chat = _kw(call, "chat_id")
    assert isinstance(chat, ast.Attribute) and isinstance(chat.value, ast.Name)
    assert (chat.value.id, chat.attr) == ("user", "telegram_id")
    src = (ROOT / "app/scheduler/digest.py").read_text(encoding="utf-8")
    fn_src = src[src.index("async def send_weekly_digests"):]
    assert "User.digest_enabled.is_(True)" in fn_src and "User.is_blocked.is_(False)" in fn_src


def test_single_recipient_sites_address_one_explicit_chat_id():
    """_send_notification, cmd_reply, cmd_activate, send_reply: chat_id — одна явна змінна власника/адресата."""
    sites = _all_send_sites()
    expected_chat = {
        ("app/scheduler/price_checker.py", "_send_notification"): ("user", "telegram_id"),
        ("app/scheduler/price_checker.py", "_pause_excess_trackers"): ("user", "telegram_id"),
        ("app/scheduler/digest.py", "send_weekly_digests"): ("user", "telegram_id"),
        ("app/bot/handlers/main.py", "cmd_reply"): "target_id",
        ("app/bot/handlers/main.py", "cmd_activate"): "target_telegram_id",
    }
    for key, want in expected_chat.items():
        for _, call in sites[key]:
            chat = _kw(call, "chat_id")
            if isinstance(want, tuple):
                assert isinstance(chat, ast.Attribute) and isinstance(chat.value, ast.Name)
                assert (chat.value.id, chat.attr) == want, key
            else:
                assert isinstance(chat, ast.Name) and chat.id == want, key
    # Дашборд: send_telegram_message(d["telegram_id"], ...) — перший позиційний аргумент
    (_, call), = sites[("app/admin/pages/feedback.py", "send_reply")]
    first = call.args[0]
    assert isinstance(first, ast.Subscript) and isinstance(first.value, ast.Name) and first.value.id == "d"
    assert isinstance(first.slice, ast.Constant) and first.slice.value == "telegram_id"


# ─── (d) _send_notification — лише власнику трекера ──────────────

async def test_send_notification_goes_to_tracker_owner_only(db):
    alice = await seed_user(301)
    bob = await seed_user(302, lang="en")
    await seed_tracker(alice, "https://shop.example/p/a")
    tr_bob = await seed_tracker(bob, "https://shop.example/p/b", title="Bob's item")
    bot = make_bot()

    async with get_session() as s:
        tr = await s.get(PriceTracker, tr_bob.id)
        await pc._send_notification(bot=bot, session=s, tracker=tr, event=PriceEventType.DECREASE,
                                    old_price=D("1000"), new_price=D("900"))

    bot.send_message.assert_awaited_once()
    kw = bot.send_message.await_args.kwargs
    assert kw["chat_id"] == 302 and "Bob's item" in kw["text"]
    notes = await notifications()
    assert len(notes) == 1 and notes[0].user_id == bob.id and notes[0].tracker_id == tr_bob.id


async def test_check_single_price_notifies_only_the_owner_of_the_changed_tracker(db, monkeypatch):
    alice = await seed_user(303)
    bob = await seed_user(304)
    tr_a = await seed_tracker(alice, "https://shop.example/p/a", price="1000")
    tr_b = await seed_tracker(bob, "https://shop.example/p/b", price="1000")
    patch_scrape(monkeypatch, product(price="800", url="https://shop.example/p/b"))
    bot = make_bot()

    await pc.check_single_price(tr_b, bot)

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 304
    assert (await load_tracker(tr_a.id)).current_price == D("1000")   # чужий трекер не чіпали


@pytest.mark.parametrize("event, kind", [
    (PriceEventType.RESTOCK, "restock"),
    (PriceEventType.DECREASE, "drop"),
    (PriceEventType.TARGET_REACHED, "drop"),
])
async def test_notification_has_contextual_mute_button(db, event, kind):
    user = await seed_user(305)
    tr = await seed_tracker(user, target="950")
    bot = make_bot()
    async with get_session() as s:
        t_ = await s.get(PriceTracker, tr.id)
        await pc._send_notification(bot=bot, session=s, tracker=t_, event=event, old_price=D("1000"), new_price=D("900"))
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    texts = [b.text for row in markup.inline_keyboard for b in row]
    assert callbacks == [f"mute:{kind}:{tr.id}", f"tsettings:{tr.id}"]
    assert texts == [t("uk", "btn_mute_restock" if kind == "restock" else "btn_mute_drop"), t("uk", "btn_tracker_settings")]


# ─── (e) Прапорці і кулдаун придушують відправку, рядків Notification немає ──

async def test_restock_muted_suppresses_send_and_creates_no_row(db, monkeypatch):
    user = await seed_user(401)
    tr = await seed_tracker(user, available=False, notify_restock=False)
    patch_scrape(monkeypatch, product(price="1000", available=True))
    bot = make_bot()
    await pc.check_single_price(tr, bot)
    bot.send_message.assert_not_awaited()
    assert await notifications() == []
    fresh = await load_tracker(tr.id)
    assert fresh.is_available is True and fresh.last_restock_notified_at is None   # стан оновлено, але тихо


@pytest.mark.parametrize("new_price, target", [("900", None), ("900", "950")])
async def test_drop_muted_suppresses_decrease_and_target(db, monkeypatch, new_price, target):
    user = await seed_user(402)
    tr = await seed_tracker(user, price="1000", target=target, notify_drop=False)
    patch_scrape(monkeypatch, product(price=new_price))
    bot = make_bot()
    await pc.check_single_price(tr, bot)
    bot.send_message.assert_not_awaited()
    assert await notifications() == []
    assert (await load_tracker(tr.id)).current_price == D(new_price)


async def test_flags_are_independent(db, monkeypatch):
    """notify_drop=False не блокує RESTOCK; notify_restock=False не блокує DECREASE."""
    user = await seed_user(403)
    tr1 = await seed_tracker(user, "https://shop.example/p/1", available=False, notify_drop=False)
    tr2 = await seed_tracker(user, "https://shop.example/p/2", price="1000", notify_restock=False)
    bot = make_bot()
    patch_scrape(monkeypatch, product(price="1000", available=True, url="https://shop.example/p/1"))
    await pc.check_single_price(tr1, bot)
    patch_scrape(monkeypatch, product(price="800", url="https://shop.example/p/2"))
    await pc.check_single_price(tr2, bot)
    assert bot.send_message.await_count == 2
    assert [n.event_type for n in await notifications()] == [PriceEventType.RESTOCK, PriceEventType.DECREASE]


async def test_restock_sets_cooldown_stamp_and_flicker_sends_once(db, monkeypatch):
    """Товар мигає в наявності тричі → одне сповіщення (кейс 17.06 / 19.06 / 22.06)."""
    user = await seed_user(404)
    tr = await seed_tracker(user, available=False, price="1000")
    bot = make_bot()
    patch_scrape(monkeypatch, product(price="1000", available=True))

    await pc.check_single_price(tr, bot)                      # 1-й restock → шлемо
    stamp = (await load_tracker(tr.id)).last_restock_notified_at
    assert stamp is not None and abs(now_naive() - stamp) < timedelta(minutes=1)

    for _ in range(2):                                        # зник → знову з'явився, двічі
        await set_flags(tr.id, is_available=False)
        await pc.check_single_price(tr, bot)

    assert bot.send_message.await_count == 1
    notes = await notifications()
    assert len(notes) == 1 and notes[0].event_type == PriceEventType.RESTOCK
    assert (await load_tracker(tr.id)).last_restock_notified_at == stamp   # штамп не оновлюється мовчки


async def test_restock_after_cooldown_sends_again(db, monkeypatch):
    user = await seed_user(405)
    old = now_naive() - timedelta(days=settings.RESTOCK_COOLDOWN_DAYS, hours=1)
    tr = await seed_tracker(user, available=False, price="1000", last_restock=old)
    patch_scrape(monkeypatch, product(price="1000", available=True))
    bot = make_bot()
    await pc.check_single_price(tr, bot)
    bot.send_message.assert_awaited_once()
    assert (await load_tracker(tr.id)).last_restock_notified_at > old


async def test_restock_within_cooldown_but_cheaper_sends(db, monkeypatch):
    user = await seed_user(406)
    tr = await seed_tracker(user, available=False, price="1000", last_restock=now_naive() - timedelta(days=1))
    patch_scrape(monkeypatch, product(price="950", available=True))
    bot = make_bot()
    await pc.check_single_price(tr, bot)
    bot.send_message.assert_awaited_once()
    assert "наявності" in bot.send_message.await_args.kwargs["text"]


async def test_restock_within_cooldown_same_or_higher_price_is_suppressed(db, monkeypatch):
    user = await seed_user(407)
    tr = await seed_tracker(user, available=False, price="1000", last_restock=now_naive() - timedelta(days=1))
    bot = make_bot()
    patch_scrape(monkeypatch, product(price="1000", available=True))
    await pc.check_single_price(tr, bot)
    await set_flags(tr.id, is_available=False)
    patch_scrape(monkeypatch, product(price="1200", available=True))
    await pc.check_single_price(tr, bot)
    bot.send_message.assert_not_awaited()
    assert await notifications() == []


async def test_suppressed_event_is_logged_with_reason(db, monkeypatch):
    user = await seed_user(408)
    tr = await seed_tracker(user, price="1000", notify_drop=False)
    patch_scrape(monkeypatch, product(price="500"))
    events = []
    with patch.object(pc.logger, "info", side_effect=lambda ev, **kw: events.append((ev, kw))):
        await pc.check_single_price(tr, make_bot())
    suppressed = [kw for ev, kw in events if ev == "notification_suppressed"]
    assert suppressed and suppressed[0]["reason"] == "muted_drop" and suppressed[0]["tracker_id"] == tr.id


def _tr(**kw) -> PriceTracker:
    base = dict(notify_drop=True, notify_restock=True, last_restock_notified_at=None)
    base.update(kw)
    return PriceTracker(**base)


NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("tracker, event, old, new, expected", [
    (_tr(), PriceEventType.RESTOCK, D("100"), D("100"), None),
    (_tr(notify_restock=False), PriceEventType.RESTOCK, D("100"), D("100"), "muted_restock"),
    (_tr(notify_drop=False), PriceEventType.RESTOCK, D("100"), D("100"), None),
    (_tr(last_restock_notified_at=NOW.replace(tzinfo=None) - timedelta(days=3)), PriceEventType.RESTOCK, D("100"), D("100"), "restock_cooldown"),
    (_tr(last_restock_notified_at=NOW.replace(tzinfo=None) - timedelta(days=3)), PriceEventType.RESTOCK, D("100"), D("99"), None),
    (_tr(last_restock_notified_at=NOW.replace(tzinfo=None) - timedelta(days=3)), PriceEventType.RESTOCK, None, D("99"), "restock_cooldown"),
    (_tr(last_restock_notified_at=NOW.replace(tzinfo=None) - timedelta(days=8)), PriceEventType.RESTOCK, D("100"), D("100"), None),
    (_tr(last_restock_notified_at=NOW - timedelta(days=3)), PriceEventType.RESTOCK, D("100"), D("100"), "restock_cooldown"),  # tz-aware теж
    (_tr(), PriceEventType.DECREASE, D("100"), D("90"), None),
    (_tr(notify_drop=False), PriceEventType.DECREASE, D("100"), D("90"), "muted_drop"),
    (_tr(notify_drop=False), PriceEventType.TARGET_REACHED, D("100"), D("90"), "muted_drop"),
    (_tr(notify_restock=False), PriceEventType.DECREASE, D("100"), D("90"), None),
])
def test_suppression_reason(tracker, event, old, new, expected):
    assert pc._suppression_reason(tracker=tracker, event=event, old_price=old, new_price=new, now=NOW) == expected


def test_restock_cooldown_default_is_seven_days():
    assert settings.RESTOCK_COOLDOWN_DAYS == 7


# ─── (f) Callback'и: свої — працюють, чужі — відкидаються ────────

async def _flags(tracker_id: str) -> tuple[bool, bool]:
    tr = await load_tracker(tracker_id)
    return tr.notify_drop, tr.notify_restock


@pytest.mark.parametrize("data_tpl, handler", [
    ("mute:restock:{id}", h.toggle_mute_from_notification),
    ("mute:drop:{id}", h.toggle_mute_from_notification),
    ("unmute:restock:{id}", h.toggle_mute_from_notification),
    ("ttoggle:drop:{id}", h.toggle_tracker_setting),
    ("ttoggle:restock:{id}", h.toggle_tracker_setting),
    ("tset:{id}", h.open_tracker_settings_inplace),
    ("tsettings:{id}", h.open_tracker_settings_new),
])
async def test_callbacks_reject_foreign_tracker(db, data_tpl, handler):
    owner = await seed_user(501)
    intruder = await seed_user(502, lang="en")
    tr = await seed_tracker(owner, notify_drop=True, notify_restock=True)

    with capture_bot() as sent:
        await handler(make_callback(502, data_tpl.format(id=tr.id), lang="en"))

    assert [s.kind for s in sent] == ["callback"]
    assert sent[0].text == t("en", "no_access")
    assert await _flags(tr.id) == (True, True)


@pytest.mark.parametrize("data, handler", [
    ("mute:restock:missing-id", h.toggle_mute_from_notification),
    ("ttoggle:drop:missing-id", h.toggle_tracker_setting),
    ("tset:missing-id", h.open_tracker_settings_inplace),
    ("tsettings:missing-id", h.open_tracker_settings_new),
])
async def test_callbacks_unknown_tracker_toast_not_found(db, data, handler):
    await seed_user(503)
    with capture_bot() as sent:
        await handler(make_callback(503, data))
    assert [s.kind for s in sent] == ["callback"] and sent[0].text == t("uk", "not_found")


@pytest.mark.parametrize("data, handler", [
    ("mute:everything:x", h.toggle_mute_from_notification),
    ("mute:restock", h.toggle_mute_from_notification),
    ("ttoggle:all:x", h.toggle_tracker_setting),
])
async def test_callbacks_malformed_kind_are_ignored(db, data, handler):
    user = await seed_user(504)
    tr = await seed_tracker(user)
    with capture_bot() as sent:
        await handler(make_callback(504, data))
    assert [s.kind for s in sent] == ["callback"] and sent[0].text == ""
    assert await _flags(tr.id) == (True, True)


async def test_mute_then_unmute_from_notification(db):
    user = await seed_user(505)
    tr = await seed_tracker(user)

    with capture_bot() as sent:
        await h.toggle_mute_from_notification(make_callback(505, f"mute:restock:{tr.id}"))
    assert await _flags(tr.id) == (True, False)
    markup, toast = sent
    assert markup.kind == "markup"
    assert markup.callbacks == [f"unmute:restock:{tr.id}", f"tsettings:{tr.id}"]
    assert markup.buttons[0] == t("uk", "btn_unmute")
    assert toast.text == t("uk", "mute_toast_restock")

    with capture_bot() as sent:
        await h.toggle_mute_from_notification(make_callback(505, f"unmute:restock:{tr.id}"))
    assert await _flags(tr.id) == (True, True)
    assert sent[0].callbacks == [f"mute:restock:{tr.id}", f"tsettings:{tr.id}"]
    assert sent[1].text == t("uk", "unmute_toast_restock")

    with capture_bot() as sent:
        await h.toggle_mute_from_notification(make_callback(505, f"mute:drop:{tr.id}"))
    assert await _flags(tr.id) == (False, True)
    assert sent[0].callbacks == [f"unmute:drop:{tr.id}", f"tsettings:{tr.id}"]
    assert sent[1].text == t("uk", "mute_toast_drop")


async def test_mute_survives_keyboard_edit_failure(db):
    """Старе повідомлення не редагується — прапорець усе одно збережено, toast показано."""
    user = await seed_user(506)
    tr = await seed_tracker(user)
    cb = make_callback(506, f"mute:drop:{tr.id}")
    from aiogram.types import Message
    with patch.object(Message, "edit_reply_markup", AsyncMock(side_effect=RuntimeError("message can't be edited"))), \
         capture_bot() as sent:
        await h.toggle_mute_from_notification(cb)
    assert await _flags(tr.id) == (False, True)
    assert sent[-1].kind == "callback" and sent[-1].text == t("uk", "mute_toast_drop")


async def test_settings_command_lists_active_and_paused_trackers(db):
    user = await seed_user(507, lang="en")
    a = await seed_tracker(user, "https://shop.example/p/1", title="Alpha")
    b = await seed_tracker(user, "https://shop.example/p/2", title="Paused", status=TrackerStatus.PAUSED, notify_drop=False)
    await seed_tracker(user, "https://shop.example/p/3", title="Broken", status=TrackerStatus.ERROR)
    other = await seed_user(508)
    await seed_tracker(other, "https://shop.example/p/9", title="Foreign")

    with capture_bot() as sent:
        await h.cmd_settings_msg(make_message(507, "/settings", "en"))
    assert sent[0].kind == "answer" and sent[0].text == t("en", "settings_prompt")
    # Перший рядок — глобальний перемикач щотижневого дайджесту (увімкнений за замовчуванням)
    assert sent[0].buttons == [f"{t('en', 'settings_digest')} ✅", "🔔 Alpha", "🔕 Paused", t("en", "btn_back")]
    assert sent[0].callbacks == ["digest:toggle", f"tset:{a.id}", f"tset:{b.id}", "my_list"]


async def test_settings_menu_empty_and_callback_edits_in_place(db):
    await seed_user(509)
    with capture_bot() as sent:
        await h.cmd_settings_msg(make_message(509, "/settings"))
    assert sent[0].text == t("uk", "settings_empty") and sent[0].callbacks == ["add_tracker"]

    with capture_bot() as sent:
        await h.cmd_settings_cb(make_callback(509, "settings_menu"))
    assert [s.kind for s in sent] == ["edit", "callback"]


async def test_tracker_settings_view_and_toggles(db):
    user = await seed_user(510)
    tr = await seed_tracker(user, title="Навушники <X>", price="1500")

    with capture_bot() as sent:
        await h.open_tracker_settings_inplace(make_callback(510, f"tset:{tr.id}"))
    view = sent[0]
    assert view.kind == "edit"
    assert "Навушники &lt;X&gt;" in view.text and "1 500 грн" in view.text
    assert view.callbacks == [f"ttoggle:drop:{tr.id}", f"ttoggle:restock:{tr.id}", "settings_menu", f"del:{tr.id}"]
    assert view.buttons[:2] == [f"✅ {t('uk', 'toggle_drop')}", f"✅ {t('uk', 'toggle_restock')}"]
    assert view.buttons[2:] == [t("uk", "btn_back"), t("uk", "btn_delete")]

    with capture_bot() as sent:
        await h.toggle_tracker_setting(make_callback(510, f"ttoggle:drop:{tr.id}"))
    assert await _flags(tr.id) == (False, True)
    assert sent[0].kind == "edit" and sent[0].buttons[0] == f"☐ {t('uk', 'toggle_drop')}"
    assert sent[1].text == t("uk", "settings_toast_off")

    with capture_bot() as sent:
        await h.toggle_tracker_setting(make_callback(510, f"ttoggle:restock:{tr.id}"))
    assert await _flags(tr.id) == (False, False)
    assert sent[0].buttons[:2] == [f"☐ {t('uk', 'toggle_drop')}", f"☐ {t('uk', 'toggle_restock')}"]

    with capture_bot() as sent:
        await h.toggle_tracker_setting(make_callback(510, f"ttoggle:drop:{tr.id}"))
    assert await _flags(tr.id) == (True, False)
    assert sent[1].text == t("uk", "settings_toast_on")


async def test_settings_button_from_notification_opens_new_message(db):
    user = await seed_user(511)
    tr = await seed_tracker(user)
    with capture_bot() as sent:
        await h.open_tracker_settings_new(make_callback(511, f"tsettings:{tr.id}"))
    assert [s.kind for s in sent] == ["answer", "callback"]     # сповіщення не перезаписуємо
    assert sent[0].callbacks[-1] == f"del:{tr.id}"


async def test_delete_from_settings_reuses_confirm_flow(db):
    user = await seed_user(512)
    tr = await seed_tracker(user, title="Видалити мене")
    with capture_bot() as sent:
        await h.confirm_delete(make_callback(512, f"del:{tr.id}"))
    assert sent[0].callbacks == [f"delok:{tr.id}", "delete_menu"]


async def test_help_and_list_mention_settings(db):
    await seed_user(513)
    with capture_bot() as sent:
        await h.cmd_help(make_message(513, "/help"))
    assert "/settings" in sent[0].text
    from app.i18n import BOT_COMMANDS
    assert all("settings" in [c for c, _ in cmds] for cmds in BOT_COMMANDS.values())


async def test_admin_user_detail_exposes_notify_flags(db):
    user = await seed_user(514)
    tr = await seed_tracker(user, notify_restock=False)
    d = await q.user_detail(user.id)
    assert [(x["id"], x["notify_drop"], x["notify_restock"]) for x in d["trackers"]] == [(tr.id, True, False)]


# ─── (g) CLI: purge-bogus-failed-notifications ────────────────────

async def _seed_notification(user: User, tracker: PriceTracker, status: str, created_at: datetime, *,
                             event: str = PriceEventType.RESTOCK, error: str | None = None) -> Notification:
    async with get_session() as s:
        n = Notification(user_id=user.id, tracker_id=tracker.id, event_type=event, old_price=None,
                         new_price=D("100"), status=status, error_message=error, created_at=created_at,
                         telegram_message_id=5 if status == NotificationStatus.SENT else None)
        s.add(n)
        await s.flush()
        return n


BOGUS = "'BoundLoggerLazyProxy' object has no attribute '_make_filtering_bound_logger'"


async def test_purge_bogus_failed_notifications_dry_run_and_apply(db, capsys):
    user = await seed_user(601)
    tr1 = await seed_tracker(user, "https://shop.example/p/1")
    tr2 = await seed_tracker(user, "https://shop.example/p/2")
    t0 = datetime(2026, 6, 17, 10, 0, 0)

    # Двійники (видаляються): FAILED з маркером поруч із SENT по тому ж трекеру і типу події
    await _seed_notification(user, tr1, NotificationStatus.SENT, t0)
    twin1 = await _seed_notification(user, tr1, NotificationStatus.FAILED, t0 + timedelta(seconds=2), error=BOGUS)
    await _seed_notification(user, tr2, NotificationStatus.SENT, t0 + timedelta(days=2), event=PriceEventType.DECREASE)
    twin2 = await _seed_notification(user, tr2, NotificationStatus.FAILED, t0 + timedelta(days=2, seconds=-9),
                                     event=PriceEventType.DECREASE, error=BOGUS)
    # НЕ двійники (лишаються):
    keep_far = await _seed_notification(user, tr1, NotificationStatus.FAILED, t0 + timedelta(minutes=5), error=BOGUS)       # > 10 с від SENT
    keep_other_event = await _seed_notification(user, tr1, NotificationStatus.FAILED, t0 + timedelta(seconds=1),
                                                event=PriceEventType.DECREASE, error=BOGUS)                                 # інший тип події
    keep_real_fail = await _seed_notification(user, tr1, NotificationStatus.FAILED, t0 + timedelta(seconds=3),
                                              error="Forbidden: bot was blocked by the user")                               # справжня помилка
    await _seed_notification(user, tr2, NotificationStatus.SENT, t0 + timedelta(days=5))
    keep_other_tracker = await _seed_notification(user, tr1, NotificationStatus.FAILED, t0 + timedelta(days=5, seconds=1), error=BOGUS)  # SENT по іншому трекеру
    before = {n.id for n in await notifications()}
    assert len(before) == 9

    # dry-run: рахує, нічого не видаляє
    assert await cli.purge_bogus_failed_notifications(apply=False) == 2
    out = capsys.readouterr().out
    assert "Знайдено 2" in out and "dry-run" in out
    assert {n.id for n in await notifications()} == before

    # apply: видаляє рівно двійників
    assert await cli.purge_bogus_failed_notifications(apply=True) == 2
    assert "Видалено 2" in capsys.readouterr().out
    after = {n.id for n in await notifications()}
    assert before - after == {twin1.id, twin2.id}
    assert {keep_far.id, keep_other_event.id, keep_real_fail.id, keep_other_tracker.id} <= after

    # Повторний запуск — чисто
    assert await cli.purge_bogus_failed_notifications(apply=True) == 0
    assert "не знайдено" in capsys.readouterr().out


async def test_purge_ignores_like_wildcard_false_positives(db):
    """LIKE трактує '_' як будь-який символ — рядок 'Xmake_filtering…' без справжнього маркера не чіпаємо."""
    user = await seed_user(602)
    tr = await seed_tracker(user)
    t0 = datetime(2026, 6, 19, 9, 0, 0)
    await _seed_notification(user, tr, NotificationStatus.SENT, t0)
    fake = await _seed_notification(user, tr, NotificationStatus.FAILED, t0 + timedelta(seconds=1),
                                    error="Xmake_filtering_bound_logger is not the marker")
    assert await cli.find_bogus_failed_notifications() == []
    assert await cli.purge_bogus_failed_notifications(apply=True) == 0
    assert {n.id for n in await notifications()} == {fake.id} | {n.id for n in await notifications() if n.status == NotificationStatus.SENT}
