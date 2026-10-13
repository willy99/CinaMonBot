"""Список користувачів з пошуком і фільтрами + картка користувача з діями."""
from __future__ import annotations

import asyncio
import json

from nicegui import ui

from app.admin import auth
from app.admin import queries as q
from app.admin.i18n import adate, at, event_label, slot_labels
from app.admin.theme import (
    TRACKER_STATUS_KEYS, TRACKER_STATUS_KINDS, badge, card, layout, money, price_history_chart, slot_classes,
    target_label,
)
from app.database.models import TrackerStatus


def _js(key: str, **kw) -> str:
    """Переклад як JS-рядок у лапках для вставки у Vue-шаблон."""
    return json.dumps(at(key, **kw), ensure_ascii=False)


# Підписи в слотах — не літерали: підставляються з at() при рендері (мова береться з сесії)
TIER_BADGE_SLOT = r'''
<q-td :props="props">
  <span class="pg-badge" :class="props.row.blocked ? 'critical' : props.value === 'premium' ? 'premium' : ''">
    <span class="dot"></span>
    {{ props.row.blocked ? __BLOCKED__ : props.value === 'premium' ? __PREMIUM_UNTIL__ : 'Free' }}
  </span>
</q-td>'''


def _tier_badge_slot() -> str:
    # "Premium · до {date}" — дата приходить з рядка таблиці, тому плейсхолдер ділимо на JS-конкатенацію
    premium = at("users_premium_until", date="\x00").split("\x00")
    premium_js = " + ".join(filter(None, [json.dumps(premium[0], ensure_ascii=False), "props.row.until",
                                          json.dumps(premium[1], ensure_ascii=False) if premium[1] else ""]))
    return TIER_BADGE_SLOT.replace("__BLOCKED__", _js("users_blocked")).replace("__PREMIUM_UNTIL__", premium_js)


def _tracker_status_slot() -> str:
    return r'''
<q-td :props="props">
  <span class="pg-badge" :class="__KINDS__[props.value]">
    <span class="dot"></span>{{ __LABELS__[props.value] || props.value }}
    <span v-if="props.row.errors" class="pg-muted">· __ERRORS__</span>
  </span>
  <span style="font-size:11px; margin-left:6px; white-space:nowrap" :title="props.row.notif_title">
    📉{{ props.row.notify_drop ? '🔔' : '🔕' }} 📦{{ props.row.notify_restock ? '🔔' : '🔕' }}
  </span>
</q-td>'''.replace("__KINDS__", slot_classes(TRACKER_STATUS_KINDS)) \
        .replace("__LABELS__", slot_labels(TRACKER_STATUS_KEYS)) \
        .replace("__ERRORS__", at("errors_n", n="{{ props.row.errors }}"))


def _no_data_slot(text: str) -> str:
    return f'<div class="full-width row flex-center q-pa-md pg-muted">{text}</div>'


def _rows_label() -> str:
    return f'rows-per-page-label="{at("rows_per_page")}"'


@ui.page("/users")
async def users_page() -> None:
    with layout(at("nav_users"), "/users"):
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
            search = ui.input(placeholder=at("users_search")) \
                .props("outlined dense clearable debounce=300").classes("flex-grow").style("max-width: 420px")
            with search.add_slot("prepend"):
                ui.icon("search")
            tier = ui.toggle({"all": at("filter_all"), "premium": "Premium", "free": "Free"}, value=state["tier"]) \
                .props("no-caps unelevated dense toggle-color=primary")
            status = ui.toggle({"all": at("users_status_any"), "active": at("filter_active"),
                                "blocked": at("users_status_blocked")}, value=state["status"]) \
                .props("no-caps unelevated dense toggle-color=primary")
            ui.space()
            count_label = ui.label().classes("pg-muted").style("font-size:13px; white-space:nowrap")

        columns = [
            {"name": "name", "label": at("col_name"), "field": "name", "align": "left", "sortable": True},
            {"name": "username", "label": at("col_username"), "field": "username", "align": "left"},
            {"name": "telegram_id", "label": "Telegram ID", "field": "telegram_id", "align": "left", "classes": "pg-mono"},
            {"name": "lang", "label": at("col_lang"), "field": "lang", "align": "left"},
            {"name": "tier", "label": at("users_col_tier"), "field": "tier", "align": "left", "sortable": True},
            {"name": "trackers", "label": at("users_col_trackers"), "field": "trackers", "align": "right", "sortable": True},
            {"name": "created", "label": at("users_col_registered"), "field": "created", "align": "left",
             "sortable": True, "sort": "(a, b, ra, rb) => ra.created_sort < rb.created_sort ? -1 : 1"},
        ]
        with card():
            table = ui.table(columns=columns, rows=[], row_key="id", pagination=25) \
                .props(f"flat {_rows_label()}").classes("w-full")
            table.add_slot("body-cell-tier", _tier_badge_slot())
            table.add_slot("body-cell-trackers", r'''
                <q-td :props="props">{{ props.value }} <span class="pg-muted">__ACTIVE__</span></q-td>'''
                .replace("__ACTIVE__", at("active_short", n="{{ props.row.trackers_active }}")))
            table.add_slot("no-data", _no_data_slot(at("users_empty")))
            table.on("rowClick", lambda e: ui.navigate.to(f"/users/{e.args[1]['id']}"))

        async def reload() -> None:
            rows = await q.users_list(state["search"], state["tier"], state["status"])
            table.rows = rows
            table.update()
            count_label.text = at("users_count", n=len(rows))

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
    with layout(at("users_one_title"), "/users"):
        data = await q.user_detail(user_id)
        if not data:
            ui.label(at("users_not_found")).classes("pg-muted")
            ui.link(at("users_back_to_list"), "/users").classes("pg-link")
            return

        content = ui.column().classes("w-full gap-5")

        async def refresh() -> None:
            fresh = await q.user_detail(user_id)
            content.clear()
            with content:
                _render_user(fresh, refresh)

        with content:
            _render_user(data, refresh)


