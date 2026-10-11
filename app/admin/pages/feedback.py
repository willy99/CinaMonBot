"""Інбокс фідбеку з бота (/feedback): нові/прочитані/з відповіддю, перегляд медіа, відповідь юзеру."""
from __future__ import annotations

import asyncio
from html import escape
from urllib.parse import quote

from nicegui import ui

from app.admin import auth
from app.admin import queries as q
from app.admin.theme import MUTED, badge, card, kpi, layout, set_nav_badge
from app.i18n import resolve_lang, t

TYPE_LABELS = {
    "text": "📝 Текст", "photo": "🖼 Фото", "document": "📎 Файл", "video": "🎥 Відео",
    "voice": "🎤 Голос", "audio": "🎵 Аудіо", "sticker": "🙂 Стікер", "other": "📦 Інше",
}
STATUS_LABELS = {"new": ("Новий", "warning"), "read": ("Прочитано", ""), "replied": ("Відповіли", "good")}

_TYPE_JS = "{" + ", ".join(f"{k}:'{v}'" for k, v in TYPE_LABELS.items()) + "}"

USER_SLOT = r'''
<q-td :props="props">
  <div style="display:flex; flex-direction:column; line-height:1.25">
    <span style="font-weight:600">{{ props.row.name }}</span>
    <a v-if="props.row.username" :href="'https://t.me/' + props.row.username" target="_blank" class="pg-link" style="font-size:12px" @click.stop>@{{ props.row.username }}</a>
    <span v-else class="pg-mono" style="font-size:11.5px; color:#898781">{{ props.row.telegram_id }}</span>
  </div>
</q-td>'''

TYPE_SLOT = r'''
<q-td :props="props"><span class="pg-badge">''' + "{{ " + _TYPE_JS + r'''[props.value] || props.value }}</span></q-td>'''

TEXT_SLOT = r'''
<q-td :props="props">
  <span class="pg-ellipsis" :style="{color: props.value ? 'inherit' : '#898781'}">{{ props.value || (props.row.file_name ? props.row.file_name : '— без тексту —') }}</span>
</q-td>'''

