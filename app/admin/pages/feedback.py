"""Інбокс фідбеку з бота (/feedback): нові/прочитані/з відповіддю, перегляд медіа, відповідь юзеру."""
from __future__ import annotations

import asyncio
import json
from html import escape
from urllib.parse import quote

from nicegui import ui

from botkit.admin import auth
from app.admin import queries as q
from app.admin.i18n import at, slot_labels
from app.admin.theme import badge, card, kpi, layout, set_nav_badge, slot_classes
from app.i18n import resolve_lang, t

# тип вмісту / статус → ключ i18n; підписи беруться через at() (Python) або slot_labels() (Vue)
TYPE_KEYS = {
    "text": "feedback_type_text", "photo": "feedback_type_photo", "document": "feedback_type_document",
    "video": "feedback_type_video", "voice": "feedback_type_voice", "audio": "feedback_type_audio",
    "sticker": "feedback_type_sticker", "other": "feedback_type_other",
}
STATUS_KEYS = {"new": "feedback_status_new", "read": "feedback_status_read", "replied": "feedback_status_replied"}
STATUS_KIND = {"new": "warning", "read": "", "replied": "good"}

USER_SLOT = r'''
<q-td :props="props">
  <div style="display:flex; flex-direction:column; line-height:1.25">
    <span style="font-weight:600">{{ props.row.name }}</span>
    <a v-if="props.row.username" :href="'https://t.me/' + props.row.username" target="_blank" class="pg-link" style="font-size:12px" @click.stop>@{{ props.row.username }}</a>
    <span v-else class="pg-mono pg-muted" style="font-size:11.5px">{{ props.row.telegram_id }}</span>
  </div>
</q-td>'''

TYPE_SLOT = r'''
<q-td :props="props"><span class="pg-badge">{{ __LABELS__[props.value] || props.value }}</span></q-td>'''

TEXT_SLOT = r'''
<q-td :props="props">
  <span class="pg-ellipsis" :class="{'pg-muted': !props.value}">{{ props.value || (props.row.file_name ? props.row.file_name : __NO_TEXT__) }}</span>
</q-td>'''

STATUS_SLOT = r'''
<q-td :props="props">
  <span class="pg-badge" :class="__KINDS__[props.value]">
    <span class="dot"></span>{{ __LABELS__[props.value] || props.value }}
  </span>
</q-td>'''


def _type_slot() -> str:
    return TYPE_SLOT.replace("__LABELS__", slot_labels(TYPE_KEYS))


def _text_slot() -> str:
    return TEXT_SLOT.replace("__NO_TEXT__", json.dumps(at("feedback_no_text_dash"), ensure_ascii=False))


def _status_slot() -> str:
    return STATUS_SLOT.replace("__KINDS__", slot_classes(STATUS_KIND)).replace("__LABELS__", slot_labels(STATUS_KEYS))


def type_label(code: str) -> str:
    key = TYPE_KEYS.get(code)
    return at(key) if key else code


def status_label(code: str) -> str:
    key = STATUS_KEYS.get(code)
    return at(key) if key else code


def media_url(row: dict) -> str | None:
    """Посилання на /media/<file_id> (проксі дашборду, токен у браузер не потрапляє)."""
    if not row.get("file_id"):
        return None
    url = f"/media/{row['file_id']}"
    if row.get("file_name"):
        url += f"?name={quote(row['file_name'])}"
    return url