def _title(raw: str) -> str:
    """queries віддає український fallback "Без назви" — показуємо його мовою адмінки."""
    return at("untitled") if raw == "Без назви" else raw


def _render_user(d: dict, refresh) -> None:
    ui.link(at("users_back_all"), "/users").classes("pg-link").style("font-size:13px")

    # ── Шапка ──
    with card():
        with ui.row().classes("w-full items-start justify-between no-wrap gap-4"):
            with ui.column().classes("gap-1"):
                with ui.row().classes("items-center gap-3"):
                    ui.label(d["name"]).style("font-size:22px; font-weight:700")
                    if d["username"]:
                        ui.link(f"@{d['username']}", f"https://t.me/{d['username']}", new_tab=True).classes("pg-link")
                    if d["blocked"]:
                        badge(at("users_blocked"), "critical")
                    elif d["tier"] == "premium":
                        badge(at("users_premium_until", date=adate(d["premium_until"])), "premium")
                    else:
                        badge("Free")
                with ui.row().classes("items-center gap-4 pg-muted").style("font-size:13px"):
                    ui.label(at("users_telegram_id", id=d["telegram_id"])).classes("pg-mono")
                    ui.label(at("users_lang", lang=d["lang"]))
                    ui.label(at("users_digest", state=at("enabled") if d.get("digest_enabled", True) else at("disabled")))
                    ui.label(at("users_registered", date=d["created"]))
                    ui.label(at("users_items", n=len(d["trackers"])))

            with ui.row().classes("items-center gap-2 no-wrap"):
                days = ui.number(value=30, min=1, max=3650, step=1) \
                    .props(f'outlined dense suffix="{at("users_days_suffix")}"').style("width: 110px")

                async def activate() -> None:
                    n = int(days.value or 30)
                    until = await q.set_premium(d["id"], n)
                    await auth.audit("premium_activate", str(d["telegram_id"]), f"+{n} днів → {until:%d.%m.%Y}")
                    ui.notify(at("users_premium_activated", date=adate(until)), type="positive")
                    await refresh()

                async def revoke() -> None:
                    await q.revoke_premium(d["id"])
                    await auth.audit("premium_revoke", str(d["telegram_id"]))
                    ui.notify(at("users_premium_revoked"), type="warning")
                    await refresh()

                async def toggle_block() -> None:
                    await q.set_blocked(d["id"], not d["blocked"])
                    await auth.audit("user_unblock" if d["blocked"] else "user_block", str(d["telegram_id"]))
                    ui.notify(at("users_unblocked") if d["blocked"] else at("users_blocked_done"), type="info")
                    await refresh()

                ui.button(at("users_btn_activate_premium"), icon="workspace_premium", on_click=activate).props("unelevated no-caps")
                if d["tier"] == "premium":
                    ui.button(at("btn_deactivate"), icon="remove_circle_outline", on_click=revoke).props("outline no-caps color=warning")
                ui.button(at("users_btn_unblock") if d["blocked"] else at("users_btn_block"),
                          icon="lock_open" if d["blocked"] else "block", on_click=toggle_block) \
                    .props(f"outline no-caps color={'positive' if d['blocked'] else 'negative'}")

    # ── Трекери ──
    with card(at("users_card_trackers"), at("users_total_n", n=len(d["trackers"]))):
        if not d["trackers"]:
            ui.label(at("users_trackers_empty")).classes("pg-muted").style("font-size:13px")
        else:
            on, off = at("on_short"), at("off_short")
            rows = [{
                **t,
                "title": _title(t["title"]),
                "price_str": money(t["price"], t["currency"]),
                "target_str": target_label(t["target"], t["currency"], t.get("target_percent")),
                "notif_title": at("users_notif_title", drop=on if t["notify_drop"] else off,
                                  restock=on if t["notify_restock"] else off),
            } for t in d["trackers"]]
            columns = [
                {"name": "title", "label": at("col_product"), "field": "title", "align": "left"},
                {"name": "source", "label": at("col_site"), "field": "source", "align": "left"},
                {"name": "price_str", "label": at("col_price"), "field": "price_str", "align": "right"},
                {"name": "target_str", "label": at("col_target"), "field": "target_str", "align": "right"},
                {"name": "status", "label": at("col_status"), "field": "status", "align": "left"},
                {"name": "last_checked", "label": at("col_checked"), "field": "last_checked", "align": "left"},
                {"name": "actions", "label": "", "field": "id", "align": "right"},
            ]
            tbl = ui.table(columns=columns, rows=rows, row_key="id").props("flat dense").classes("w-full")
            tbl.add_slot("body-cell-title", r'''
                <q-td :props="props">
                  <a :href="props.row.url" target="_blank" class="pg-link pg-ellipsis">{{ props.value }}</a>
                  <span v-if="!props.row.available" style="color:var(--serious); font-size:11px; margin-left:6px">__OUT__</span>
                </q-td>'''.replace("__OUT__", at("users_out_of_stock_hint")))
            tbl.add_slot("body-cell-status", _tracker_status_slot())
            tbl.add_slot("body-cell-actions", r'''
                <q-td :props="props">
                  <q-btn flat dense round icon="show_chart" size="sm" @click="$parent.$emit('history', props.row)"><q-tooltip>__HISTORY__</q-tooltip></q-btn>
                  <q-btn v-if="props.row.status !== 'active'" flat dense round icon="play_arrow" size="sm" color="positive" @click="$parent.$emit('activate', props.row)"><q-tooltip>__ACTIVATE__</q-tooltip></q-btn>
                  <q-btn v-else flat dense round icon="pause" size="sm" color="warning" @click="$parent.$emit('pause', props.row)"><q-tooltip>__PAUSE__</q-tooltip></q-btn>
                </q-td>'''.replace("__HISTORY__", at("users_tip_history")).replace("__ACTIVATE__", at("btn_activate"))
                .replace("__PAUSE__", at("users_tip_pause")))

            async def show_history(e) -> None:
                row = e.args
                points = await q.tracker_history(row["id"])
                with ui.dialog() as dlg, ui.card().style("min-width: 720px; max-width: 95vw"):
                    ui.label(row["title"]).style("font-weight:600; font-size:15px")
                    ui.label(at("users_history_sub", source=row["source"], currency=row["currency"], n=len(points))) \
                        .classes("pg-muted").style("font-size:12px")
                    if points:
                        price_history_chart(points, row["currency"], float(row["target"]) if row["target"] else None)
                        lo, hi = min(p[1] for p in points), max(p[1] for p in points)
                        with ui.row().classes("gap-6").style("font-size:13px"):
                            ui.label(at("users_min", v=money(lo, row["currency"])))
                            ui.label(at("users_max", v=money(hi, row["currency"])))
                            ui.label(at("users_now", v=money(row["price"], row["currency"])))
                    else:
                        ui.label(at("users_history_empty")).classes("pg-muted")
                    ui.button(at("btn_close"), on_click=dlg.close).props("flat no-caps")
                dlg.open()

            async def set_status(e, status: TrackerStatus) -> None:
                row = e.args
                await q.set_tracker_status(row["id"], status)
                await auth.audit(f"tracker_{status}", row["id"], row["title"][:60])
                ui.notify(at("users_tracker_activated") if status == TrackerStatus.ACTIVE else at("users_tracker_paused"),
                          type="info")
                await refresh()

            tbl.on("history", show_history)
            tbl.on("activate", lambda e: set_status(e, TrackerStatus.ACTIVE))
            tbl.on("pause", lambda e: set_status(e, TrackerStatus.PAUSED))

    # ── Сповіщення ──
    with card(at("users_card_notifs"), at("last_n", n=len(d["notifications"]))):
        if not d["notifications"]:
            ui.label(at("users_notifs_empty")).classes("pg-muted").style("font-size:13px")
        else:
            rows = [{
                "when": n["when"], "event": event_label(n["event"]),
                "title": _title(n["title"]),
                "change": f"{n['old']:g} → {n['new']:g}" if n["old"] is not None else f"{n['new']:g}",
                "status": at("users_notif_sent") if n["status"] == "sent" else f"✗ {n['error'][:40]}",
            } for n in d["notifications"]]
            columns = [
                {"name": "when", "label": at("col_when"), "field": "when", "align": "left"},
                {"name": "event", "label": at("col_event"), "field": "event", "align": "left"},
                {"name": "title", "label": at("col_product"), "field": "title", "align": "left"},
                {"name": "change", "label": at("col_price"), "field": "change", "align": "right"},
                {"name": "status", "label": at("col_status"), "field": "status", "align": "left"},
            ]
            ui.table(columns=columns, rows=rows, pagination=10).props(f"flat dense {_rows_label()}").classes("w-full")
