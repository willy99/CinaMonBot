"""Керування адмінами: додати, деактивувати, скинути пароль, змінити Telegram ID + журнал дій."""
from __future__ import annotations

from nicegui import ui

from app.admin import auth
from app.admin import queries as q
from app.admin.theme import MUTED, card, layout
from app.config import settings


@ui.page("/admins")
async def admins_page() -> None:
    with layout("Адміністратори", "/admins"):
        me = auth.current_admin() or {}
        content = ui.column().classes("w-full gap-5")

        async def refresh() -> None:
            content.clear()
            with content:
                await _render(me, refresh)

        with content:
            await _render(me, refresh)


async def _render(me: dict, refresh) -> None:
    admins = await q.admins_list()
    log = await q.audit_log(50)

    with ui.element("div").classes("w-full grid gap-4").style("grid-template-columns: minmax(0, 2fr) minmax(320px, 1fr);"):
        # ── Список ──
        with card("Адміни", f"{sum(1 for a in admins if a['active'])} активних з {len(admins)}"):
            columns = [
                {"name": "username", "label": "Логін", "field": "username", "align": "left"},
                {"name": "telegram_id", "label": "Telegram ID (2FA)", "field": "telegram_id", "align": "left", "classes": "pg-mono"},
                {"name": "active", "label": "Статус", "field": "active", "align": "left"},
                {"name": "last_login", "label": "Останній вхід", "field": "last_login", "align": "left"},
                {"name": "created", "label": "Створено", "field": "created", "align": "left"},
                {"name": "actions", "label": "", "field": "id", "align": "right"},
            ]
            tbl = ui.table(columns=columns, rows=admins, row_key="id").props("flat dense").classes("w-full")
            tbl.add_slot("body-cell-active", r'''
                <q-td :props="props">
                  <span class="pg-badge" :class="props.value ? 'good' : 'critical'"><span class="dot"></span>{{ props.value ? 'Активний' : 'Вимкнений' }}</span>
                </q-td>''')
            tbl.add_slot("body-cell-actions", r'''
                <q-td :props="props">
                  <q-btn flat dense round icon="key" size="sm" @click="$parent.$emit('password', props.row)"><q-tooltip>Змінити пароль</q-tooltip></q-btn>
                  <q-btn flat dense round icon="send" size="sm" @click="$parent.$emit('telegram', props.row)"><q-tooltip>Змінити Telegram ID</q-tooltip></q-btn>
                  <q-btn v-if="props.row.active" flat dense round icon="person_off" size="sm" color="negative" @click="$parent.$emit('deactivate', props.row)"><q-tooltip>Деактивувати</q-tooltip></q-btn>
                  <q-btn v-else flat dense round icon="person" size="sm" color="positive" @click="$parent.$emit('activate', props.row)"><q-tooltip>Активувати</q-tooltip></q-btn>
                </q-td>''')

            async def deactivate(e) -> None:
                row = e.args
                if row["id"] == me.get("id"):
                    ui.notify("Не можна деактивувати самого себе", type="negative")
                    return
                if await q.admins_count_active() <= 1:
                    ui.notify("Має залишитись хоча б один активний адмін", type="negative")
                    return
                await q.set_admin_active(row["id"], False)
                await auth.audit("admin_deactivate", row["username"])
                ui.notify(f"{row['username']} деактивовано", type="warning")
                await refresh()

            async def activate(e) -> None:
                row = e.args
                await q.set_admin_active(row["id"], True)
                await auth.audit("admin_activate", row["username"])
                ui.notify(f"{row['username']} активовано", type="positive")
                await refresh()

            def change_password(e) -> None:
                row = e.args
                with ui.dialog() as dlg, ui.card().style("min-width: 380px"):
                    ui.label(f"Новий пароль для {row['username']}").style("font-weight:600")
                    pw = ui.input("Пароль", password=True, password_toggle_button=True).props("outlined dense").classes("w-full")
                    err = ui.label().style("color:#ffb3b3; font-size:13px; min-height:18px")

                    async def save() -> None:
                        problem = auth.validate_password_strength(pw.value or "")
                        if problem:
                            err.text = problem
                            return
                        await q.set_admin_password(row["id"], auth.hash_password(pw.value))
                        await auth.audit("admin_password_reset", row["username"])
                        ui.notify("Пароль змінено", type="positive")
                        dlg.close()

                    with ui.row().classes("justify-end w-full gap-2"):
                        ui.button("Скасувати", on_click=dlg.close).props("flat no-caps")
                        ui.button("Зберегти", on_click=save).props("unelevated no-caps")
                dlg.open()

            def change_telegram(e) -> None:
                row = e.args
                with ui.dialog() as dlg, ui.card().style("min-width: 380px"):
                    ui.label(f"Telegram ID для 2FA · {row['username']}").style("font-weight:600")
                    ui.label("Адмін має хоча б раз написати боту, інакше код не дійде.").style(f"font-size:12px; color:{MUTED}")
                    tg = ui.input("Telegram ID", value=str(row["telegram_id"] or "")).props("outlined dense inputmode=numeric").classes("w-full")
                    err = ui.label().style("color:#ffb3b3; font-size:13px; min-height:18px")

                    async def save() -> None:
                        raw = (tg.value or "").strip()
                        if raw and not raw.isdigit():
                            err.text = "Має бути число"
                            return
                        await q.set_admin_telegram(row["id"], int(raw) if raw else None)
                        await auth.audit("admin_telegram_change", row["username"], raw or "—")
                        ui.notify("Telegram ID оновлено", type="positive")
                        dlg.close()
                        await refresh()

                    with ui.row().classes("justify-end w-full gap-2"):
                        ui.button("Скасувати", on_click=dlg.close).props("flat no-caps")
                        ui.button("Зберегти", on_click=save).props("unelevated no-caps")
                dlg.open()

            tbl.on("deactivate", deactivate)
            tbl.on("activate", activate)
            tbl.on("password", change_password)
            tbl.on("telegram", change_telegram)

        # ── Додати ──
        with card("Новий адмін"):
            username = ui.input("Логін").props("outlined dense autocomplete=off").classes("w-full")
            password = ui.input("Пароль", password=True, password_toggle_button=True) \
                .props("outlined dense autocomplete=new-password").classes("w-full")
            telegram = ui.input("Telegram ID для 2FA").props("outlined dense inputmode=numeric").classes("w-full")
            ui.label("Мінімум 10 символів, літери і цифри. Telegram ID можна дізнатися через @userinfobot.") \
                .style(f"font-size:12px; color:{MUTED}")
            err = ui.label().style("color:#ffb3b3; font-size:13px; min-height:18px")

            async def create() -> None:
                u = (username.value or "").strip().lower()
                p = password.value or ""
                tg = (telegram.value or "").strip()
                if not u or len(u) < 3 or not u.replace("_", "").replace(".", "").isalnum():
                    err.text = "Логін: латиниця, цифри, _ або ., від 3 символів"
                    return
                problem = auth.validate_password_strength(p)
                if problem:
                    err.text = problem
                    return
                if tg and not tg.isdigit():
                    err.text = "Telegram ID має бути числом"
                    return
                if not tg and settings.ADMIN_2FA_ENABLED:
                    err.text = "2FA увімкнено: Telegram ID обов'язковий"
                    return
                if await q.admin_exists(u):
                    err.text = "Такий логін уже є"
                    return
                await q.create_admin(u, auth.hash_password(p), int(tg) if tg else None)
                await auth.audit("admin_create", u, f"telegram_id={tg or '—'}")
                ui.notify(f"Адміна {u} створено", type="positive")
                await refresh()

            ui.button("Створити", icon="person_add", on_click=create).props("unelevated no-caps").classes("w-full")

    # ── Журнал ──
    with card("Журнал дій", "останні 50"):
        if not log:
            ui.label("Поки порожньо").classes("pg-muted").style("font-size:13px")
        else:
            columns = [
                {"name": "when", "label": "Коли", "field": "when", "align": "left"},
                {"name": "admin", "label": "Адмін", "field": "admin", "align": "left"},
                {"name": "action", "label": "Дія", "field": "action", "align": "left", "classes": "pg-mono"},
                {"name": "target", "label": "Ціль", "field": "target", "align": "left", "classes": "pg-mono"},
                {"name": "details", "label": "Деталі", "field": "details", "align": "left"},
            ]
            ui.table(columns=columns, rows=log, pagination=15).props('flat dense rows-per-page-label="Рядків:"').classes("w-full")