@ui.page("/feedback")
async def feedback_page() -> None:
    with layout(at("nav_feedback"), "/feedback"):
        state = {"status": "all", "search": ""}

        kpi_box = ui.element("div").classes("w-full grid gap-3").style(
            "grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));"
        )

        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            search = ui.input(placeholder=at("feedback_search")) \
                .props("outlined dense clearable debounce=300").classes("flex-grow").style("max-width: 420px")
            with search.add_slot("prepend"):
                ui.icon("search")
            status_toggle = ui.toggle({"new": at("feedback_filter_new"), "all": at("filter_all"),
                                       "replied": at("feedback_filter_replied")}, value="all") \
                .props("no-caps unelevated dense toggle-color=primary")
            ui.space()
            count_label = ui.label().classes("pg-muted").style("font-size:13px; white-space:nowrap")

        columns = [
            {"name": "when", "label": at("col_when"), "field": "when", "align": "left", "sortable": True,
             "sort": "(a, b, ra, rb) => ra.created_sort < rb.created_sort ? -1 : 1"},
            {"name": "user", "label": at("col_user"), "field": "name", "align": "left", "sortable": True},
            {"name": "lang", "label": at("col_lang"), "field": "lang", "align": "left"},
            {"name": "content_type", "label": at("col_type"), "field": "content_type", "align": "left"},
            {"name": "text", "label": at("col_text"), "field": "text", "align": "left"},
            {"name": "status", "label": at("col_status"), "field": "status", "align": "left", "sortable": True},
        ]
        with card():
            table = ui.table(columns=columns, rows=[], row_key="id", pagination=25) \
                .props(f'flat rows-per-page-label="{at("rows_per_page")}"').classes("w-full")
            table.add_slot("body-cell-user", USER_SLOT)
            table.add_slot("body-cell-content_type", _type_slot())
            table.add_slot("body-cell-text", _text_slot())
            table.add_slot("body-cell-status", _status_slot())
            table.add_slot("no-data", f'<div class="full-width row flex-center q-pa-md pg-muted">{at("feedback_empty")}</div>')

        async def render_kpis() -> None:
            counts = await q.feedback_counts()
            set_nav_badge("/feedback", counts["new"])
            kpi_box.clear()
            with kpi_box:
                kpi(at("feedback_kpi_new"), counts["new"], icon="mark_email_unread")
                kpi(at("feedback_kpi_read"), counts["read"], icon="drafts")
                kpi(at("feedback_kpi_replied"), counts["replied"], icon="reply")
                kpi(at("feedback_kpi_total"), counts["total"], icon="forum")

        async def reload() -> None:
            rows = await q.feedback_list(state["status"], state["search"])
            table.rows = rows
            table.update()
            count_label.text = at("feedback_count", n=len(rows))
            await render_kpis()

        def on_search(e):
            state["search"] = e.value or ""
            asyncio.create_task(reload())

        def on_status(e):
            state["status"] = e.value or "all"
            asyncio.create_task(reload())

        async def on_row(e) -> None:
            row = e.args[1]
            fresh = await q.feedback_get(row["id"])
            if fresh:
                await open_feedback_dialog(fresh, reload)

        search.on_value_change(on_search)
        status_toggle.on_value_change(on_status)
        table.on("rowClick", on_row)
        await reload()


