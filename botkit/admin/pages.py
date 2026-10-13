"""
Сторінки адмінки, спільні для проєктів: /login (логін + пароль → код у Telegram) і /admins (керування
адмінами: ролі owner/admin, деактивація, пароль, 2FA Telegram ID, журнал дій).

Тема й тексти — з botkit.setup(... admin_ui=AdminUI(...)). Маршрути реєструє register(): імпорт модуля
сам по собі сторінок НЕ додає (інакше ранній імпорт у тестах ламав би фікстуру NiceGUI user).

Інших адмінів керує лише owner; кожен може змінити власні пароль і Telegram ID. Зміна пароля / 2FA вимагає
поточного пароля того, хто змінює. Будь-яка зміна збільшує session_version — сесії адміна стають недійсними.
"""
from __future__ import annotations

import json

from nicegui import app, ui

from botkit.admin import auth
from botkit.admin import queries as q
from botkit.context import ctx


def _ui():
    ui_ = ctx().admin_ui
    if ui_ is None:
        raise RuntimeError("botkit: для сторінок адмінки потрібен admin_ui у botkit.setup(...)")
    return ui_


def at(key: str, **kw) -> str:
    return _ui().at(key, **kw)


def layout(title: str, active: str):
    return _ui().layout(title, active)


def card(*a, **kw):
    return _ui().card(*a, **kw)


def apply_theme() -> None:
    _ui().apply_theme()


def palette() -> dict:
    return _ui().palette()


def settings_switches() -> None:
    _ui().settings_switches()


def _login_css() -> str:
    return _ui().css + """
.pg-login { min-height: 100vh; display:flex; align-items:center; justify-content:center; padding: 24px; }
.pg-login-card { width: 100%; max-width: 400px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 18px; padding: 32px 30px; position: relative; }
.pg-login-switches { position:absolute; top: 14px; right: 14px; }
.pg-login-switches .pg-switches { gap: 4px; }
"""


def register() -> None:
    """Додає маршрути /login і /admins. Викликати з app/admin/pages/__init__.py (його виконує admin.py)."""
    ui.page("/login")(login_page)
    ui.page("/admins")(admins_page)


# ─── /login ──────────────────────────────────────────────────────

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


async def login_page() -> None:
    ui.add_css(_login_css())
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
                ui.icon(_ui().brand_icon, size="30px").style(f"color:{palette()['accent']}")
                with ui.column().classes("gap-0"):
                    ui.label(_ui().brand_title or at("page_title_suffix")).style("font-weight:700; font-size:18px; line-height:1.1")
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

            if not ctx().settings.ADMIN_2FA_ENABLED:
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


# ─── /admins ─────────────────────────────────────────────────────

# Повідомлення auth.validate_password_strength — українські рядки; переклад за текстом (як на /login),
# щоб не чіпати auth і його тести. Невідомий текст показуємо як є.
_PASSWORD_MESSAGE_KEYS = {
    "Пароль має бути не коротший за 10 символів": "admins_pw_too_short",
    "Пароль має містити і літери, і цифри": "admins_pw_letters_digits",
}


def _password_problem(password: str) -> str | None:
    problem = auth.validate_password_strength(password)
    if not problem:
        return None
    key = _PASSWORD_MESSAGE_KEYS.get(problem)
    return at(key) if key else problem


def _error_label() -> ui.label:
    return ui.label().classes("pg-danger").style("font-size:13px; min-height:18px")


def _current_password_input() -> ui.input:
    return ui.input(at("admins_current_password"), password=True, password_toggle_button=True) \
        .props("outlined dense autocomplete=current-password").classes("w-full").mark("admin-current-password")


def _may_edit(me: dict, row: dict) -> bool:
    return me.get("role") == auth.OWNER or row["id"] == me.get("id")


