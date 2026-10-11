"""Платформи: реєстр магазинів + статистика парсингу (ScrapeLog) і трекерів по доменах."""
from __future__ import annotations

import asyncio

from nicegui import ui

from app.admin import queries as q
from app.admin.theme import STATUS, badge, bar_chart, card, kpi, layout
from app.services.scrapers import registry

STATUS_LABELS = {
    "verified": "Перевірено",
    "experimental": "Експериментально",
    "needs_js": "Потрібен JS",
    "blocked": "Блокує",
    "other": "Інший сайт",
}
STATUS_KIND = {"verified": "good", "experimental": "warning", "needs_js": "serious", "blocked": "critical", "other": ""}

STATUS_SLOT = r'''
<q-td :props="props">
  <span class="pg-badge" :class="{verified:'good', experimental:'warning', needs_js:'serious', blocked:'critical'}[props.value] || ''">
    <span class="dot"></span>
    {{ {verified:'Перевірено', experimental:'Експериментально', needs_js:'Потрібен JS', blocked:'Блокує', other:'Інший сайт'}[props.value] || props.value }}
  </span>
</q-td>'''

RATE_SLOT = r'''
<q-td :props="props">
  <span v-if="props.value === null || props.value === undefined" style="color:#898781">—</span>
  <span v-else :style="{color: props.value >= 30 ? '#ffb3b3' : props.value >= 10 ? '#ec835a' : 'inherit', fontWeight: props.value >= 10 ? 600 : 400}">{{ props.value }}%</span>
</q-td>'''

TRACKERS_SLOT = r'''
<q-td :props="props">
  {{ props.value }}
  <span style="color:#898781">/ {{ props.row.active }} акт.<span v-if="props.row.error"> · <span style="color:#ec835a">{{ props.row.error }} пом.</span></span></span>
</q-td>'''


def _countries() -> dict[str, str]:
    codes = sorted({p.country for p in registry.PLATFORMS if p.country})
    return {"all": "Усі країни", **{c: c for c in codes}}


