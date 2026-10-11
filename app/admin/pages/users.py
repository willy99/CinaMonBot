"""Список користувачів з пошуком і фільтрами + картка користувача з діями."""
from __future__ import annotations

import asyncio

from nicegui import ui

from app.admin import auth
from app.admin import queries as q
from app.admin.theme import (
    MUTED, badge, card, kpi, layout, money, price_history_chart, status_badge, target_label,
)
from app.database.models import TrackerStatus

TIER_BADGE_SLOT = r'''
<q-td :props="props">
  <span class="pg-badge" :class="props.row.blocked ? 'critical' : props.value === 'premium' ? 'premium' : ''">
    <span class="dot"></span>
    {{ props.row.blocked ? 'Заблокований' : props.value === 'premium' ? 'Premium · до ' + props.row.until : 'Free' }}
  </span>
</q-td>'''


@ui.page("/users")
async def users_page() -> None:
    with layout("Користувачі", "/users"):
        try:
            _qp = dict(ui.context.client.request.query_params)
        except Exception:  # noqa: BLE001
            _qp = {}
        state = {
            "search": _qp.get("q", ""),
            "tier": _qp.get("tier") if _qp.get("tier") in ("premium", "free") else "all",
            "status": _qp.get("status") if _qp.get("status") in ("active", "blocked") else "all",
        }

        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            search = ui.input(placeholder="Пошук: ім'я, username або Telegram ID") \
                .props("outlined dense clearable debounce=300").classes("flex-grow").style("max-width: 420px")
            with search.add_slot("prepend"):
                ui.icon("search")
            tier = ui.toggle({"all": "Усі", "premium": "Premium", "free": "Free"}, value=state["tier"]) \
                .props("no-caps unelevated dense toggle-color=primary")
            status = ui.toggle({"all": "Будь-який статус", "active": "Активні", "blocked": "Заблоковані"}, value=state["status"]) \
                .props("no-caps unelevated dense toggle-color=primary")
            ui.space()
            count_label = ui.label().classes("pg-muted").style("font-size:13px; white-space:nowrap")

        columns = [
            {"name": "name", "label": "Ім'я", "field": "name", "align": "left", "sortable": True},
            {"name": "username", "label": "Username", "field": "username", "align": "left"},
            {"name": "telegram_id", "label": "Telegram ID", "field": "telegram_id", "align": "left", "classes": "pg-mono"},
            {"name": "lang", "label": "Мова", "field": "lang", "align": "left"},
            {"name": "tier", "label": "План", "field": "tier", "align": "left", "sortable": True},
            {"name": "trackers", "label": "Товари", "field": "trackers", "align": "right", "sortable": True},
            {"name": "created", "label": "Зареєстровано", "field": "created", "align": "left",
             "sortable": True, "sort": "(a, b, ra, rb) => ra.created_sort < rb.created_sort ? -1 : 1"},
        ]
        with card():
            table = ui.table(columns=columns, rows=[], row_key="id", pagination=25) \
                .props('flat rows-per-page-label="Рядків:"').classes("w-full")
            table.add_slot("body-cell-tier", TIER_BADGE_SLOT)
            table.add_slot("body-cell-trackers", r'''
                <q-td :props="props">{{ props.value }} <span style="color:#898781">/ {{ props.row.trackers_active }} акт.</span></q-td>''')
            table.add_slot("no-data", '<div class="full-width row flex-center q-pa-md" style="color:#898781">Нікого не знайдено</div>')
            table.on("rowClick", lambda e: ui.navigate.to(f"/users/{e.args[1]['id']}"))

        async def reload() -> None:
            rows = await q.users_list(state["search"], state["tier"], state["status"])
            table.rows = rows
            table.update()
            count_label.text = f"{len(rows)} користувачів"

        def on_search(e):
            state["search"] = e.value or ""
            asyncio.create_task(reload())

        def on_tier(e):
            state["tier"] = e.value or "all"
            asyncio.create_task(reload())

        def on_status(e):
            state["status"] = e.value or "all"
            asyncio.create_task(reload())

        search.on_value_change(on_search)
        tier.on_value_change(on_tier)
        status.on_value_change(on_status)
        await reload()


@ui.page("/users/{user_id}")
async def user_page(user_id: str) -> None:
    with layout("Користувач", "/users"):
        data = await q.user_detail(user_id)
        if not data:
            ui.label("Користувача не знайдено").classes("pg-muted")
            ui.link("← До списку", "/users").classes("pg-link")
            return

        content = ui.column().classes("w-full gap-5")

        async def refresh() -> None:
            fresh = await q.user_detail(user_id)
            content.clear()
            with content:
                _render_user(fresh, refresh)

        with content:
            _render_user(data, refresh)