STATUS_SLOT = r'''
<q-td :props="props">
  <span class="pg-badge" :class="{new:'warning', read:'', replied:'good'}[props.value]">
    <span class="dot"></span>{{ {new:'Новий', read:'Прочитано', replied:'Відповіли'}[props.value] || props.value }}
  </span>
</q-td>'''


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
    with layout("Фідбек", "/feedback"):
        state = {"status": "all", "search": ""}

        kpi_box = ui.element("div").classes("w-full grid gap-3").style(
            "grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));"
        )

        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            search = ui.input(placeholder="Пошук: ім'я, username, текст або Telegram ID") \
                .props("outlined dense clearable debounce=300").classes("flex-grow").style("max-width: 420px")
            with search.add_slot("prepend"):
                ui.icon("search")
            status_toggle = ui.toggle({"new": "Нові", "all": "Усі", "replied": "З відповіддю"}, value="all") \
                .props("no-caps unelevated dense toggle-color=primary")
            ui.space()
            count_label = ui.label().classes("pg-muted").style("font-size:13px; white-space:nowrap")

        columns = [
            {"name": "when", "label": "Коли", "field": "when", "align": "left", "sortable": True,
             "sort": "(a, b, ra, rb) => ra.created_sort < rb.created_sort ? -1 : 1"},
            {"name": "user", "label": "Користувач", "field": "name", "align": "left", "sortable": True},
            {"name": "lang", "label": "Мова", "field": "lang", "align": "left"},
            {"name": "content_type", "label": "Тип", "field": "content_type", "align": "left"},
            {"name": "text", "label": "Текст", "field": "text", "align": "left"},
            {"name": "status", "label": "Статус", "field": "status", "align": "left", "sortable": True},
        ]
        with card():
            table = ui.table(columns=columns, rows=[], row_key="id", pagination=25) \
                .props('flat rows-per-page-label="Рядків:"').classes("w-full")
            table.add_slot("body-cell-user", USER_SLOT)
            table.add_slot("body-cell-content_type", TYPE_SLOT)
            table.add_slot("body-cell-text", TEXT_SLOT)
            table.add_slot("body-cell-status", STATUS_SLOT)
            table.add_slot("no-data", '<div class="full-width row flex-center q-pa-md" style="color:#898781">Фідбеку ще немає</div>')

        async def render_kpis() -> None:
            counts = await q.feedback_counts()
            set_nav_badge("/feedback", counts["new"])
            kpi_box.clear()
            with kpi_box:
                kpi("Нові", counts["new"], icon="mark_email_unread")
                kpi("Прочитані", counts["read"], icon="drafts")
                kpi("Відповіли", counts["replied"], icon="reply")
                kpi("Всього", counts["total"], icon="forum")

        async def reload() -> None:
            rows = await q.feedback_list(state["status"], state["search"])
            table.rows = rows
            table.update()
            count_label.text = f"{len(rows)} повідомлень"
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
    """Картка одного фідбеку: текст, медіа, дії, історія юзера. Відкриття позначає новий як прочитаний."""
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
                    text, kind = STATUS_LABELS.get(d["status"], (d["status"], ""))
                    badge(text, kind)
                    badge(TYPE_LABELS.get(d["content_type"], d["content_type"]))
                with ui.row().classes("items-center gap-4").style(f"font-size:13px; color:{MUTED}"):
                    ui.label(f"Telegram ID: {d['telegram_id']}").classes("pg-mono")
                    ui.label(f"Мова: {d['lang']}")
                    ui.label(d["when"])
                    if d["user_id"]:
                        ui.link("Картка користувача →", f"/users/{d['user_id']}").classes("pg-link")
                    if not d["forwarded"] and d["content_type"] != "text":
                        ui.label("· копія адміну в Telegram не дійшла").style("color:#ec835a")
            ui.button(icon="close", on_click=dlg.close).props("flat round dense")

        # ── Текст ──
        if d["text"]:
            ui.label(d["text"]).style(
                "white-space:pre-wrap; word-break:break-word; font-size:14.5px; line-height:1.45; "
                "background: var(--surface-2); border-radius: 10px; padding: 12px 14px; width:100%"
            )
        else:
            ui.label("Без тексту").classes("pg-muted").style("font-size:13px")

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
                label = d["file_name"] or {"video": "відео", "voice": "голосове", "audio": "аудіо"}.get(ctype, "файл")
                with ui.row().classes("items-center gap-2"):
                    ui.icon("download", size="18px").style(f"color:{MUTED}")
                    ui.link(f"Завантажити {label}", url, new_tab=True).classes("pg-link")
                    if d["mime_type"]:
                        ui.label(d["mime_type"]).classes("pg-mono").style("font-size:11.5px")
        elif d["content_type"] != "text":
            ui.label("Медіа без file_id — дивись копію в чаті адміна").classes("pg-muted").style("font-size:13px")

        # ── Відповідь (уже надіслана) ──
        if d["reply_text"]:
            with ui.column().classes("w-full gap-1"):
                ui.label(f"Відповідь · {d['replied_by']} · {d['replied_at']}").classes("pg-muted").style("font-size:12px")
                ui.label(d["reply_text"]).style(
                    "white-space:pre-wrap; font-size:13.5px; border-left: 3px solid var(--good); padding: 4px 10px"
                )

        # ── Дії ──
        reply_box = ui.textarea(placeholder="Текст відповіді користувачу (бот надішле його в Telegram)") \
            .props("outlined dense autogrow").classes("w-full")

        async def send_reply() -> None:
            text = (reply_box.value or "").strip()
            if not text:
                ui.notify("Введіть текст відповіді", type="warning")
                return
            reply_btn.props("loading")
            try:
                ok = await auth.send_telegram_message(
                    d["telegram_id"], t(lang, "feedback_reply_header", text=escape(text))
                )
            finally:
                reply_btn.props(remove="loading")
            if not ok:
                ui.notify("Не вдалося надіслати в Telegram (юзер заблокував бота?)", type="negative")
                return
            who = (auth.current_admin() or {}).get("username", "admin")
            await q.feedback_reply_saved(d["id"], text, who)
            await auth.audit("feedback_reply", str(d["telegram_id"]), text[:80])
            ui.notify("Відповідь надіслано", type="positive")
            dlg.close()
            await refresh()

        async def mark(status: str) -> None:
            await q.feedback_mark(d["id"], status)
            ui.notify("Позначено як прочитане" if status == "read" else "Позначено як нове", type="info")
            dlg.close()
            await refresh()

        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            reply_btn = ui.button("Відповісти", icon="send", on_click=send_reply).props("unelevated no-caps")
            if d["status"] == "new":
                ui.button("Позначити прочитаним", icon="drafts", on_click=lambda: mark("read")).props("outline no-caps")
            elif d["status"] == "read":
                ui.button("Позначити непрочитаним", icon="mark_email_unread", on_click=lambda: mark("new")).props("outline no-caps")
            ui.space()
            ui.button("Закрити", on_click=dlg.close).props("flat no-caps")

        # ── Попередні фідбеки юзера ──
        with card("Попередні повідомлення", f"{len(history)} від цього користувача"):
            if not history:
                ui.label("Це перше повідомлення").classes("pg-muted").style("font-size:13px")
            else:
                cols = [
                    {"name": "when", "label": "Коли", "field": "when", "align": "left"},
                    {"name": "content_type", "label": "Тип", "field": "content_type", "align": "left"},
                    {"name": "text", "label": "Текст", "field": "text", "align": "left"},
                    {"name": "status", "label": "Статус", "field": "status", "align": "left"},
                ]
                tbl = ui.table(columns=cols, rows=history, row_key="id", pagination=5) \
                    .props('flat dense rows-per-page-label="Рядків:"').classes("w-full")
                tbl.add_slot("body-cell-content_type", TYPE_SLOT)
                tbl.add_slot("body-cell-text", TEXT_SLOT)
                tbl.add_slot("body-cell-status", STATUS_SLOT)
    dlg.open()
