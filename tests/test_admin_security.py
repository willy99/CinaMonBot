"""S6: сесії адмінки відкликаються, ролі owner/admin, повторний пароль, блокування за логіном у БД; S12: редирект."""
from __future__ import annotations

import pytest

from botkit.admin import auth
from botkit.admin import queries as aq
from app.config import settings
from tests.admin_helpers import PASSWORD, login, make_admin, reset_auth_state


@pytest.fixture(autouse=True)
def clean_auth(db):
    reset_auth_state()
    yield
    reset_auth_state()


async def test_deactivation_revokes_open_session(user):
    a = await make_admin()
    await make_admin("second")
    await login(user)
    await aq.set_admin_active(a.id, False)
    auth.forget_session_cache()
    await user.open("/users")
    await user.should_see("Увійти", retries=40)


async def test_password_change_revokes_other_sessions(user):
    a = await make_admin()
    await login(user)
    await aq.set_admin_password(a.id, auth.hash_password("another-pass-42"))
    auth.forget_session_cache()
    await user.open("/")
    await user.should_see("Увійти", retries=40)


async def test_plain_admin_cannot_manage_admins(user):
    await make_admin("boss")
    await make_admin("tester", role="admin")
    await login(user)
    await user.open("/admins")
    await user.should_see("Лише власник (owner) може це робити.", retries=40)
    await user.should_not_see("Створити адміна")


async def test_verify_current_password():
    a = await make_admin()
    assert await auth.verify_current_password(a.id, PASSWORD)
    assert not await auth.verify_current_password(a.id, "nope")


async def test_db_lockout_ignores_ip():
    await make_admin()
    for _ in range(settings.ADMIN_LOGIN_MAX_ATTEMPTS * auth.DB_LOCK_FACTOR):
        assert await auth.db_lock_remaining("tester") == 0
        await auth.db_register_failure("tester")            # з різних IP — лічильник один
    assert await auth.db_lock_remaining("TESTER") > 0


async def test_ensure_owner_promotes_oldest_active():
    first = await make_admin("first", role="admin")
    await make_admin("second", role="admin")
    assert await aq.ensure_owner() == "first"
    assert await aq.ensure_owner() is None
    assert [a["role"] for a in await aq.admins_list() if a["id"] == first.id] == ["owner"]


@pytest.mark.parametrize("path,expected", [("/users", "/users"), ("//evil.example", "/"), ("/\\evil.example", "/"),
                                           ("https://evil.example", "/"), ("", "/")])
def test_safe_next(path, expected):
    assert auth.safe_next_path(path) == expected


def assert_vue_attributes_intact(user) -> None:
    """Симульований браузер не компілює Vue: ловимо обірвані атрибути (подвійні лапки всередині v-if="…"),
    через які Quasar не рендерить таблицю зовсім — так «зламалась» сторінка /admins."""
    import re

    from nicegui import ui
    for table in user.find(kind=ui.table).elements:
        for name, slot in table.slots.items():
            for m in re.finditer(r'\s((?:v-[\w-]+|:[\w.-]+|@[\w.-]+))="([^"]*)"(.)', slot.template or "", re.S):
                assert m.group(3) in " \t\n>/", f"{name}: обірваний атрибут {m.group(1)}={m.group(2)!r}"


@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_admins_page_slots_compile(user, role):
    await make_admin("boss", role="owner")
    await make_admin("tester", role=role)
    await login(user)
    await user.open("/admins")
    await user.should_see("tester", retries=40)
    assert_vue_attributes_intact(user)
