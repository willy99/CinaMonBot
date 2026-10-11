"""Сторінка входу: логін + пароль → код із Telegram."""
from __future__ import annotations

from nicegui import app, ui

from app.admin import auth
from app.admin.theme import ACCENT, CSS, MUTED
from app.config import settings

LOGIN_CSS = CSS + """
.pg-login {{ min-height: 100vh; display:flex; align-items:center; justify-content:center; padding: 24px; }}
.pg-login-card {{ width: 100%; max-width: 400px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 18px; padding: 32px 30px; }}
""".replace("{{", "{").replace("}}", "}")


def _client_ip() -> str:
    try:
        return ui.context.client.ip or ""
    except Exception:
        return ""


@ui.page("/login")
async def login_page() -> None:
    ui.add_css(LOGIN_CSS)
    ui.page_title("Вхід · ЦінаБот Admin")
    ui.colors(primary=ACCENT)

    if auth.current_admin():
        ui.navigate.to("/")
        return

    pending = app.storage.user.get("pending_2fa")

    with ui.element("div").classes("pg-login w-full"):
        with ui.element("div").classes("pg-login-card"):
            with ui.row().classes("items-center gap-3 no-wrap").style("margin-bottom: 22px;"):
                ui.icon("shield", size="30px").style(f"color:{ACCENT}")
                with ui.column().classes("gap-0"):
                    ui.label("ЦінаБот Admin").style("font-weight:700; font-size:18px; line-height:1.1")
                    ui.label("Панель керування").style(f"font-size:12px; color:{MUTED}")

            container = ui.column().classes("w-full gap-3")
            if pending:
                _render_code_step(container, pending)
            else:
                _render_password_step(container)


def _render_password_step(container, notice: str = "") -> None:
    """notice — повідомлення, яке треба показати над формою (напр., "код прострочений")."""
    container.clear()
    with container:
        ui.label("Увійдіть, щоб продовжити").style(f"color:{MUTED}; font-size:13px")
        username = ui.input("Логін").props("outlined dense autofocus autocomplete=username").classes("w-full")
        password = ui.input("Пароль", password=True, password_toggle_button=True) \
            .props("outlined dense autocomplete=current-password").classes("w-full")
        error = ui.label(notice).style("color:#ffb3b3; font-size:13px; min-height:18px")

        async def submit() -> None:
            u, p = username.value.strip(), password.value
            ip = _client_ip()
            if not u or not p:
                error.text = "Введіть логін і пароль"
                return
            locked = auth.is_locked(u, ip)
            if locked:
                error.text = f"Забагато спроб. Спробуйте через {locked // 60 + 1} хв"
                return
            btn.props("loading")
            try:
                admin = await auth.authenticate(u, p)
            finally:
                btn.props(remove="loading")
            if not admin:
                auth.register_failure(u, ip)
                error.text = "Невірний логін або пароль"
                return
            auth.register_success(u, ip)

            if not settings.ADMIN_2FA_ENABLED:
                auth.login_session(admin.id, admin.username)
                await auth.mark_logged_in(admin.id)
                ui.navigate.to(app.storage.user.pop("referrer_path", "/"))
                return

            ok, msg = await auth.start_2fa(admin, ip)
            if not ok:
                error.text = msg
                return
            app.storage.user["pending_2fa"] = {"id": admin.id, "username": admin.username}
            _render_code_step(container, app.storage.user["pending_2fa"])

        password.on("keydown.enter", submit)
        username.on("keydown.enter", submit)
        btn = ui.button("Увійти", on_click=submit).props("unelevated no-caps").classes("w-full").style("height:42px")


def _render_code_step(container, pending: dict) -> None:
    container.clear()
    with container:
        ui.label(f"Крок 2 з 2 · {pending['username']}").style(f"color:{MUTED}; font-size:13px")
        ui.label("Код надіслано у ваш Telegram. Введіть його нижче.").style("font-size:14px")
        code = ui.input("Код з Telegram").props(
            "outlined dense autofocus inputmode=numeric maxlength=6 autocomplete=one-time-code"
        ).classes("w-full")
        error = ui.label().style("color:#ffb3b3; font-size:13px; min-height:18px")

        async def submit() -> None:
            ok, msg = auth.verify_2fa(pending["id"], code.value or "")
            if not ok:
                if "заново" in msg:
                    # Код прострочений / забагато спроб: назад на крок 1, але з поясненням
                    app.storage.user.pop("pending_2fa", None)
                    _render_password_step(container, notice=msg)
                    return
                error.text = msg
                return
            app.storage.user.pop("pending_2fa", None)
            auth.login_session(pending["id"], pending["username"])
            await auth.mark_logged_in(pending["id"])
            ui.navigate.to(app.storage.user.pop("referrer_path", "/"))

        def back() -> None:
            app.storage.user.pop("pending_2fa", None)
            _render_password_step(container)

        code.on("keydown.enter", submit)
        ui.button("Підтвердити", on_click=submit).props("unelevated no-caps").classes("w-full").style("height:42px")
        ui.button("Назад до входу", on_click=back).props("flat no-caps dense").classes("w-full")
