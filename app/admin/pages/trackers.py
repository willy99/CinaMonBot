"""Усі трекери з фільтрами по статусу та майданчику."""
from __future__ import annotations

import asyncio

from nicegui import ui

from app.admin import queries as q
from app.admin.theme import card, layout

STATUS_SLOT = r'''
<q-td :props="props">
  <span class="pg-badge" :class="{active:'good', paused:'warning', paused_limit:'warning', error:'critical', out_of_stock:'serious'}[props.value]">
    <span class="dot"></span>{{ {active:'Активний', paused:'На паузі', paused_limit:'Пауза: ліміт', error:'Помилка', out_of_stock:'Немає'}[props.value] || props.value }}
    <span v-if="props.row.errors" style="color:#898781">· {{ props.row.errors }}</span>
  </span>
</q-td>'''


@ui.page("/trackers")
async def trackers_page() -> None:
    with layout("Трекери", "/trackers"):
        state = {"status": "all", "source": "all", "search": ""}
        rows, sources = await q.trackers_list()

        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            search = ui.input(placeholder="Пошук за назвою товару") \
                .props("outlined dense clearable debounce=300").classes("flex-grow").style("max-width: 380px")
            with search.add_slot("prepend"):
                ui.icon("search")
            status = ui.toggle(
                {"all": "Усі", "active": "Активні", "paused": "На паузі", "error": "Помилки"}, value="all",
            ).props("no-caps unelevated dense toggle-color=primary")
            source = ui.select({"all": "Усі сайти", **{s: s for s in sources}}, value="all") \
                .props("outlined dense").style("min-width: 180px")
            ui.space()
            count_label = ui.label(f"{len(rows)} трекерів").classes("pg-muted").style("font-size:13px; white-space:nowrap")

        columns = [
            {"name": "title", "label": "Товар", "field": "title", "align": "left"},
            {"name": "source", "label": "Сайт", "field": "source", "align": "left", "sortable": True},
            {"name": "price", "label": "Ціна", "field": "price", "align": "right", "sortable": True},
            {"name": "status", "label": "Статус", "field": "status", "align": "left", "sortable": True},
            {"name": "user", "label": "Користувач", "field": "user", "align": "left"},
            {"name": "last_checked", "label": "Перевірено", "field": "last_checked", "align": "left"},
            {"name": "created", "label": "Додано", "field": "created", "align": "left"},
        ]
        with card():
            table = ui.table(columns=columns, rows=rows, row_key="id", pagination=25).props('flat rows-per-page-label="Рядків:"').classes("w-full")
            table.add_slot("body-cell-title", r'''
                <q-td :props="props"><a :href="props.row.url" target="_blank" class="pg-link pg-ellipsis">{{ props.value }}</a></q-td>''')
            table.add_slot("body-cell-price", r'''
                <q-td :props="props">{{ props.value === null ? '—' : props.value.toLocaleString('uk-UA') + ' ' + props.row.currency }}</q-td>''')
            table.add_slot("body-cell-status", STATUS_SLOT)
            table.add_slot("body-cell-user", r'''
                <q-td :props="props"><a :href="'/users/' + props.row.user_id" class="pg-link">{{ props.value }}</a></q-td>''')

        async def reload() -> None:
            new_rows, _ = await q.trackers_list(state["status"], state["source"], state["search"])
            table.rows = new_rows
            table.update()
            count_label.text = f"{len(new_rows)} трекерів"

        def bind(key):
            def handler(e):
                state[key] = e.value or "all" if key != "search" else (e.value or "")
                asyncio.create_task(reload())
            return handler

        search.on_value_change(bind("search"))
        status.on_value_change(bind("status"))
        source.on_value_change(bind("source"))
