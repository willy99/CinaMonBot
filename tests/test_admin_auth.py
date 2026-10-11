"""
Тести автентифікації адмінки (app.admin.auth).

Чисті функції (паролі, антиперебір, 2FA, Telegram) — без БД; authenticate/mark_logged_in/audit — з
фікстурою db; сесія (app.storage.user) — через симульований браузер `user`, бо сховище живе
лише в контексті запиту.

    venv/bin/python -m pytest tests/test_admin_auth.py -q
"""
from __future__ import annotations

import json
import time
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from nicegui import ui
from sqlalchemy import select

from app.admin import auth
from app.admin import queries as q
from app.config import settings
from app.database.models import Admin, AdminAction
from app.database.session import get_session
from tests.admin_helpers import PASSWORD, login, make_admin, reset_auth_state


@pytest.fixture(autouse=True)
def _clean_auth_state():
    reset_auth_state()
    yield
    reset_auth_state()


# ─── Паролі ──────────────────────────────────────────────────────

def test_hash_password_round_trip():
    stored = auth.hash_password("S3cret-pass-word")
    algo, salt, digest = stored.split("$")
    assert algo == "scrypt" and len(salt) == 32 and len(digest) == 64
    assert auth.verify_password("S3cret-pass-word", stored)


def test_verify_password_rejects_wrong_password():
    stored = auth.hash_password("S3cret-pass-word")
    assert not auth.verify_password("S3cret-pass-word ", stored)
    assert not auth.verify_password("", stored)


@pytest.mark.parametrize("stored", ["", "plain", "md5$00$11", "scrypt$zz$yy", "scrypt$00", "scrypt$00$11$22"])
def test_verify_password_malformed_stored_value_is_false(stored):
    assert auth.verify_password("anything", stored) is False


def test_hash_password_uses_random_salt():
    a, b = auth.hash_password("same-pass-1"), auth.hash_password("same-pass-1")
    assert a != b
    assert auth.verify_password("same-pass-1", a) and auth.verify_password("same-pass-1", b)


@pytest.mark.parametrize("password, fragment", [
    ("short1", "10 символів"),
    ("abcdefghi", "10 символів"),
    ("1234567890", "літери, і цифри"),
    ("abcdefghij", "літери, і цифри"),
    ("абвгдежзик", "літери, і цифри"),
    ("abcdefghi1", None),
    ("correct-horse-7", None),
    ("абвгдежзи1", None),
])
def test_validate_password_strength(password, fragment):
    problem = auth.validate_password_strength(password)
    if fragment is None:
        assert problem is None
    else:
        assert fragment in problem


# ─── Антиперебір ─────────────────────────────────────────────────