def _render_user(d: dict, refresh) -> None:
    ui.link("← Усі користувачі", "/users").classes("pg-link").style("font-size:13px")

    # ── Шапка ──
    with card():
        with ui.row().classes("w-full items-start justify-between no-wrap gap-4"):
            with ui.column().classes("gap-1"):
                with ui.row().classes("items-center gap-3"):
                    ui.label(d["name"]).style("font-size:22px; font-weight:700")
                    if d["username"]:
                        ui.link(f"@{d['username']}", f"https://t.me/{d['username']}", new_tab=True).classes("pg-link")
                    if d["blocked"]:
                        badge("Заблокований", "critical")
                    elif d["tier"] == "premium":
                        badge(f"Premium · до {d['premium_until_str']}", "premium")
                    else:
                        badge("Free")
                with ui.row().classes("items-center gap-4").style(f"font-size:13px; color:{MUTED}"):
                    ui.label(f"Telegram ID: {d['telegram_id']}").classes("pg-mono")
                    ui.label(f"Мова: {d['lang']}")
                    ui.label(f"📬 дайджест: {'увімкнено' if d.get('digest_enabled', True) else 'вимкнено'}")
                    ui.label(f"Зареєстровано: {d['created']}")
                    ui.label(f"Товарів: {len(d['trackers'])}")

            with ui.row().classes("items-center gap-2 no-wrap"):
                days = ui.number(value=30, min=1, max=3650, step=1).props("outlined dense suffix=дн.").style("width: 110px")

                async def activate() -> None:
                    n = int(days.value or 30)
                    until = await q.set_premium(d["id"], n)
                    await auth.audit("premium_activate", str(d["telegram_id"]), f"+{n} днів → {until:%d.%m.%Y}")
                    ui.notify(f"Premium активовано до {until:%d.%m.%Y}", type="positive")
                    await refresh()

                async def revoke() -> None:
                    await q.revoke_premium(d["id"])
                    await auth.audit("premium_revoke", str(d["telegram_id"]))
                    ui.notify("Premium деактивовано", type="warning")
                    await refresh()

                async def toggle_block() -> None:
                    await q.set_blocked(d["id"], not d["blocked"])
                    await auth.audit("user_unblock" if d["blocked"] else "user_block", str(d["telegram_id"]))
                    ui.notify("Розблоковано" if d["blocked"] else "Заблоковано", type="info")
                    await refresh()

                ui.button("Активувати Premium", icon="workspace_premium", on_click=activate).props("unelevated no-caps")
                if d["tier"] == "premium":
                    ui.button("Деактивувати", icon="remove_circle_outline", on_click=revoke).props("outline no-caps color=warning")
                ui.button("Розблокувати" if d["blocked"] else "Заблокувати",
                          icon="lock_open" if d["blocked"] else "block", on_click=toggle_block) \
                    .props(f"outline no-caps color={'positive' if d['blocked'] else 'negative'}")

    # ── Трекери ──
    with card("Товари користувача", f"{len(d['trackers'])} всього"):
        if not d["trackers"]:
            ui.label("Нічого не додано").classes("pg-muted").style("font-size:13px")
        else:
            rows = [{
                **t,
                "price_str": money(t["price"], t["currency"]),
                "target_str": target_label(t["target"], t["currency"], t.get("target_percent")),
            } for t in d["trackers"]]
            columns = [
                {"name": "title", "label": "Товар", "field": "title", "align": "left"},
                {"name": "source", "label": "Сайт", "field": "source", "align": "left"},
                {"name": "price_str", "label": "Ціна", "field": "price_str", "align": "right"},
                {"name": "target_str", "label": "Ціль", "field": "target_str", "align": "right"},
                {"name": "status", "label": "Статус", "field": "status", "align": "left"},
                {"name": "last_checked", "label": "Перевірено", "field": "last_checked", "align": "left"},
                {"name": "actions", "label": "", "field": "id", "align": "right"},
            ]
            tbl = ui.table(columns=columns, rows=rows, row_key="id").props("flat dense").classes("w-full")
            tbl.add_slot("body-cell-title", r'''
                <q-td :props="props">
                  <a :href="props.row.url" target="_blank" class="pg-link pg-ellipsis">{{ props.value }}</a>
                  <span v-if="!props.row.available" style="color:#ec835a; font-size:11px; margin-left:6px">немає в наявності</span>
                </q-td>''')
            tbl.add_slot("body-cell-status", r'''
                <q-td :props="props">
                  <span class="pg-badge" :class="{active:'good', paused:'warning', paused_limit:'warning', error:'critical', out_of_stock:'serious'}[props.value]">
                    <span class="dot"></span>{{ {active:'Активний', paused:'На паузі', paused_limit:'Пауза: ліміт', error:'Помилка', out_of_stock:'Немає'}[props.value] || props.value }}
                    <span v-if="props.row.errors" style="color:#898781">· {{ props.row.errors }} пом.</span>
                  </span>
                  <span style="font-size:11px; margin-left:6px; white-space:nowrap" :title="'Сповіщення: знижки ' + (props.row.notify_drop ? 'увімк.' : 'вимк.') + ', наявність ' + (props.row.notify_restock ? 'увімк.' : 'вимк.')">
                    📉{{ props.row.notify_drop ? '🔔' : '🔕' }} 📦{{ props.row.notify_restock ? '🔔' : '🔕' }}
                  </span>
                </q-td>''')
            tbl.add_slot("body-cell-actions", r'''
                <q-td :props="props">
                  <q-btn flat dense round icon="show_chart" size="sm" @click="$parent.$emit('history', props.row)"><q-tooltip>Історія цін</q-tooltip></q-btn>
                  <q-btn v-if="props.row.status !== 'active'" flat dense round icon="play_arrow" size="sm" color="positive" @click="$parent.$emit('activate', props.row)"><q-tooltip>Активувати</q-tooltip></q-btn>
                  <q-btn v-else flat dense round icon="pause" size="sm" color="warning" @click="$parent.$emit('pause', props.row)"><q-tooltip>На паузу</q-tooltip></q-btn>
                </q-td>''')

            async def show_history(e) -> None:
                row = e.args
                points = await q.tracker_history(row["id"])
                with ui.dialog() as dlg, ui.card().style("min-width: 720px; max-width: 95vw"):
                    ui.label(row["title"]).style("font-weight:600; font-size:15px")
                    ui.label(f"{row['source']} · {row['currency']} · {len(points)} записів").classes("pg-muted").style("font-size:12px")
                    if points:
                        price_history_chart(points, row["currency"], float(row["target"]) if row["target"] else None)
                        lo, hi = min(p[1] for p in points), max(p[1] for p in points)
                        with ui.row().classes("gap-6").style("font-size:13px"):
                            ui.label(f"Мінімум: {money(lo, row['currency'])}")
                            ui.label(f"Максимум: {money(hi, row['currency'])}")
                            ui.label(f"Зараз: {money(row['price'], row['currency'])}")
                    else:
                        ui.label("Історії ще немає").classes("pg-muted")
                    ui.button("Закрити", on_click=dlg.close).props("flat no-caps")
                dlg.open()

            async def set_status(e, status: TrackerStatus) -> None:
                row = e.args
                await q.set_tracker_status(row["id"], status)
                await auth.audit(f"tracker_{status}", row["id"], row["title"][:60])
                ui.notify("Трекер активовано" if status == TrackerStatus.ACTIVE else "Трекер на паузі", type="info")
                await refresh()

            tbl.on("history", show_history)
            tbl.on("activate", lambda e: set_status(e, TrackerStatus.ACTIVE))
            tbl.on("pause", lambda e: set_status(e, TrackerStatus.PAUSED))

    # ── Сповіщення ──
    with card("Останні сповіщення", f"{len(d['notifications'])} останніх"):
        if not d["notifications"]:
            ui.label("Сповіщень ще не було").classes("pg-muted").style("font-size:13px")
        else:
            names = {"decrease": "Зниження", "target_reached": "Ціль", "restock": "В наявності", "increase": "Підвищення"}
            rows = [{
                "when": n["when"], "event": names.get(n["event"], n["event"]),
                "title": n["title"],
                "change": f"{n['old']:g} → {n['new']:g}" if n["old"] is not None else f"{n['new']:g}",
                "status": "✓ надіслано" if n["status"] == "sent" else f"✗ {n['error'][:40]}",
            } for n in d["notifications"]]
            columns = [
                {"name": "when", "label": "Коли", "field": "when", "align": "left"},
                {"name": "event", "label": "Подія", "field": "event", "align": "left"},
                {"name": "title", "label": "Товар", "field": "title", "align": "left"},
                {"name": "change", "label": "Ціна", "field": "change", "align": "right"},
                {"name": "status", "label": "Статус", "field": "status", "align": "left"},
            ]
            ui.table(columns=columns, rows=rows, pagination=10).props('flat dense rows-per-page-label="Рядків:"').classes("w-full")