@ui.page("/platforms")
async def platforms_page() -> None:
    with layout("Платформи", "/platforms"):
        ov, problems = await asyncio.gather(q.platforms_overview(), q.platform_problems_24h())

        # ── KPI ──
        with ui.element("div").classes("w-full grid gap-3").style(
            "grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));"
        ):
            kpi("Перевірено", ov.verified, icon="verified")
            kpi("Експериментально", ov.experimental, icon="science")
            kpi("Потрібен JS", ov.needs_js, icon="javascript")
            kpi("Блокують", ov.blocked, icon="block")
            kpi("Інші сайти", ov.other_domains, delta_label="домени поза реєстром", icon="public")
            kpi("Успішність 24 год", f"{ov.success_rate_24h:g}%" if ov.success_rate_24h is not None else "—",
                delta_label=f"{ov.checks_24h} спроб парсингу", icon="task_alt")
            kpi("Вилікувано 30 дн.", ov.healed_30d, delta_label="селектори підібрав Claude", icon="healing")

        # ── Проблеми за 24 год ──
        with card("Проблеми за 24 год", "частка невдач ≥ 30% при ≥ 3 спробах"):
            if not problems:
                with ui.row().classes("items-center gap-2"):
                    badge("Усе гаразд", "good")
                    ui.label("Жодної платформи з високою часткою невдач").classes("pg-muted").style("font-size:13px")
            else:
                cols = [
                    {"name": "name", "label": "Платформа", "field": "name", "align": "left"},
                    {"name": "domain", "label": "Домен", "field": "domain", "align": "left", "classes": "pg-mono"},
                    {"name": "status", "label": "Статус", "field": "status", "align": "left"},
                    {"name": "checks", "label": "Спроб", "field": "checks", "align": "right"},
                    {"name": "fails", "label": "Невдач", "field": "fails", "align": "right"},
                    {"name": "rate", "label": "Частка", "field": "rate", "align": "right"},
                    {"name": "healed", "label": "Автолікування", "field": "healed", "align": "left"},
                    {"name": "last_fail_reason", "label": "Остання причина", "field": "last_fail_reason", "align": "left", "classes": "pg-mono"},
                ]
                tbl = ui.table(columns=cols, rows=problems, row_key="id").props("flat dense").classes("w-full")
                tbl.add_slot("body-cell-status", STATUS_SLOT)
                tbl.add_slot("body-cell-rate", RATE_SLOT)
                tbl.add_slot("body-cell-healed", r'''
                    <q-td :props="props">
                      <span v-if="props.value" class="pg-badge good"><span class="dot"></span>так · {{ props.value }}</span>
                      <span v-else style="color:#898781">ні</span>
                    </q-td>''')

        # ── Фільтри ──
        state = {"status": "all", "country": "all", "search": ""}
        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            search = ui.input(placeholder="Пошук: назва або домен") \
                .props("outlined dense clearable debounce=300").classes("flex-grow").style("max-width: 360px")
            with search.add_slot("prepend"):
                ui.icon("search")
            status = ui.toggle(
                {"all": "Усі", "verified": "Перевірені", "experimental": "Експерим.",
                 "needs_js": "Потрібен JS", "blocked": "Блокують", "other": "Інші сайти"},
                value="all",
            ).props("no-caps unelevated dense toggle-color=primary")
            country = ui.select(_countries(), value="all").props("outlined dense").style("min-width: 150px")
            ui.space()
            count_label = ui.label().classes("pg-muted").style("font-size:13px; white-space:nowrap")

        # ── Таблиця ──
        columns = [
            {"name": "name", "label": "Платформа", "field": "name", "align": "left", "sortable": True},
            {"name": "domain", "label": "Домен", "field": "domain", "align": "left", "classes": "pg-mono"},
            {"name": "country", "label": "Країна", "field": "country", "align": "left", "sortable": True},
            {"name": "status", "label": "Статус", "field": "status", "align": "left", "sortable": True},
            {"name": "strategy", "label": "Стратегія", "field": "strategy", "align": "left", "classes": "pg-mono"},
            {"name": "trackers", "label": "Трекери", "field": "trackers", "align": "right", "sortable": True,
             "sort": "(a, b, ra, rb) => ra.active - rb.active || a - b"},
            {"name": "users", "label": "Юзерів", "field": "users", "align": "right", "sortable": True},
            {"name": "checks_30d", "label": "Спроб 30д", "field": "checks_30d", "align": "right", "sortable": True},
            {"name": "fails_30d", "label": "Невдач", "field": "fails_30d", "align": "right", "sortable": True},
            {"name": "error_rate_30d", "label": "Помилок", "field": "error_rate_30d", "align": "right", "sortable": True},
            {"name": "healed_30d", "label": "Вилік.", "field": "healed_30d", "align": "right", "sortable": True},
            {"name": "last_ok", "label": "Останній успіх", "field": "last_ok", "align": "left",
             "sortable": True, "sort": "(a, b, ra, rb) => ra.last_ok_sort < rb.last_ok_sort ? -1 : 1"},
            {"name": "last_fail_reason", "label": "Остання помилка", "field": "last_fail_reason", "align": "left", "classes": "pg-mono"},
            {"name": "avg_ms", "label": "Сер. мс", "field": "avg_ms", "align": "right", "sortable": True},
        ]
        with card():
            table = ui.table(columns=columns, rows=[], row_key="id", pagination=25) \
                .props('flat dense rows-per-page-label="Рядків:"').classes("w-full")
            table.add_slot("body-cell-status", STATUS_SLOT)
            table.add_slot("body-cell-error_rate_30d", RATE_SLOT)
            table.add_slot("body-cell-trackers", TRACKERS_SLOT)
            table.add_slot("body-cell-name", r'''
                <q-td :props="props"><span style="font-weight:600">{{ props.value }}</span></q-td>''')
            table.add_slot("body-cell-avg_ms", r'''
                <q-td :props="props">{{ props.value === null ? '—' : props.value }}</q-td>''')
            table.add_slot("body-cell-last_fail_reason", r'''
                <q-td :props="props"><span v-if="props.value">{{ props.value }} <span style="color:#898781; font-family:system-ui">· {{ props.row.last_fail_at }}</span></span><span v-else style="color:#898781">—</span></q-td>''')
            table.add_slot("no-data", '<div class="full-width row flex-center q-pa-md" style="color:#898781">Нічого не знайдено</div>')
            table.on("rowClick", lambda e: _open_detail(e.args[1]))

        async def reload() -> None:
            rows = await q.platforms_table(state["status"], state["country"], state["search"])
            table.rows = rows
            table.update()
            count_label.text = f"{len(rows)} платформ"

        def on_search(e):
            state["search"] = e.value or ""
            asyncio.create_task(reload())

        def on_status(e):
            state["status"] = e.value or "all"
            asyncio.create_task(reload())

        def on_country(e):
            state["country"] = e.value or "all"
            asyncio.create_task(reload())

        search.on_value_change(on_search)
        status.on_value_change(on_status)
        country.on_value_change(on_country)
        await reload()


