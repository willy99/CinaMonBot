"""
Загальна перевірка AuthMiddleware на рівні HTTP (без браузера): будь-який невідомий шлях
без сесії → редірект на /login; статика NiceGUI (/_nicegui/...) і сам /login — без авторизації.
Маршрут /media/<file_id> (фідбек) тут не тестується.

Використовує user.http_client — httpx.AsyncClient поверх ASGI з cookie-jar сесії.

    venv/bin/python -m pytest tests/test_admin_media_guard.py -q
"""
from __future__ import annotations

from pathlib import Path

import nicegui
import pytest

from tests.admin_helpers import login, make_admin


def _static_url() -> str:
    static_dir = Path(nicegui.__file__).parent / "static"
    f = next(p for p in sorted(static_dir.iterdir()) if p.suffix in {".css", ".js"})
    return f"/_nicegui/{nicegui.__version__}/static/{f.name}"


@pytest.fixture
def http(user):
    client = getattr(user, "http_client", None)
    if client is None:
        pytest.skip("у цій версії NiceGUI user.http_client недоступний")
    return client


@pytest.mark.parametrize("path", ["/whatever", "/media/abc", "/api/anything", "/users/x/y"])
async def test_unknown_path_requires_auth(db, http, path):
    r = await http.get(path, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/login"


async def test_unknown_path_redirect_lands_on_login_page(db, user):
    await user.open("/whatever")
    await user.should_see("Увійдіть, щоб продовжити")


async def test_post_to_unknown_path_requires_auth(db, http):
    r = await http.post("/whatever", follow_redirects=False)
    assert r.status_code == 307


async def test_login_is_public(db, http):
    r = await http.get("/login", follow_redirects=False)
    assert r.status_code == 200


async def test_nicegui_static_passes_without_auth(db, http):
    r = await http.get(_static_url(), follow_redirects=False)
    assert r.status_code == 200


async def test_nicegui_prefix_unknown_file_is_not_redirected(db, http):
    r = await http.get(f"/_nicegui/{nicegui.__version__}/static/no-such-file.css", follow_redirects=False)
    assert r.status_code == 404   # не 307: middleware пропускає, далі вирішує роутер


async def test_after_login_unknown_path_is_404_not_redirect(db, user, http):
    await make_admin()
    await login(user)
    r = await http.get("/whatever", follow_redirects=False)
    assert r.status_code == 404