async def admins_page() -> None:
    with layout(at("admins_title"), "/admins"):
        me = auth.current_admin() or {}
        content = ui.column().classes("w-full gap-5")

        async def refresh() -> None:
            content.clear()
            with content:
                await _render(me, refresh)

        with content:
            await _render(me, refresh)


async def _render(me: dict, refresh) -> None:
    owner = me.get("role") == auth.OWNER
    admins = await q.admins_list()
    log = await q.audit_log(50)
    for a in admins:
        if a["last_login"] == "ніколи":       # queries віддає український літерал — мапимо на сторінці
            a["last_login"] = at("never")

    with ui.element("div").classes("w-full grid gap-4").style("grid-template-columns: minmax(0, 2fr) minmax(320px, 1fr);"):
        # ── Список ──
        with card(at("nav_admins"), at("admins_active_of", active=sum(1 for a in admins if a["active"]), total=len(admins))):
            columns = [
                {"name": "username", "label": at("login_username"), "field": "username", "align": "left"},
                {"name": "telegram_id", "label": at("admins_col_telegram"), "field": "telegram_id", "align": "left", "classes": "pg-mono"},
                {"name": "role", "label": at("admins_col_role"), "field": "role", "align": "left"},
                {"name": "active", "label": at("col_status"), "field": "active", "align": "left"},
                {"name": "last_login", "label": at("admins_col_last_login"), "field": "last_login", "align": "left"},
                {"name": "created", "label": at("col_created"), "field": "created", "align": "left"},
                {"name": "actions", "label": "", "field": "id", "align": "right"},
            ]
            tbl = ui.table(columns=columns, rows=admins, row_key="id").props("flat dense").classes("w-full")
            tbl.add_slot("body-cell-active", r'''
                <q-td :props="props">
                  <span class="pg-badge" :class="props.value ? 'good' : 'critical'"><span class="dot"></span>{{ props.value ? __ACTIVE__ : __DISABLED__ }}</span>
                </q-td>'''.replace("__ACTIVE__", json.dumps(at("status_active"), ensure_ascii=False))
                .replace("__DISABLED__", json.dumps(at("admins_status_disabled"), ensure_ascii=False)))
            tbl.add_slot("body-cell-actions", r'''
                <q-td :props="props">
                  <template v-if="__OWNER__ || props.row.id === __ME__">
                    <q-btn flat dense round icon="key" size="sm" @click="$parent.$emit('password', props.row)"><q-tooltip>__PASSWORD__</q-tooltip></q-btn>
                    <q-btn flat dense round icon="send" size="sm" @click="$parent.$emit('telegram', props.row)"><q-tooltip>__TELEGRAM__</q-tooltip></q-btn>
                  </template>
                  <template v-if="__OWNER__ && props.row.id !== __ME__">
                    <q-btn flat dense round icon="shield" size="sm" @click="$parent.$emit('role', props.row)"><q-tooltip>__ROLE__</q-tooltip></q-btn>
                    <q-btn v-if="props.row.active" flat dense round icon="person_off" size="sm" color="negative" @click="$parent.$emit('deactivate', props.row)"><q-tooltip>__DEACTIVATE__</q-tooltip></q-btn>
                    <q-btn v-else flat dense round icon="person" size="sm" color="positive" @click="$parent.$emit('activate', props.row)"><q-tooltip>__ACTIVATE__</q-tooltip></q-btn>
                  </template>
                </q-td>'''.replace("__PASSWORD__", at("admins_tip_password")).replace("__TELEGRAM__", at("admins_tip_telegram"))
                .replace("__DEACTIVATE__", at("btn_deactivate")).replace("__ACTIVATE__", at("btn_activate"))
                .replace("__ROLE__", at("admins_tip_role")).replace("__OWNER__", "true" if owner else "false")
                .replace("__ME__", "'" + str(me.get("id") or "") + "'"))   # UUID; одинарні лапки — атрибут v-if у подвійних
            tbl.add_slot("body-cell-role", r'''
                <q-td :props="props">{{ props.value === 'owner' ? __OWNER_L__ : __ADMIN_L__ }}</q-td>'''
                .replace("__OWNER_L__", json.dumps(at("role_owner"), ensure_ascii=False))
                .replace("__ADMIN_L__", json.dumps(at("role_admin"), ensure_ascii=False)))

            def deny() -> bool:
                if not owner:
                    ui.notify(at("owner_only"), type="negative")
                return not owner

            async def deactivate(e) -> None:
                row = e.args
                if deny():
                    return
                if row["id"] == me.get("id"):
                    ui.notify(at("admins_cant_self_deactivate"), type="negative")
                    return
                if await q.admins_count_active() <= 1:
                    ui.notify(at("admins_keep_one_active"), type="negative")
                    return
                if row["role"] == auth.OWNER and await q.owners_count_active() <= 1:
                    ui.notify(at("admins_keep_one_owner"), type="negative")
                    return
                await q.set_admin_active(row["id"], False)
                await auth.audit("admin_deactivate", row["username"])
                ui.notify(at("admins_deactivated", name=row["username"]), type="warning")
                await refresh()

            async def activate(e) -> None:
                row = e.args
                if deny():
                    return
                await q.set_admin_active(row["id"], True)
                await auth.audit("admin_activate", row["username"])
                ui.notify(at("admins_activated", name=row["username"]), type="positive")
                await refresh()

            async def role(e) -> None:
                row = e.args
                if deny() or row["id"] == me.get("id"):
                    return
                new = "admin" if row["role"] == auth.OWNER else auth.OWNER
                if new == "admin" and await q.owners_count_active() <= 1:
                    ui.notify(at("admins_keep_one_owner"), type="negative")
                    return
                await q.set_admin_role(row["id"], new)
                await auth.audit("admin_role_change", row["username"], new)
                ui.notify(at("admins_role_changed", name=row["username"]), type="positive")
                await refresh()

            def change_password(e) -> None:
                row = e.args
                if not _may_edit(me, row):
                    ui.notify(at("owner_only"), type="negative")
                    return
                with ui.dialog() as dlg, ui.card().style("min-width: 380px"):
                    ui.label(at("admins_new_password_for", name=row["username"])).style("font-weight:600")
                    pw = ui.input(at("login_password"), password=True, password_toggle_button=True) \
                        .props("outlined dense").classes("w-full").mark("admin-new-password")
                    current = _current_password_input()
                    err = _error_label()

                    async def save() -> None:
                        problem = _password_problem(pw.value or "")
                        if problem:
                            err.text = problem
                            return
                        if not await auth.verify_current_password(me.get("id"), current.value):
                            err.text = at("admins_wrong_password")
                            return
                        await q.set_admin_password(row["id"], auth.hash_password(pw.value))
                        if row["id"] == me.get("id"):
                            await auth.login_session(row["id"])          # власна сесія — з новою версією
                        await auth.audit("admin_password_reset", row["username"])
                        ui.notify(at("admins_password_changed"), type="positive")
                        dlg.close()

                    with ui.row().classes("justify-end w-full gap-2"):
                        ui.button(at("btn_cancel"), on_click=dlg.close).props("flat no-caps")
                        ui.button(at("btn_save"), on_click=save).props("unelevated no-caps")
                dlg.open()

            def change_telegram(e) -> None:
                row = e.args
                if not _may_edit(me, row):
                    ui.notify(at("owner_only"), type="negative")
                    return
                with ui.dialog() as dlg, ui.card().style("min-width: 380px"):
                    ui.label(at("admins_telegram_for", name=row["username"])).style("font-weight:600")
                    ui.label(at("admins_telegram_hint")).classes("pg-muted").style("font-size:12px")
                    tg = ui.input("Telegram ID", value=str(row["telegram_id"] or "")).props("outlined dense inputmode=numeric") \
                        .classes("w-full").mark("admin-telegram")
                    current = _current_password_input()
                    err = _error_label()

                    async def save() -> None:
                        raw = (tg.value or "").strip()
                        if raw and not raw.isdigit():
                            err.text = at("admins_must_be_number")
                            return
                        if not await auth.verify_current_password(me.get("id"), current.value):
                            err.text = at("admins_wrong_password")
                            return
                        await q.set_admin_telegram(row["id"], int(raw) if raw else None)
                        if row["id"] == me.get("id"):
                            await auth.login_session(row["id"])
                        await auth.audit("admin_telegram_change", row["username"], raw or "—")
                        ui.notify(at("admins_telegram_updated"), type="positive")
                        dlg.close()
                        await refresh()

                    with ui.row().classes("justify-end w-full gap-2"):
                        ui.button(at("btn_cancel"), on_click=dlg.close).props("flat no-caps")
                        ui.button(at("btn_save"), on_click=save).props("unelevated no-caps")
                dlg.open()

            tbl.on("deactivate", deactivate)
            tbl.on("activate", activate)
            tbl.on("password", change_password)
            tbl.on("telegram", change_telegram)
            tbl.on("role", role)

        # ── Додати (лише owner) ──
        if not owner:
            with card(at("admins_new_title")):
                ui.label(at("owner_only")).classes("pg-muted").style("font-size:13px")
        else:
            with card(at("admins_new_title")):
                username = ui.input(at("login_username")).props("outlined dense autocomplete=off").classes("w-full")
                password = ui.input(at("login_password"), password=True, password_toggle_button=True) \
                    .props("outlined dense autocomplete=new-password").classes("w-full")
                telegram = ui.input(at("admins_telegram_label")).props("outlined dense inputmode=numeric").classes("w-full")
                ui.label(at("admins_new_hint")).classes("pg-muted").style("font-size:12px")
                err = _error_label()

                async def create() -> None:
                    if deny():
                        return
                    u = (username.value or "").strip().lower()
                    p = password.value or ""
                    tg = (telegram.value or "").strip()
                    if not u or len(u) < 3 or not u.replace("_", "").replace(".", "").isalnum():
                        err.text = at("admins_err_username")
                        return
                    problem = _password_problem(p)
                    if problem:
                        err.text = problem
                        return
                    if tg and not tg.isdigit():
                        err.text = at("admins_err_telegram_number")
                        return
                    if not tg and ctx().settings.ADMIN_2FA_ENABLED:
                        err.text = at("admins_err_2fa_required")
                        return
                    if await q.admin_exists(u):
                        err.text = at("admins_err_exists")
                        return
                    await q.create_admin(u, auth.hash_password(p), int(tg) if tg else None)
                    await auth.audit("admin_create", u, f"telegram_id={tg or '—'}")
                    ui.notify(at("admins_created", name=u), type="positive")
                    await refresh()

                ui.button(at("admins_btn_create"), icon="person_add", on_click=create).props("unelevated no-caps").classes("w-full")

    # ── Журнал ──
    with card(at("admins_log_title"), at("admins_log_sub", n=50)):
        if not log:
            ui.label(at("admins_log_empty")).classes("pg-muted").style("font-size:13px")
        else:
            columns = [
                {"name": "when", "label": at("col_when"), "field": "when", "align": "left"},
                {"name": "admin", "label": at("col_admin"), "field": "admin", "align": "left"},
                {"name": "action", "label": at("col_action"), "field": "action", "align": "left", "classes": "pg-mono"},
                {"name": "target", "label": at("col_target"), "field": "target", "align": "left", "classes": "pg-mono"},
                {"name": "details", "label": at("col_details"), "field": "details", "align": "left"},
            ]
            ui.table(columns=columns, rows=log, pagination=15) \
                .props(f'flat dense rows-per-page-label="{at("rows_per_page")}"').classes("w-full")
