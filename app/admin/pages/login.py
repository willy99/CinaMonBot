"""Сторінка входу: логін + пароль → код із Telegram. Перемикачі мови/теми — у правому верхньому куті картки."""
from __future__ import annotations

from nicegui import app, ui

from app.admin import auth
from app.admin.i18n import at
from app.admin.theme import CSS, apply_theme, palette, settings_switches
from app.config import settings

LOGIN_CSS = CSS + """
.pg-login {{ min-height: 100vh; display:flex; align-items:center; justify-content:center; padding: 24px; }}
.pg-login-card {{ width: 100%; max-width: 400px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 18px; padding: 32px 30px; position: relative; }}
.pg-login-switches {{ position:absolute; top: 14px; right: 14px; }}
.pg-login-switches .pg-switches {{ gap: 4px; }}
""".replace("{{", "{").replace("}}", "}")

# Повідомлення з app/admin/auth.py (start_2fa / verify_2fa) — українські рядки; тут переклад за текстом,
# щоб не чіпати auth і його тести. Невідомий текст показуємо як є.
_AUTH_MESSAGE_KEYS = {
    "У цього адміна не вказано Telegram ID для 2FA": "auth_no_telegram_id",
    "Не вдалося надіслати код у Telegram. Переконайтесь, що ви писали боту.": "auth_send_failed",
    "Код не запитувався або вже використаний": "auth_code_not_requested",
    "Код прострочений, увійдіть заново": "auth_code_expired",
    "Забагато спроб, увійдіть заново": "auth_too_many_attempts",
    "Невірний код": "auth_code_invalid",
}


def _auth_message(msg: str) -> str:
    key = _AUTH_MESSAGE_KEYS.get(msg)
    return at(key) if key else msg


def _client_ip() -> str:
    try:
        return ui.context.client.ip or ""
    except Exception:
        return ""


@ui.page("/login")
async def login_page() -> None:
    ui.add_css(LOGIN_CSS)
    apply_theme()
    ui.page_title(f"{at('login_title')} · {at('page_title_suffix')}")

    if auth.current_admin():
        ui.navigate.to("/")
        return

    pending = app.storage.user.get("pending_2fa")

    with ui.element("div").classes("pg-login w-full"):
        with ui.element("div").classes("pg-login-card"):
            with ui.element("div").classes("pg-login-switches"):
                settings_switches()
            with ui.row().classes("items-center gap-3 no-wrap").style("margin-bottom: 22px;"):
                ui.icon("shield", size="30px").style(f"color:{palette()['accent']}")
                with ui.column().classes("gap-0"):
                    ui.label("ЦінаБот Admin").style("font-weight:700; font-size:18px; line-height:1.1")
                    ui.label(at("login_subtitle")).classes("pg-muted").style("font-size:12px")

            container = ui.column().classes("w-full gap-3")
            if pending:
                _render_code_step(container, pending)
            else:
                _render_password_step(container)


def _render_password_step(container, notice: str = "") -> None:
    """notice — повідомлення, яке треба показати над формою (напр., "код прострочений")."""
    container.clear()
    with container:
        ui.label(at("login_prompt")).classes("pg-muted").style("font-size:13px")
        username = ui.input(at("login_username")).props("outlined dense autofocus autocomplete=username").classes("w-full")
        password = ui.input(at("login_password"), password=True, password_toggle_button=True) \
            .props("outlined dense autocomplete=current-password").classes("w-full")
        error = ui.label(notice).classes("pg-danger").style("font-size:13px; min-height:18px")

        async def submit() -> None:
            u, p = username.value.strip(), password.value
            ip = _client_ip()
            if not u or not p:
                error.text = at("login_empty")
                return
            locked = auth.is_locked(u, ip) or await auth.db_lock_remaining(u)
            if locked:
                error.text = at("login_locked", minutes=locked // 60 + 1)
                return
            btn.props("loading")
            try:
                admin = await auth.authenticate(u, p)
            finally:
                btn.props(remove="loading")
            if not admin:
                auth.register_failure(u, ip)
                await auth.db_register_failure(u)
                error.text = at("login_invalid")
                return
            auth.register_success(u, ip)
            await auth.db_register_success(admin.id)

            if not settings.ADMIN_2FA_ENABLED:
                await auth.login_session(admin.id)
                await auth.mark_logged_in(admin.id)
                ui.navigate.to(auth.safe_next_path(app.storage.user.pop("referrer_path", "/")))
                return

            ok, msg = await auth.start_2fa(admin, ip)
            if not ok:
                error.text = _auth_message(msg)
                return
            app.storage.user["pending_2fa"] = {"id": admin.id, "username": admin.username}
            _render_code_step(container, app.storage.user["pending_2fa"])

        password.on("keydown.enter", submit)
        username.on("keydown.enter", submit)
        btn = ui.button(at("login_submit"), on_click=submit).props("unelevated no-caps").classes("w-full").style("height:42px")


def _render_code_step(container, pending: dict) -> None:
    container.clear()
    with container:
        ui.label(at("login_step2", username=pending["username"])).classes("pg-muted").style("font-size:13px")
        ui.label(at("login_code_sent")).style("font-size:14px")
        code = ui.input(at("login_code")).props(
            "outlined dense autofocus inputmode=numeric maxlength=6 autocomplete=one-time-code"
        ).classes("w-full")
        error = ui.label().classes("pg-danger").style("font-size:13px; min-height:18px")

        async def submit() -> None:
            ok, msg = auth.verify_2fa(pending["id"], code.value or "")
            if not ok:
                if "заново" in msg:
                    # Код прострочений / забагато спроб: назад на крок 1, але з поясненням
                    app.storage.user.pop("pending_2fa", None)
                    _render_password_step(container, notice=_auth_message(msg))
                    return
                error.text = _auth_message(msg)
                return
            app.storage.user.pop("pending_2fa", None)
            await auth.login_session(pending["id"])
            await auth.mark_logged_in(pending["id"])
            ui.navigate.to(auth.safe_next_path(app.storage.user.pop("referrer_path", "/")))

        def back() -> None:
            app.storage.user.pop("pending_2fa", None)
            _render_password_step(container)

        code.on("keydown.enter", submit)
        ui.button(at("login_confirm"), on_click=submit).props("unelevated no-caps").classes("w-full").style("height:42px")
        ui.button(at("login_back"), on_click=back).props("flat no-caps dense").classes("w-full")