def test_lockout_after_max_attempts(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_LOGIN_MAX_ATTEMPTS", 3)
    for _ in range(2):
        auth.register_failure("tester", "1.1.1.1")
        assert auth.is_locked("tester", "1.1.1.1") == 0
    auth.register_failure("tester", "1.1.1.1")
    remaining = auth.is_locked("tester", "1.1.1.1")
    assert 0 < remaining <= settings.ADMIN_LOGIN_LOCK_MINUTES * 60


def test_lockout_is_per_username_and_ip(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_LOGIN_MAX_ATTEMPTS", 2)
    auth.register_failure("tester", "1.1.1.1")
    auth.register_failure("tester", "1.1.1.1")
    assert auth.is_locked("tester", "1.1.1.1") > 0
    assert auth.is_locked("tester", "2.2.2.2") == 0
    assert auth.is_locked("other", "1.1.1.1") == 0
    # логін нечутливий до регістру — той самий ключ
    assert auth.is_locked("TESTER", "1.1.1.1") > 0


def test_register_success_clears_failures(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_LOGIN_MAX_ATTEMPTS", 2)
    auth.register_failure("tester", "ip")
    auth.register_success("tester", "ip")
    auth.register_failure("tester", "ip")
    assert auth.is_locked("tester", "ip") == 0


def test_lockout_expires(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_LOGIN_MAX_ATTEMPTS", 1)
    auth.register_failure("tester", "ip")
    assert auth.is_locked("tester", "ip") > 0
    real = time.monotonic()
    fake = types.SimpleNamespace(time=time.time,
                                 monotonic=lambda: real + settings.ADMIN_LOGIN_LOCK_MINUTES * 60 + 1)
    monkeypatch.setattr(auth, "time", fake)
    assert auth.is_locked("tester", "ip") == 0
    assert auth._attempt_key("tester", "ip") not in auth._attempts


# ─── 2FA ─────────────────────────────────────────────────────────

def _admin(**kw) -> Admin:
    return Admin(id="adm-1", username="tester", password_hash="x", **kw)


async def test_start_2fa_sends_six_digit_code(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(auth, "send_telegram_message", send)
    ok, msg = await auth.start_2fa(_admin(telegram_id=4242), "9.9.9.9")
    assert ok and "надіслано" in msg
    send.assert_awaited_once()
    chat_id, text = send.await_args.args
    assert chat_id == 4242
    pending = auth._pending["adm-1"]
    assert len(pending.code) == 6 and pending.code.isdigit()
    assert pending.code in text and "9.9.9.9" in text
    assert pending.admin == {"id": "adm-1", "username": "tester"}
    assert pending.expires_at > time.monotonic()


async def test_verify_2fa_success_is_one_shot(monkeypatch):
    monkeypatch.setattr(auth, "send_telegram_message", AsyncMock(return_value=True))
    await auth.start_2fa(_admin(telegram_id=1), "")
    code = auth._pending["adm-1"].code
    assert auth.verify_2fa("adm-1", f" {code} ") == (True, "OK")
    ok, msg = auth.verify_2fa("adm-1", code)
    assert not ok and "використаний" in msg


async def test_verify_2fa_wrong_code(monkeypatch):
    monkeypatch.setattr(auth, "send_telegram_message", AsyncMock(return_value=True))
    await auth.start_2fa(_admin(telegram_id=1), "")
    code = auth._pending["adm-1"].code
    wrong = "000000" if code != "000000" else "111111"
    assert auth.verify_2fa("adm-1", wrong) == (False, "Невірний код")
    assert "adm-1" in auth._pending  # ще можна спробувати


async def test_verify_2fa_expired(monkeypatch):
    monkeypatch.setattr(auth, "send_telegram_message", AsyncMock(return_value=True))
    await auth.start_2fa(_admin(telegram_id=1), "")
    code = auth._pending["adm-1"].code
    real = time.monotonic()
    monkeypatch.setattr(auth, "time", types.SimpleNamespace(
        time=time.time, monotonic=lambda: real + settings.ADMIN_2FA_CODE_TTL_SECONDS + 1))
    ok, msg = auth.verify_2fa("adm-1", code)
    assert not ok and "прострочений" in msg
    assert "adm-1" not in auth._pending


async def test_verify_2fa_attempts_cap(monkeypatch):
    monkeypatch.setattr(auth, "send_telegram_message", AsyncMock(return_value=True))
    await auth.start_2fa(_admin(telegram_id=1), "")
    code = auth._pending["adm-1"].code
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(auth._MAX_CODE_ATTEMPTS):
        assert auth.verify_2fa("adm-1", wrong) == (False, "Невірний код")
    ok, msg = auth.verify_2fa("adm-1", code)  # навіть правильний код після вичерпання спроб
    assert not ok and "Забагато спроб" in msg
    assert "adm-1" not in auth._pending


def test_verify_2fa_without_pending():
    ok, msg = auth.verify_2fa("nobody", "123456")
    assert not ok and "не запитувався" in msg


async def test_start_2fa_requires_telegram_id(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(auth, "send_telegram_message", send)
    ok, msg = await auth.start_2fa(_admin(telegram_id=None), "")
    assert not ok and "Telegram ID" in msg
    send.assert_not_awaited()
    assert auth._pending == {}


async def test_start_2fa_send_failure_drops_code(monkeypatch):
    monkeypatch.setattr(auth, "send_telegram_message", AsyncMock(return_value=False))
    ok, msg = await auth.start_2fa(_admin(telegram_id=1), "")
    assert not ok and "Не вдалося надіслати" in msg
    assert auth._pending == {}


# ─── Telegram Bot API ────────────────────────────────────────────

_SEND_URL = r"^https://api\.telegram\.org/bot[^/]+/sendMessage$"


async def test_send_telegram_message_ok():
    with respx.mock(assert_all_called=True) as m:
        route = m.post(url__regex=_SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
        assert await auth.send_telegram_message(7, "<b>hi</b>") is True
    req = route.calls.last.request
    assert json.loads(req.content) == {"chat_id": 7, "text": "<b>hi</b>", "parse_mode": "HTML"}
    # токен іде в URL (так працює Bot API); не друкуємо його в assert-повідомленнях
    assert settings.BOT_TOKEN.get_secret_value() in str(req.url)


async def test_send_telegram_message_http_error_is_false():
    with respx.mock(assert_all_called=True) as m:
        m.post(url__regex=_SEND_URL).mock(return_value=httpx.Response(400, json={"ok": False, "description": "chat not found"}))
        assert await auth.send_telegram_message(7, "hi") is False


async def test_send_telegram_message_network_error_is_false():
    with respx.mock(assert_all_called=True) as m:
        m.post(url__regex=_SEND_URL).mock(side_effect=httpx.ConnectError("boom"))
        assert await auth.send_telegram_message(7, "hi") is False


# ─── authenticate / mark_logged_in / audit (БД) ─────────────────

async def test_authenticate_paths(db):
    await q.create_admin("Tester", auth.hash_password(PASSWORD), None)
    assert await auth.authenticate("unknown", PASSWORD) is None
    assert await auth.authenticate("tester", "wrong-password-1") is None
    admin = await auth.authenticate("  TESTER ", PASSWORD)
    assert admin is not None and admin.username == "tester"


async def test_authenticate_inactive_admin(db):
    a = await q.create_admin("tester", auth.hash_password(PASSWORD), None)
    await q.set_admin_active(a.id, False)
    assert await auth.authenticate("tester", PASSWORD) is None


async def test_mark_logged_in_sets_last_login(db):
    a = await q.create_admin("tester", auth.hash_password(PASSWORD), None)
    async with get_session() as s:
        assert (await s.get(Admin, a.id)).last_login_at is None
    await auth.mark_logged_in(a.id)
    await auth.mark_logged_in("no-such-id")  # мовчки ігнорується
    async with get_session() as s:
        last = (await s.get(Admin, a.id)).last_login_at
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert last is not None and abs((now - last).total_seconds()) < 60


async def test_audit_without_session_is_system(db):
    await auth.audit("test_action", "tgt", "details here")
    async with get_session() as s:
        row = (await s.execute(select(AdminAction))).scalar_one()
    assert row.admin_username == "system" and row.admin_id is None
    assert (row.action, row.target, row.details) == ("test_action", "tgt", "details here")


def test_current_admin_outside_ui_context_is_none():
    assert auth.current_admin() is None


# ─── Сесія (потрібен контекст запиту → сторінка в симульованому браузері) ──

async def test_login_session_current_admin_logout(db, user):
    await make_admin()
    await login(user)

    @ui.page("/_t_session")
    def _page() -> None:
        auth.login_session("id-x", "alice")
        cur = auth.current_admin()
        ui.label(f"cur:{cur['username']}:{cur['id']}:{int(time.time() - cur['login_at'])}")
        auth.logout()
        ui.label(f"after:{auth.current_admin()}")

    await user.open("/_t_session")
    await user.should_see("cur:alice:id-x:0")
    await user.should_see("after:None")
    # logout() зняв сесію — захищені сторінки знову ведуть на логін
    await user.open("/")
    await user.should_see("Увійти")
    await user.should_not_see("адміністратор")


async def test_session_expires_after_admin_session_hours(db, user, monkeypatch):
    await make_admin()
    await login(user)
    await user.open("/users")
    await user.should_see("адміністратор")

    real = time.time()
    monkeypatch.setattr(auth, "time", types.SimpleNamespace(
        time=lambda: real + settings.ADMIN_SESSION_HOURS * 3600 + 5, monotonic=time.monotonic))
    await user.open("/users")
    await user.should_see("Увійти")

    # прострочена сесія видаляється, а не просто ховається: з реальним часом усе одно логін
    monkeypatch.setattr(auth, "time", time)
    await user.open("/users")
    await user.should_see("Увійти")
    await user.should_not_see("адміністратор")


async def test_audit_inside_session_uses_admin_username(db, user):
    admin = await make_admin("auditor")
    await login(user, "auditor")

    @ui.page("/_t_audit")
    async def _page() -> None:
        await auth.audit("page_action", "t1")
        ui.label("done")

    await user.open("/_t_audit")
    await user.should_see("done")
    async with get_session() as s:
        row = (await s.execute(select(AdminAction))).scalar_one()
    assert (row.admin_username, row.admin_id, row.action) == ("auditor", admin.id, "page_action")
