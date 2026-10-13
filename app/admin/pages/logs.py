"""
Журнали з серверним пейджингом: /notifications (сповіщення юзерам) і /checks (перевірки цін).

Відкриваються кліком по картках/плитках дашборду. Показуємо по 25 від найновіших,
фільтри застосовуються на сервері, пейджер — « ‹ стор. N з M › ».
Параметри в URL (?page=2&event=decrease) — щоб посилання можна було передати.
"""
from __future__ import annotations

import asyncio
import json
import math
from typing import Awaitable, Callable

from nicegui import ui

from app.admin import queries as q
from app.admin.i18n import at, event_label
from app.admin.theme import card, layout, money

PAGE_SIZE = q.PAGE_SIZE


def _query_params() -> dict[str, str]:
    try:
        return dict(ui.context.client.request.query_params)
    except Exception:  # noqa: BLE001
        return {}


class Pager:
    """Кнопки пейджингу + підпис. on_change(page) викликається після кліку."""

    def __init__(self, on_change: Callable[[int], Awaitable[None]]) -> None:
        self.page = 1
        self.total = 0
        self._on_change = on_change
        with ui.row().classes("w-full items-center justify-between no-wrap").style("margin-top: 10px;"):
            self.info = ui.label().classes("pg-muted").style("font-size:13px")
            with ui.element("div").classes("pg-pager"):
                self.first = ui.button(icon="first_page", on_click=lambda: self.go(1)).props("flat dense round size=sm")
                self.prev = ui.button(icon="chevron_left", on_click=lambda: self.go(self.page - 1)).props("flat dense round size=sm")
                self.label = ui.label()
                self.next = ui.button(icon="chevron_right", on_click=lambda: self.go(self.page + 1)).props("flat dense round size=sm")
                self.last = ui.button(icon="last_page", on_click=lambda: self.go(self.pages)).props("flat dense round size=sm")

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / PAGE_SIZE))

    async def go(self, page: int) -> None:
        page = min(max(1, page), self.pages)
        if page == self.page:
            return
        self.page = page
        await self._on_change(page)

    def render(self, shown: int) -> None:
        start = (self.page - 1) * PAGE_SIZE + 1 if self.total else 0
        end = start + shown - 1 if shown else 0
        self.info.text = at("pager_shown", start=start, end=end, total=self.total)
        self.label.text = at("pager_page", page=self.page, pages=self.pages)
        for b, disabled in ((self.first, self.page <= 1), (self.prev, self.page <= 1),
                            (self.next, self.page >= self.pages), (self.last, self.page >= self.pages)):
            if disabled:
                b.props("disable")
            else:
                b.props(remove="disable")


def _no_data_slot(text: str) -> str:
    return f'<div class="full-width row flex-center q-pa-md pg-muted">{text}</div>'


# ─── /notifications ──────────────────────────────────────────────

# Підписи у слотах — не літерали: підставляються з at() при рендері (див. _notif_status_slot)
NOTIF_STATUS_SLOT = r'''
<q-td :props="props">
  <span class="pg-badge" :class="props.value === 'sent' ? 'good' : 'critical'">
    <span class="dot"></span>{{ props.value === 'sent' ? __SENT__ : __FAILED__ }}
  </span>
  <div v-if="props.row.error" class="pg-sub" style="max-width:260px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap">{{ props.row.error }}</div>
</q-td>'''


def _notif_status_slot() -> str:
    return NOTIF_STATUS_SLOT.replace("__SENT__", json.dumps(at("notif_sent"), ensure_ascii=False)) \
        .replace("__FAILED__", json.dumps(at("notif_failed"), ensure_ascii=False))


# Підпис події (props.row.event_label) сторінка перекладає сама в load(), бо queries дає український

NOTIF_EVENT_SLOT = r'''
<q-td :props="props">
  <span>{{ {decrease:'📉', target_reached:'🎯', restock:'✅', increase:'📈'}[props.value] || '•' }} {{ props.row.event_label }}</span>
</q-td>'''


