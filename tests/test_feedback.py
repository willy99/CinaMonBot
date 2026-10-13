"""
Фідбек: бот приймає будь-яке повідомлення (текст/фото/файл), зберігає в базу,
копіює адміну; /reply від адміна; запити інбоксу; сторінка /feedback і проксі /media.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from nicegui import ui
from sqlalchemy import select

from botkit.admin import auth
from app.admin import queries as q
from botkit.admin import queries as aq
from app.bot.handlers import main as h
from app.database.models import Feedback, User
from app.database.session import get_session
from app.i18n import t
from tests.helpers import ADMIN_ID, capture_bot, fsm_for, fsm_storage, make_message


async def _send_feedback(uid: int, text: str | None = None, *, lang: str = "uk", **kw):
    """Ставить юзера в стан фідбеку, викликає process_feedback, повертає (msg, state, sent)."""
    msg = make_message(uid, text, lang=lang, **kw)
    state = fsm_for(uid)
    await state.set_state(h.FeedbackState.waiting)
    with capture_bot() as sent:
        await h.process_feedback(msg, state)
    return msg, state, sent


async def _rows() -> list[Feedback]:
    async with get_session() as s:
        return list((await s.execute(select(Feedback).order_by(Feedback.created_at))).scalars().all())


# ─── Бот: process_feedback ───────────────────────────────────────

async def test_cancel_is_registered_before_feedback_state_handler():
    names = [hh.callback.__name__ for hh in h.router.message.handlers]
    assert names.index("cmd_cancel") < names.index("process_feedback")


async def test_text_feedback_saved_forwarded_and_thanked(db):
    fsm_storage()
    msg, state, sent = await _send_feedback(1, "Знайшов баг у /list", lang="en")

    assert await state.get_state() is None
    rows = await _rows()
    assert len(rows) == 1
    fb = rows[0]
    assert fb.content_type == "text"
    assert fb.text == "Знайшов баг у /list"
    assert fb.telegram_id == 1 and fb.chat_id == 1 and fb.telegram_message_id == 1
    assert fb.status == "new" and fb.file_id is None and fb.forwarded_message_id is None
    async with get_session() as s:
        user = (await s.execute(select(User).where(User.telegram_id == 1))).scalar_one()
    assert fb.user_id == user.id

    msg.bot.send_message.assert_awaited_once()
    kw = msg.bot.send_message.await_args.kwargs
    assert kw["chat_id"] == ADMIN_ID
    assert "💬 <b>Feedback</b>" in kw["text"]
    assert "Tester @tester [en]" in kw["text"]
    assert "<code>1</code>" in kw["text"]
    assert kw["text"].endswith("Знайшов баг у /list")
    msg.bot.copy_message.assert_not_awaited()
    assert sent[-1].text == t("en", "feedback_thanks")


async def test_photo_feedback_copies_media_to_admin(db):
    fsm_storage()
    msg, state, sent = await _send_feedback(2, photo=True, caption="ось скрін")

    assert await state.get_state() is None
    fb = (await _rows())[0]
    assert fb.content_type == "photo"
    assert fb.file_id == "photo-file-id"
    assert fb.text == "ось скрін"
    assert fb.forwarded_message_id == 778

    header = msg.bot.send_message.await_args.kwargs["text"]
    assert "🖼 Фото" in header and header.endswith("ось скрін")
    msg.bot.copy_message.assert_awaited_once_with(chat_id=ADMIN_ID, from_chat_id=2, message_id=1)
    assert sent[-1].text == t("uk", "feedback_thanks")


async def test_photo_without_caption_is_still_accepted(db):
    fsm_storage()
    msg, state, sent = await _send_feedback(3, photo=True)
    assert await state.get_state() is None
    fb = (await _rows())[0]
    assert fb.content_type == "photo" and fb.text is None
    assert sent[-1].text == t("uk", "feedback_thanks")


async def test_document_feedback_stores_file_meta(db):
    fsm_storage()
    msg, state, sent = await _send_feedback(4, document=True, caption="звіт")
    fb = (await _rows())[0]
    assert fb.content_type == "document"
    assert fb.file_id == "doc-file-id"
    assert fb.file_name == "report.pdf" and fb.mime_type == "application/pdf"
    assert fb.forwarded_message_id == 778
    assert "📎 Файл: report.pdf" in msg.bot.send_message.await_args.kwargs["text"]
    msg.bot.copy_message.assert_awaited_once_with(chat_id=ADMIN_ID, from_chat_id=4, message_id=1)
    assert sent[-1].text == t("uk", "feedback_thanks")


async def test_delivery_failure_keeps_row_and_still_replies(db):
    fsm_storage()
    msg = make_message(5, None, photo=True, caption="x")
    msg.bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    state = fsm_for(5)
    await state.set_state(h.FeedbackState.waiting)
    with capture_bot() as sent:
        await h.process_feedback(msg, state)

    assert await state.get_state() is None
    fb = (await _rows())[0]
    assert fb.content_type == "photo" and fb.file_id == "photo-file-id"
    assert fb.forwarded_message_id is None
    msg.bot.copy_message.assert_not_awaited()
    assert sent[-1].text == t("uk", "feedback_received")


async def test_html_in_feedback_is_escaped_for_admin(db):
    fsm_storage()
    msg, _, _ = await _send_feedback(6, "<b>bold</b> & co")
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; co" in msg.bot.send_message.await_args.kwargs["text"]


# ─── Бот: /reply ─────────────────────────────────────────────────

async def test_admin_reply_sends_to_user_and_marks_replied(db):
    fsm_storage()
    await _send_feedback(7, "перше", lang="en")
    await _send_feedback(7, "друге", lang="en")

    admin_msg = make_message(ADMIN_ID, "/reply 7 Дякую, виправив!", username="admin")
    with capture_bot() as sent:
        await h.cmd_reply(admin_msg)

    admin_msg.bot.send_message.assert_awaited_once()
    kw = admin_msg.bot.send_message.await_args.kwargs
    assert kw["chat_id"] == 7
    assert kw["text"] == t("en", "feedback_reply_header", text="Дякую, виправив!")
    assert sent[-1].text.startswith("✅ Відповідь надіслано")

    rows = await _rows()
    assert [r.status for r in rows] == ["new", "replied"]
    assert rows[1].reply_text == "Дякую, виправив!"
    assert rows[1].replied_by == f"telegram:{ADMIN_ID}"
    assert rows[1].replied_at is not None


async def test_admin_reply_usage_and_non_admin_ignored(db):
    fsm_storage()
    await _send_feedback(8, "hi")

    bad = make_message(ADMIN_ID, "/reply 8")
    with capture_bot() as sent:
        await h.cmd_reply(bad)
    assert "Використання" in sent[-1].text
    bad.bot.send_message.assert_not_awaited()

    stranger = make_message(9, "/reply 8 hack")
    with capture_bot() as sent:
        await h.cmd_reply(stranger)
    assert sent == []
    stranger.bot.send_message.assert_not_awaited()
    assert (await _rows())[0].status == "new"


# ─── Запити інбоксу ──────────────────────────────────────────────

async def test_feedback_queries(db):
    fsm_storage()
    await _send_feedback(10, "Перший текст", lang="uk")
    await _send_feedback(11, photo=True, caption="скріншот оплати", lang="en")
    await _send_feedback(10, document=True)
    rows = await _rows()
    a, b, c = rows

    assert await q.feedback_counts() == {"new": 3, "read": 0, "replied": 0, "total": 3}

    lst = await q.feedback_list()
    assert len(lst) == 3
    assert lst[0]["id"] == c.id  # найновіші згори
    assert lst[-1]["text"] == "Перший текст" and lst[-1]["content_type"] == "text"
    assert {r["content_type"] for r in lst} == {"text", "photo", "document"}

    assert [r["id"] for r in await q.feedback_list(search="оплати")] == [b.id]
    assert [r["id"] for r in await q.feedback_list(search="10")] == [c.id, a.id]
    assert [r["id"] for r in await q.feedback_list(search="tester")] and len(await q.feedback_list(search="nobody")) == 0

    assert await q.feedback_mark(a.id, "read") is True
    assert await q.feedback_mark("missing", "read") is False
    assert await q.feedback_reply_saved(b.id, "Дякую!", "pavel") is True
    counts = await q.feedback_counts()
    assert (counts["new"], counts["read"], counts["replied"], counts["total"]) == (1, 1, 1, 3)
    assert [r["id"] for r in await q.feedback_list(status="new")] == [c.id]
    assert [r["id"] for r in await q.feedback_list(status="replied")] == [b.id]
    assert {r["id"] for r in await q.feedback_list(status="unreplied")} == {a.id, c.id}

    got = await q.feedback_get(b.id)
    assert got["status"] == "replied" and got["reply_text"] == "Дякую!" and got["replied_by"] == "pavel"
    assert got["replied_at"] and got["file_id"] == "photo-file-id" and got["forwarded"] is True
    assert await q.feedback_get("missing") is None

    hist = await q.feedback_user_history(10)
    assert [r["id"] for r in hist] == [c.id, a.id]
    assert hist[1]["status"] == "read"


# ─── Дашборд: сторінка /feedback і /media ────────────────────────

async def _login(user) -> None:
    await aq.create_admin("tester", auth.hash_password("correct-horse-9"), telegram_id=None)
    await user.open("/login")
    user.find("Логін").type("tester")
    user.find("Пароль").type("correct-horse-9")
    user.find("Увійти").click()
    await user.should_see("Дашборд")


async def test_feedback_page_shows_kpis_and_rows(user, db):
    fsm_storage()
    await _send_feedback(12, "Хочу підтримку Amazon", lang="uk")
    await _login(user)

    await user.open("/feedback")
    await user.should_see("Нові")
    await user.should_see("Прочитані")
    await user.should_see("Відповіли")
    await user.should_see("Всього")
    await user.should_see("1 повідомлень")
    # рядки таблиці не входять у should_see — читаємо їх із самого елемента
    rows = [r for tbl in user.find(kind=ui.table).elements for r in tbl.rows]
    assert [r["text"] for r in rows] == ["Хочу підтримку Amazon"]
    assert rows[0]["status"] == "new" and rows[0]["content_type"] == "text"


async def test_dashboard_has_new_feedback_tile(user, db):
    fsm_storage()
    await _send_feedback(13, "ще один")
    await _login(user)
    await user.open("/")
    await user.should_see("Нові фідбеки")


async def test_feedback_dialog_marks_read_and_replies(db, user):
    """Логіка картки без браузера: відкриття позначає як прочитане, відповідь шле в Telegram і пише в базу."""
    fsm_storage()
    await _send_feedback(14, "питання", lang="en")
    fb = (await _rows())[0]
    await _login(user)

    from app.admin.pages import feedback as page
    refreshed = []

    async def refresh():
        refreshed.append(1)

    with patch.object(auth, "send_telegram_message", AsyncMock(return_value=True)) as send:
        await user.open("/feedback")
        with user.client:
            await page.open_feedback_dialog(await q.feedback_get(fb.id), refresh)
        assert (await q.feedback_get(fb.id))["status"] == "read"
        assert refreshed

        await user.should_see("Позначити непрочитаним")
        user.find("Текст відповіді").type("Answer!")
        user.find("Відповісти").click()
        await user.should_see("Відповідь надіслано")

    send.assert_awaited_once()
    chat_id, text = send.await_args.args
    assert chat_id == 14 and text == t("en", "feedback_reply_header", text="Answer!")
    got = await q.feedback_get(fb.id)
    assert got["status"] == "replied" and got["reply_text"] == "Answer!" and got["replied_by"] == "tester"
    log = await aq.audit_log()
    assert log[0]["action"] == "feedback_reply" and log[0]["target"] == "14" and log[0]["details"] == "Answer!"


async def test_media_route_requires_login(user, db):
    r = await user.http_client.get("/media/photo-file-id", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/login"


async def test_media_route_streams_photo_inline_and_404s_unknown(user, db):
    await _login(user)
    from botkit.admin import telegram as tg

    async def fake_path(file_id: str):
        return "photos/file_1.jpg" if file_id == "photo-file-id" else (
            "documents/file_2.pdf" if file_id == "doc-file-id" else None)

    async def fake_download(path: str):
        return (b"\xff\xd8JPEG", "image/jpeg") if path.endswith(".jpg") else (b"%PDF-1.4", "application/pdf")

    with patch.object(tg, "get_file_path", fake_path), patch.object(tg, "download_file", fake_download):
        r = await user.http_client.get("/media/photo-file-id")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/jpeg")
        assert r.headers["content-disposition"] == "inline"
        assert r.content == b"\xff\xd8JPEG"

        r = await user.http_client.get("/media/doc-file-id?name=звіт.pdf")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/pdf")
        assert r.headers["content-disposition"].startswith("attachment;")
        assert "filename*=UTF-8''%D0%B7%D0%B2%D1%96%D1%82.pdf" in r.headers["content-disposition"]

        r = await user.http_client.get("/media/unknown-id")
        assert r.status_code == 404