async def open_feedback_dialog(d: dict, refresh) -> None:
    """Картка одного фідбеку: текст, медіа, дії, історія юзера. Відкриття позначає новий як прочитаний.
    Текст відповіді юзеру йде через t() бота (мовою юзера); все інше тут — мовою адмінки (at)."""
    if d["status"] == "new":
        await q.feedback_mark(d["id"], "read")
        d["status"] = "read"
        await refresh()

    history = [h for h in await q.feedback_user_history(d["telegram_id"]) if h["id"] != d["id"]]
    lang = resolve_lang(d["lang"] if d["lang"] != "—" else None)

    with ui.dialog() as dlg, ui.card().style("min-width: 640px; max-width: 95vw; gap: 14px"):
        # ── Шапка ──
        with ui.row().classes("w-full items-start justify-between no-wrap gap-4"):
            with ui.column().classes("gap-1"):
                with ui.row().classes("items-center gap-3"):
                    ui.label(d["name"]).style("font-size:18px; font-weight:700")
                    if d["username"]:
                        ui.link(f"@{d['username']}", f"https://t.me/{d['username']}", new_tab=True).classes("pg-link")
                    badge(status_label(d["status"]), STATUS_KIND.get(d["status"], ""))
                    badge(type_label(d["content_type"]))
                with ui.row().classes("items-center gap-4 pg-muted").style("font-size:13px"):
                    ui.label(at("users_telegram_id", id=d["telegram_id"])).classes("pg-mono")
                    ui.label(at("users_lang", lang=d["lang"]))
                    ui.label(d["when"])
                    if d["user_id"]:
                        ui.link(at("feedback_user_card"), f"/users/{d['user_id']}").classes("pg-link")
                    if not d["forwarded"] and d["content_type"] != "text":
                        ui.label(at("feedback_not_forwarded")).style("color:var(--serious)")
            ui.button(icon="close", on_click=dlg.close).props("flat round dense")

        # ── Текст ──
        if d["text"]:
            ui.label(d["text"]).style(
                "white-space:pre-wrap; word-break:break-word; font-size:14.5px; line-height:1.45; "
                "background: var(--surface-2); border-radius: 10px; padding: 12px 14px; width:100%"
            )
        else:
            ui.label(at("feedback_no_text")).classes("pg-muted").style("font-size:13px")

        # ── Медіа ──
        url = media_url(d)
        if url:
            ctype = d["content_type"]
            if ctype in ("photo", "sticker"):
                ui.html(
                    f'<a href="{escape(url)}" target="_blank"><img src="{escape(url)}" alt="{ctype}" '
                    f'style="max-width:100%; max-height:420px; border-radius:10px; border:1px solid var(--border)"></a>'
                ).classes("w-full")
            else:
                media_key = {"video": "feedback_media_video", "voice": "feedback_media_voice",
                             "audio": "feedback_media_audio"}.get(ctype, "feedback_media_file")
                label = d["file_name"] or at(media_key)
                with ui.row().classes("items-center gap-2"):
                    ui.icon("download", size="18px").classes("pg-muted")
                    ui.link(at("feedback_download", what=label), url, new_tab=True).classes("pg-link")
                    if d["mime_type"]:
                        ui.label(d["mime_type"]).classes("pg-mono").style("font-size:11.5px")
        elif d["content_type"] != "text":
            ui.label(at("feedback_media_no_file_id")).classes("pg-muted").style("font-size:13px")

        # ── Відповідь (уже надіслана) ──
        if d["reply_text"]:
            with ui.column().classes("w-full gap-1"):
                ui.label(at("feedback_reply_meta", by=d["replied_by"], when=d["replied_at"])).classes("pg-muted") \
                    .style("font-size:12px")
                ui.label(d["reply_text"]).style(
                    "white-space:pre-wrap; font-size:13.5px; border-left: 3px solid var(--good); padding: 4px 10px"
                )

        # ── Дії ──
        reply_box = ui.textarea(placeholder=at("feedback_reply_placeholder")) \
            .props("outlined dense autogrow").classes("w-full")

        async def send_reply() -> None:
            text = (reply_box.value or "").strip()
            if not text:
                ui.notify(at("feedback_reply_empty"), type="warning")
                return
            reply_btn.props("loading")
            try:
                ok = await auth.send_telegram_message(
                    d["telegram_id"], t(lang, "feedback_reply_header", text=escape(text))
                )
            finally:
                reply_btn.props(remove="loading")
            if not ok:
                ui.notify(at("feedback_send_failed"), type="negative")
                return
            who = (auth.current_admin() or {}).get("username", "admin")
            await q.feedback_reply_saved(d["id"], text, who)
            await auth.audit("feedback_reply", str(d["telegram_id"]), text[:80])
            ui.notify(at("feedback_reply_sent"), type="positive")
            dlg.close()
            await refresh()

        async def mark(status: str) -> None:
            await q.feedback_mark(d["id"], status)
            ui.notify(at("feedback_marked_read") if status == "read" else at("feedback_marked_new"), type="info")
            dlg.close()
            await refresh()

        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            reply_btn = ui.button(at("feedback_btn_reply"), icon="send", on_click=send_reply).props("unelevated no-caps")
            if d["status"] == "new":
                ui.button(at("feedback_btn_mark_read"), icon="drafts", on_click=lambda: mark("read")).props("outline no-caps")
            elif d["status"] == "read":
                ui.button(at("feedback_btn_mark_unread"), icon="mark_email_unread", on_click=lambda: mark("new")) \
                    .props("outline no-caps")
            ui.space()
            ui.button(at("btn_close"), on_click=dlg.close).props("flat no-caps")

        # ── Попередні фідбеки юзера ──
        with card(at("feedback_history_title"), at("feedback_history_sub", n=len(history))):
            if not history:
                ui.label(at("feedback_history_empty")).classes("pg-muted").style("font-size:13px")
            else:
                cols = [
                    {"name": "when", "label": at("col_when"), "field": "when", "align": "left"},
                    {"name": "content_type", "label": at("col_type"), "field": "content_type", "align": "left"},
                    {"name": "text", "label": at("col_text"), "field": "text", "align": "left"},
                    {"name": "status", "label": at("col_status"), "field": "status", "align": "left"},
                ]
                tbl = ui.table(columns=cols, rows=history, row_key="id", pagination=5) \
                    .props(f'flat dense rows-per-page-label="{at("rows_per_page")}"').classes("w-full")
                tbl.add_slot("body-cell-content_type", _type_slot())
                tbl.add_slot("body-cell-text", _text_slot())
                tbl.add_slot("body-cell-status", _status_slot())
    dlg.open()