@ui.page("/notifications")
async def notifications_page() -> None:
    with layout(at("notifs_title"), "/"):
        params = _query_params()
        state = {
            "event": params.get("event", "all"),
            "status": params.get("status", "all"),
            "search": params.get("q", ""),
        }

        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            search = ui.input(placeholder=at("notifs_search"), value=state["search"]) \
                .props("outlined dense clearable debounce=300").classes("flex-grow").style("max-width: 420px")
            with search.add_slot("prepend"):
                ui.icon("search")
            event = ui.select({"all": at("notifs_all_events"), **{c: event_label(c) for c in q.NOTIF_EVENT_LABELS}},
                              value=state["event"]).props("outlined dense").style("min-width: 190px")
            status = ui.toggle({"all": at("notifs_status_all"), "sent": at("notifs_status_sent"),
                                "failed": at("notifs_status_failed")}, value=state["status"]) \
                .props("no-caps unelevated dense toggle-color=primary")
            ui.space()
            ui.label(at("per_page_newest", n=PAGE_SIZE)).classes("pg-muted").style("font-size:13px; white-space:nowrap")

        columns = [
            {"name": "when", "label": at("col_when"), "field": "when", "align": "left", "style": "white-space:nowrap"},
            {"name": "event", "label": at("col_event"), "field": "event", "align": "left"},
            {"name": "title", "label": at("col_product"), "field": "title", "align": "left"},
            {"name": "change", "label": at("col_price"), "field": "change", "align": "right", "style": "white-space:nowrap"},
            {"name": "user", "label": at("col_user"), "field": "user", "align": "left"},
            {"name": "status", "label": at("col_status"), "field": "status", "align": "left"},
        ]
        with card():
            table = ui.table(columns=columns, rows=[], row_key="id").props("flat dense hide-pagination") \
                .classes("w-full")
            table.add_slot("body-cell-event", NOTIF_EVENT_SLOT)
            table.add_slot("body-cell-title", r'''
                <q-td :props="props"><a :href="props.row.url" target="_blank" class="pg-link pg-ellipsis" style="max-width:300px">{{ props.value }}</a>
                <div class="pg-sub">{{ props.row.source }}</div></q-td>''')
            table.add_slot("body-cell-user", r'''
                <q-td :props="props"><a :href="'/users/' + props.row.user_id" class="pg-link">{{ props.value }}</a>
                <span v-if="props.row.username" class="pg-muted"> @{{ props.row.username }}</span></q-td>''')
            table.add_slot("body-cell-status", _notif_status_slot())
            table.add_slot("no-data", _no_data_slot(at("notifs_empty")))

            async def load(page: int) -> None:
                rows, total = await q.notifications_page(
                    page, event=state["event"], status=state["status"], search=state["search"],
                )
                for r in rows:
                    new = money(r["new"], r["currency"])
                    r["change"] = f"{money(r['old'], r['currency'])} → {new}" if r["old"] is not None else new
                    r["event_label"] = event_label(r["event"])
                table.rows = rows
                table.update()
                pager.total = total
                pager.page = page
                pager.render(len(rows))

            pager = Pager(load)

        async def reset_and_load() -> None:
            await load(1)

        def bind(key, default="all"):
            def handler(e):
                state[key] = (e.value or default) if key != "search" else (e.value or "")
                asyncio.create_task(reset_and_load())
            return handler

        search.on_value_change(bind("search", ""))
        event.on_value_change(bind("event"))
        status.on_value_change(bind("status"))

        try:
            start_page = int(params.get("page", "1"))
        except ValueError:
            start_page = 1
        await load(max(1, start_page))


# ─── /checks ─────────────────────────────────────────────────────

@ui.page("/checks")
async def checks_page() -> None:
    with layout(at("checks_title"), "/"):
        params = _query_params()
        state = {"source": params.get("source", "all"), "search": params.get("q", "")}
        _, _, sources = await q.checks_page(1, 1)

        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            search = ui.input(placeholder=at("checks_search"), value=state["search"]) \
                .props("outlined dense clearable debounce=300").classes("flex-grow").style("max-width: 380px")
            with search.add_slot("prepend"):
                ui.icon("search")
            source = ui.select({"all": at("checks_all_sites"), **{s: s for s in sources}}, value=state["source"]) \
                .props("outlined dense").style("min-width: 190px")
            ui.space()
            ui.label(at("per_page_newest", n=PAGE_SIZE)).classes("pg-muted").style("font-size:13px; white-space:nowrap")

        columns = [
            {"name": "when", "label": at("col_when"), "field": "when", "align": "left", "style": "white-space:nowrap"},
            {"name": "title", "label": at("col_product"), "field": "title", "align": "left"},
            {"name": "price_str", "label": at("col_price"), "field": "price_str", "align": "right", "style": "white-space:nowrap"},
            {"name": "available", "label": at("col_availability"), "field": "available", "align": "left"},
            {"name": "user", "label": at("col_user"), "field": "user", "align": "left"},
        ]
        with card():
            table = ui.table(columns=columns, rows=[], row_key="id").props("flat dense hide-pagination").classes("w-full")
            table.add_slot("body-cell-title", r'''
                <q-td :props="props"><a :href="props.row.url" target="_blank" class="pg-link pg-ellipsis">{{ props.value }}</a>
                <div class="pg-sub">{{ props.row.source }}</div></q-td>''')
            table.add_slot("body-cell-available", r'''
                <q-td :props="props"><span class="pg-badge" :class="props.value ? 'good' : 'serious'"><span class="dot"></span>{{ props.value ? __IN_STOCK__ : __OUT__ }}</span></q-td>'''
                .replace("__IN_STOCK__", json.dumps(at("in_stock"), ensure_ascii=False))
                .replace("__OUT__", json.dumps(at("status_out_of_stock"), ensure_ascii=False)))
            table.add_slot("body-cell-user", r'''
                <q-td :props="props"><a :href="'/users/' + props.row.user_id" class="pg-link">{{ props.value }}</a></q-td>''')
            table.add_slot("no-data", _no_data_slot(at("checks_empty")))

            async def load(page: int) -> None:
                rows, total, _ = await q.checks_page(page, source=state["source"], search=state["search"])
                for r in rows:
                    r["price_str"] = money(r["price"], r["currency"])
                table.rows = rows
                table.update()
                pager.total = total
                pager.page = page
                pager.render(len(rows))

            pager = Pager(load)

        def bind(key, default="all"):
            def handler(e):
                state[key] = (e.value or default) if key != "search" else (e.value or "")
                asyncio.create_task(load(1))
            return handler

        search.on_value_change(bind("search", ""))
        source.on_value_change(bind("source"))

        try:
            start_page = int(params.get("page", "1"))
        except ValueError:
            start_page = 1
        await load(max(1, start_page))