async def _open_detail(row: dict) -> None:
    """Діалог з довідником платформи, денними ok/fail за 30 днів і останніми спробами."""
    d = await q.platform_detail(row["domain"])
    with ui.dialog() as dlg, ui.card().style("min-width: 820px; max-width: 95vw; max-height: 92vh"):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            with ui.row().classes("items-center gap-3 no-wrap"):
                ui.label(d["name"]).style("font-weight:700; font-size:18px")
                badge(STATUS_LABELS.get(d["status"], d["status"]), STATUS_KIND.get(d["status"], ""))
                if d["country"]:
                    ui.label(d["country"]).classes("pg-muted").style("font-size:13px")
            ui.button(icon="close", on_click=dlg.close).props("flat round dense")

        with ui.row().classes("items-center gap-4 wrap").style("font-size:13px"):
            ui.label(d["domain"]).classes("pg-mono")
            if d["aliases"]:
                ui.label("аліаси: " + ", ".join(d["aliases"])).classes("pg-muted")
            if d["strategy"]:
                ui.label(f"стратегія: {d['strategy']}").classes("pg-muted")
            if d["sample_url"]:
                ui.link("приклад сторінки ↗", d["sample_url"], new_tab=True).classes("pg-link")
        if d["notes"]:
            ui.label(d["notes"]).style("font-size:13px; color:#c3c2b7")

        with ui.row().classes("items-center gap-6").style("font-size:13px; color:#898781"):
            ui.label(f"Трекерів: {row.get('trackers', 0)} · активних {row.get('active', 0)} · з помилкою {row.get('error', 0)}")
            ui.label(f"Юзерів: {row.get('users', 0)}")
            ui.label(f"Вилікувано за 30 дн.: {row.get('healed_30d', 0)}")

        total_ok = sum(v for _, v in d["daily_ok"])
        total_fail = sum(v for _, v in d["daily_fail"])
        with ui.element("div").classes("w-full grid gap-4").style("grid-template-columns: 1fr 1fr;"):
            with card("Успішні спроби", f"по днях, 30 днів · {total_ok}"):
                bar_chart(d["daily_ok"], color=STATUS["good"], height="170px")
            with card("Невдалі спроби", f"по днях, 30 днів · {total_fail}"):
                bar_chart(d["daily_fail"], color=STATUS["critical"], height="170px")

        with card("Останні спроби парсингу", f"{len(d['recent'])} останніх"):
            if not d["recent"]:
                ui.label("Записів у журналі ще немає").classes("pg-muted").style("font-size:13px")
            else:
                cols = [
                    {"name": "when", "label": "Коли", "field": "when", "align": "left"},
                    {"name": "domain", "label": "Домен", "field": "domain", "align": "left", "classes": "pg-mono"},
                    {"name": "ok", "label": "Результат", "field": "ok", "align": "left"},
                    {"name": "how", "label": "Стратегія / причина", "field": "how", "align": "left", "classes": "pg-mono"},
                    {"name": "healed", "label": "Автолік.", "field": "healed", "align": "left"},
                    {"name": "duration_ms", "label": "мс", "field": "duration_ms", "align": "right"},
                ]
                rows = [{**r, "how": r["strategy"] if r["ok"] else r["reason"]} for r in d["recent"]]
                tbl = ui.table(columns=cols, rows=rows, row_key="id").props("flat dense").classes("w-full")
                tbl.add_slot("body-cell-ok", r'''
                    <q-td :props="props">
                      <span class="pg-badge" :class="props.value ? 'good' : 'critical'"><span class="dot"></span>{{ props.value ? 'OK' : 'Помилка' }}</span>
                    </q-td>''')
                tbl.add_slot("body-cell-healed", r'''
                    <q-td :props="props"><span v-if="props.value">✓</span><span v-else style="color:#898781">—</span></q-td>''')
    dlg.open()
