"""
Журнали з серверним пейджингом: /notifications (сповіщення юзерам) і /checks (перевірки цін).

Відкриваються кліком по картках/плитках дашборду. Показуємо по 25 від найновіших,
фільтри застосовуються на сервері, пейджер — « ‹ стор. N з M › ».
Параметри в URL (?page=2&event=decrease) — щоб посилання можна було передати.
"""
from __future__ import annotations

import asyncio
import math
from typing import Awaitable, Callable

from nicegui import ui

from app.admin import queries as q
from app.admin.theme import MUTED, card, layout, money

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
        self.info.text = f"Показано {start}–{end} з {self.total}"
        self.label.text = f"стор. {self.page} з {self.pages}"
        for b, disabled in ((self.first, self.page <= 1), (self.prev, self.page <= 1),
                            (self.next, self.page >= self.pages), (self.last, self.page >= self.pages)):
            if disabled:
                b.props("disable")
            else:
                b.props(remove="disable")


# ─── /notifications ──────────────────────────────────────────────

NOTIF_STATUS_SLOT = r'''
<q-td :props="props">
  <span class="pg-badge" :class="props.value === 'sent' ? 'good' : 'critical'">
    <span class="dot"></span>{{ props.value === 'sent' ? 'Надіслано' : 'Не вдалось' }}
  </span>
  <div v-if="props.row.error" style="color:#898781; font-size:11px; max-width:260px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap">{{ props.row.error }}</div>
</q-td>'''

NOTIF_EVENT_SLOT = r'''
<q-td :props="props">
  <span>{{ {decrease:'📉', target_reached:'🎯', restock:'✅', increase:'📈'}[props.value] || '•' }} {{ props.row.event_label }}</span>
</q-td>'''


@ui.page("/notifications")
async def notifications_page() -> None:
    with layout("Сповіщення", "/"):
        params = _query_params()
        state = {
            "event": params.get("event", "all"),
            "status": params.get("status", "all"),
            "search": params.get("q", ""),
        }

        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            search = ui.input(placeholder="Пошук: товар, ім'я, username або Telegram ID", value=state["search"]) \
                .props("outlined dense clearable debounce=300").classes("flex-grow").style("max-width: 420px")
            with search.add_slot("prepend"):
                ui.icon("search")
            event = ui.select({"all": "Усі події", **q.NOTIF_EVENT_LABELS}, value=state["event"]) \
                .props("outlined dense").style("min-width: 190px")
            status = ui.toggle({"all": "Усі", "sent": "Надіслані", "failed": "Невдалі"}, value=state["status"]) \
                .props("no-caps unelevated dense toggle-color=primary")
            ui.space()
            ui.label(f"по {PAGE_SIZE} від найновіших").classes("pg-muted").style("font-size:13px; white-space:nowrap")

        columns = [
            {"name": "when", "label": "Коли", "field": "when", "align": "left", "style": "white-space:nowrap"},
            {"name": "event", "label": "Подія", "field": "event", "align": "left"},
            {"name": "title", "label": "Товар", "field": "title", "align": "left"},
            {"name": "change", "label": "Ціна", "field": "change", "align": "right", "style": "white-space:nowrap"},
            {"name": "user", "label": "Користувач", "field": "user", "align": "left"},
            {"name": "status", "label": "Статус", "field": "status", "align": "left"},
        ]
        with card():
            table = ui.table(columns=columns, rows=[], row_key="id").props("flat dense hide-pagination") \
                .classes("w-full")
            table.add_slot("body-cell-event", NOTIF_EVENT_SLOT)
            table.add_slot("body-cell-title", r'''
                <q-td :props="props"><a :href="props.row.url" target="_blank" class="pg-link pg-ellipsis" style="max-width:300px">{{ props.value }}</a>
                <div style="color:#898781; font-size:11px">{{ props.row.source }}</div></q-td>''')
            table.add_slot("body-cell-user", r'''
                <q-td :props="props"><a :href="'/users/' + props.row.user_id" class="pg-link">{{ props.value }}</a>
                <span v-if="props.row.username" style="color:#898781"> @{{ props.row.username }}</span></q-td>''')
            table.add_slot("body-cell-status", NOTIF_STATUS_SLOT)
            table.add_slot("no-data", '<div class="full-width row flex-center q-pa-md" style="color:#898781">Сповіщень немає</div>')

            async def load(page: int) -> None:
                rows, total = await q.notifications_page(
                    page, event=state["event"], status=state["status"], search=state["search"],
                )
                for r in rows:
                    new = money(r["new"], r["currency"])
                    r["change"] = f"{money(r['old'], r['currency'])} → {new}" if r["old"] is not None else new
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
    with layout("Перевірки цін", "/"):
        params = _query_params()
        state = {"source": params.get("source", "all"), "search": params.get("q", "")}
        _, _, sources = await q.checks_page(1, 1)

        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            search = ui.input(placeholder="Пошук: товар, ім'я або username", value=state["search"]) \
                .props("outlined dense clearable debounce=300").classes("flex-grow").style("max-width: 380px")
            with search.add_slot("prepend"):
                ui.icon("search")
            source = ui.select({"all": "Усі сайти", **{s: s for s in sources}}, value=state["source"]) \
                .props("outlined dense").style("min-width: 190px")
            ui.space()
            ui.label(f"по {PAGE_SIZE} від найновіших").classes("pg-muted").style("font-size:13px; white-space:nowrap")

        columns = [
            {"name": "when", "label": "Коли", "field": "when", "align": "left", "style": "white-space:nowrap"},
            {"name": "title", "label": "Товар", "field": "title", "align": "left"},
            {"name": "price_str", "label": "Ціна", "field": "price_str", "align": "right", "style": "white-space:nowrap"},
            {"name": "available", "label": "Наявність", "field": "available", "align": "left"},
            {"name": "user", "label": "Користувач", "field": "user", "align": "left"},
        ]
        with card():
            table = ui.table(columns=columns, rows=[], row_key="id").props("flat dense hide-pagination").classes("w-full")
            table.add_slot("body-cell-title", r'''
                <q-td :props="props"><a :href="props.row.url" target="_blank" class="pg-link pg-ellipsis">{{ props.value }}</a>
                <div style="color:#898781; font-size:11px">{{ props.row.source }}</div></q-td>''')
            table.add_slot("body-cell-available", r'''
                <q-td :props="props"><span class="pg-badge" :class="props.value ? 'good' : 'serious'"><span class="dot"></span>{{ props.value ? 'В наявності' : 'Немає' }}</span></q-td>''')
            table.add_slot("body-cell-user", r'''
                <q-td :props="props"><a :href="'/users/' + props.row.user_id" class="pg-link">{{ props.value }}</a></q-td>''')
            table.add_slot("no-data", '<div class="full-width row flex-center q-pa-md" style="color:#898781">Перевірок ще не було</div>')

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
