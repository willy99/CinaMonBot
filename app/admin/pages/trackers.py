"""Усі трекери з фільтрами по статусу та майданчику."""
from __future__ import annotations

import asyncio

from nicegui import ui

from app.admin import queries as q
from app.admin.i18n import at, slot_labels
from app.admin.theme import TRACKER_STATUS_KEYS, TRACKER_STATUS_KINDS, card, layout, money, slot_classes

# Підписи статусів у слоті — з at() при рендері (мова з сесії), а не літералами
STATUS_SLOT = r'''
<q-td :props="props">
  <span class="pg-badge" :class="__KINDS__[props.value]">
    <span class="dot"></span>{{ __LABELS__[props.value] || props.value }}
    <span v-if="props.row.errors" class="pg-muted">· {{ props.row.errors }}</span>
  </span>
</q-td>'''


def _status_slot() -> str:
    return STATUS_SLOT.replace("__KINDS__", slot_classes(TRACKER_STATUS_KINDS)) \
        .replace("__LABELS__", slot_labels(TRACKER_STATUS_KEYS))


def _prepare(rows: list[dict]) -> list[dict]:
    """Ціна у форматі мови адмінки + переклад fallback-назви з queries ("Без назви")."""
    for r in rows:
        r["price_str"] = money(r["price"], r["currency"])
        if r["title"] == "Без назви":
            r["title"] = at("untitled")
    return rows


@ui.page("/trackers")
async def trackers_page() -> None:
    with layout(at("nav_trackers"), "/trackers"):
        state = {"status": "all", "source": "all", "search": ""}
        rows, sources = await q.trackers_list()
        rows = _prepare(rows)

        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            search = ui.input(placeholder=at("trackers_search")) \
                .props("outlined dense clearable debounce=300").classes("flex-grow").style("max-width: 380px")
            with search.add_slot("prepend"):
                ui.icon("search")
            status = ui.toggle(
                {"all": at("filter_all"), "active": at("filter_active"), "paused": at("status_paused"),
                 "error": at("trackers_filter_errors")}, value="all",
            ).props("no-caps unelevated dense toggle-color=primary")
            source = ui.select({"all": at("checks_all_sites"), **{s: s for s in sources}}, value="all") \
                .props("outlined dense").style("min-width: 180px")
            ui.space()
            count_label = ui.label(at("trackers_count", n=len(rows))).classes("pg-muted") \
                .style("font-size:13px; white-space:nowrap")

        columns = [
            {"name": "title", "label": at("col_product"), "field": "title", "align": "left"},
            {"name": "source", "label": at("col_site"), "field": "source", "align": "left", "sortable": True},
            {"name": "price", "label": at("col_price"), "field": "price", "align": "right", "sortable": True},
            {"name": "status", "label": at("col_status"), "field": "status", "align": "left", "sortable": True},
            {"name": "user", "label": at("col_user"), "field": "user", "align": "left"},
            {"name": "last_checked", "label": at("col_checked"), "field": "last_checked", "align": "left"},
            {"name": "created", "label": at("col_added"), "field": "created", "align": "left"},
        ]
        with card():
            table = ui.table(columns=columns, rows=rows, row_key="id", pagination=25) \
                .props(f'flat rows-per-page-label="{at("rows_per_page")}"').classes("w-full")
            table.add_slot("body-cell-title", r'''
                <q-td :props="props"><a :href="props.row.url" target="_blank" class="pg-link pg-ellipsis">{{ props.value }}</a></q-td>''')
            table.add_slot("body-cell-price", r'''
                <q-td :props="props">{{ props.row.price_str }}</q-td>''')
            table.add_slot("body-cell-status", _status_slot())
            table.add_slot("body-cell-user", r'''
                <q-td :props="props"><a :href="'/users/' + props.row.user_id" class="pg-link">{{ props.value }}</a></q-td>''')

        async def reload() -> None:
            new_rows, _ = await q.trackers_list(state["status"], state["source"], state["search"])
            table.rows = _prepare(new_rows)
            table.update()
            count_label.text = at("trackers_count", n=len(new_rows))

        def bind(key):
            def handler(e):
                state[key] = e.value or "all" if key != "search" else (e.value or "")
                asyncio.create_task(reload())
            return handler

        search.on_value_change(bind("search"))
        status.on_value_change(bind("status"))
        source.on_value_change(bind("source"))
