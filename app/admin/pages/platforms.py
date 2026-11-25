"""Платформи: реєстр магазинів + статистика парсингу (ScrapeLog) і трекерів по доменах."""
from __future__ import annotations

import asyncio
import json

from nicegui import ui

from app.admin import queries as q
from app.admin.i18n import admin_lang, at, slot_labels
from app.admin.theme import (
    PLATFORM_STATUS_KEYS, PLATFORM_STATUS_KINDS, STATUS, badge, bar_chart, card, kpi, layout, platform_status_badge,
    slot_classes,
)
from app.i18n import country_label
from app.services.scrapers import registry

# Підписи у слотах — з at() при рендері (див. _status_slot / _trackers_slot), кольори — токени теми
STATUS_SLOT = r'''
<q-td :props="props">
  <span class="pg-badge" :class="__KINDS__[props.value] || ''">
    <span class="dot"></span>
    {{ __LABELS__[props.value] || props.value }}
  </span>
</q-td>'''

RATE_SLOT = r'''
<q-td :props="props">
  <span v-if="props.value === null || props.value === undefined" class="pg-muted">—</span>
  <span v-else :style="{color: props.value >= 30 ? 'var(--critical-ink)' : props.value >= 10 ? 'var(--serious)' : 'inherit', fontWeight: props.value >= 10 ? 600 : 400}">{{ props.value }}%</span>
</q-td>'''

TRACKERS_SLOT = r'''
<q-td :props="props">
  {{ props.value }}
  <span class="pg-muted">__ACTIVE__<span v-if="props.row.error"> · <span style="color:var(--serious)">__ERRORS__</span></span></span>
</q-td>'''


def _status_slot() -> str:
    return STATUS_SLOT.replace("__KINDS__", slot_classes(PLATFORM_STATUS_KINDS)) \
        .replace("__LABELS__", slot_labels(PLATFORM_STATUS_KEYS))


def _trackers_slot() -> str:
    return TRACKERS_SLOT.replace("__ACTIVE__", at("active_short", n="{{ props.row.active }}")) \
        .replace("__ERRORS__", at("errors_n", n="{{ props.row.error }}"))


def _countries() -> dict[str, str]:
    codes = sorted({p.country for p in registry.PLATFORMS if p.country})
    lang = admin_lang()
    return {"all": at("platforms_all_countries"), **{c: country_label(c, lang) for c in codes}}


@ui.page("/platforms")
async def platforms_page() -> None:
    with layout(at("nav_platforms"), "/platforms"):
        ov, problems = await asyncio.gather(q.platforms_overview(), q.platform_problems_24h())

        # ── KPI ──
        with ui.element("div").classes("w-full grid gap-3").style(
            "grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));"
        ):
            kpi(at("platform_verified"), ov.verified, icon="verified")
            kpi(at("platform_experimental"), ov.experimental, icon="science")
            kpi(at("platform_needs_js"), ov.needs_js, icon="javascript")
            kpi(at("platforms_kpi_blocked"), ov.blocked, icon="block")
            kpi(at("platforms_kpi_other"), ov.other_domains, delta_label=at("platforms_kpi_other_delta"), icon="public")
            kpi(at("platforms_kpi_success"), f"{ov.success_rate_24h:g}%" if ov.success_rate_24h is not None else "—",
                delta_label=at("platforms_kpi_attempts", n=ov.checks_24h), icon="task_alt")
            kpi(at("platforms_kpi_healed"), ov.healed_30d, delta_label=at("platforms_kpi_healed_delta"), icon="healing")

        # ── Проблеми за 24 год ──
        with card(at("platforms_problems_title"), at("platforms_problems_sub")):
            if not problems:
                with ui.row().classes("items-center gap-2"):
                    badge(at("all_ok"), "good")
                    ui.label(at("platforms_problems_empty")).classes("pg-muted").style("font-size:13px")
            else:
                cols = [
                    {"name": "name", "label": at("col_platform"), "field": "name", "align": "left"},
                    {"name": "domain", "label": at("col_domain"), "field": "domain", "align": "left", "classes": "pg-mono"},
                    {"name": "status", "label": at("col_status"), "field": "status", "align": "left"},
                    {"name": "checks", "label": at("col_attempts"), "field": "checks", "align": "right"},
                    {"name": "fails", "label": at("col_fails"), "field": "fails", "align": "right"},
                    {"name": "rate", "label": at("col_rate"), "field": "rate", "align": "right"},
                    {"name": "healed", "label": at("col_autoheal"), "field": "healed", "align": "left"},
                    {"name": "last_fail_reason", "label": at("col_last_reason"), "field": "last_fail_reason", "align": "left", "classes": "pg-mono"},
                ]
                tbl = ui.table(columns=cols, rows=problems, row_key="id").props("flat dense").classes("w-full")
                tbl.add_slot("body-cell-status", _status_slot())
                tbl.add_slot("body-cell-rate", RATE_SLOT)
                tbl.add_slot("body-cell-healed", r'''
                    <q-td :props="props">
                      <span v-if="props.value" class="pg-badge good"><span class="dot"></span>__YES__ · {{ props.value }}</span>
                      <span v-else class="pg-muted">__NO__</span>
                    </q-td>'''.replace("__YES__", at("yes")).replace("__NO__", at("no")))

        # ── Фільтри ──
        state = {"status": "all", "country": "all", "search": ""}
        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            search = ui.input(placeholder=at("platforms_search")) \
                .props("outlined dense clearable debounce=300").classes("flex-grow").style("max-width: 360px")
            with search.add_slot("prepend"):
                ui.icon("search")
            status = ui.toggle(
                {"all": at("filter_all"), "verified": at("platforms_filter_verified"),
                 "experimental": at("platforms_filter_experimental"), "needs_js": at("platform_needs_js"),
                 "blocked": at("platforms_kpi_blocked"), "other": at("platforms_kpi_other")},
                value="all",
            ).props("no-caps unelevated dense toggle-color=primary")
            country = ui.select(_countries(), value="all").props("outlined dense").style("min-width: 170px")
            ui.space()
            count_label = ui.label().classes("pg-muted").style("font-size:13px; white-space:nowrap")

        # ── Таблиця ──
        columns = [
            {"name": "name", "label": at("col_platform"), "field": "name", "align": "left", "sortable": True},
            {"name": "domain", "label": at("col_domain"), "field": "domain", "align": "left", "classes": "pg-mono"},
            {"name": "country", "label": at("col_country"), "field": "country", "align": "left", "sortable": True},
            {"name": "status", "label": at("col_status"), "field": "status", "align": "left", "sortable": True},
            {"name": "strategy", "label": at("col_strategy"), "field": "strategy", "align": "left", "classes": "pg-mono"},
            {"name": "trackers", "label": at("col_trackers"), "field": "trackers", "align": "right", "sortable": True,
             "sort": "(a, b, ra, rb) => ra.active - rb.active || a - b"},
            {"name": "users", "label": at("col_users_short"), "field": "users", "align": "right", "sortable": True},
            {"name": "checks_30d", "label": at("col_attempts_30d"), "field": "checks_30d", "align": "right", "sortable": True},
            {"name": "fails_30d", "label": at("col_fails"), "field": "fails_30d", "align": "right", "sortable": True},
            {"name": "error_rate_30d", "label": at("col_errors"), "field": "error_rate_30d", "align": "right", "sortable": True},
            {"name": "healed_30d", "label": at("col_healed_short"), "field": "healed_30d", "align": "right", "sortable": True},
            {"name": "last_ok", "label": at("col_last_ok"), "field": "last_ok", "align": "left",
             "sortable": True, "sort": "(a, b, ra, rb) => ra.last_ok_sort < rb.last_ok_sort ? -1 : 1"},
            {"name": "last_fail_reason", "label": at("col_last_error"), "field": "last_fail_reason", "align": "left", "classes": "pg-mono"},
            {"name": "avg_ms", "label": at("col_avg_ms"), "field": "avg_ms", "align": "right", "sortable": True},
        ]
        with card():
            table = ui.table(columns=columns, rows=[], row_key="id", pagination=25) \
                .props(f'flat dense rows-per-page-label="{at("rows_per_page")}"').classes("w-full")
            table.add_slot("body-cell-status", _status_slot())
            table.add_slot("body-cell-error_rate_30d", RATE_SLOT)
            table.add_slot("body-cell-trackers", _trackers_slot())
            table.add_slot("body-cell-name", r'''
                <q-td :props="props"><span style="font-weight:600">{{ props.value }}</span></q-td>''')
            table.add_slot("body-cell-avg_ms", r'''
                <q-td :props="props">{{ props.value === null ? '—' : props.value }}</q-td>''')
            table.add_slot("body-cell-last_fail_reason", r'''
                <q-td :props="props"><span v-if="props.value">{{ props.value }} <span class="pg-muted" style="font-family:system-ui">· {{ props.row.last_fail_at }}</span></span><span v-else class="pg-muted">—</span></q-td>''')
            table.add_slot("no-data", f'<div class="full-width row flex-center q-pa-md pg-muted">{at("nothing_found")}</div>')
            table.on("rowClick", lambda e: _open_detail(e.args[1], on_change=reload))

        async def reload() -> None:
            rows = await q.platforms_table(state["status"], state["country"], state["search"])
            table.rows = rows
            table.update()
            count_label.text = at("platforms_count", n=len(rows))

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


async def _open_detail(row: dict, on_change=None) -> None:
    """Діалог з довідником платформи, денними ok/fail за 30 днів і останніми спробами."""
    d = await q.platform_detail(row["domain"])
    with ui.dialog() as dlg, ui.card().style("min-width: 820px; max-width: 95vw; max-height: 92vh"):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            with ui.row().classes("items-center gap-3 no-wrap"):
                ui.label(d["name"]).style("font-weight:700; font-size:18px")
                platform_status_badge(d["status"])
                if d["country"]:
                    ui.label(country_label(d["country"], admin_lang())).classes("pg-muted").style("font-size:13px")
            ui.button(icon="close", on_click=dlg.close).props("flat round dense")

        if d["auto_blocked"]:
            # Магазин стабільно блокує перевірки: позиції юзерів на паузі, поки адмін не розблокує
            async def unblock() -> None:
                from botkit.admin import auth
                n = await q.unblock_domain(d["domain"])
                await auth.audit("domain_unblock", d["domain"], f"resumed={n}")
                ui.notify(at("platforms_unblocked", n=n), type="positive")
                dlg.close()
                if on_change is not None:
                    await on_change()

            with ui.row().classes("w-full items-center justify-between no-wrap gap-4") \
                    .style("padding:10px 12px; border-radius:8px; background:var(--critical-bg, rgba(208,59,59,.12))"):
                ui.label(at("platforms_auto_blocked", when=d["blocked_at"], reason=d["blocked_reason"] or "—",
                            n=d["paused_domain"])).style("font-size:13px")
                ui.button(at("platforms_unblock_btn"), icon="lock_open", on_click=unblock).props("unelevated dense no-caps")

        with ui.row().classes("items-center gap-4 wrap").style("font-size:13px"):
            ui.label(d["domain"]).classes("pg-mono")
            if d["aliases"]:
                ui.label(at("platforms_aliases", list=", ".join(d["aliases"]))).classes("pg-muted")
            if d["strategy"]:
                ui.label(at("platforms_strategy", name=d["strategy"])).classes("pg-muted")
            if d["sample_url"]:
                ui.link(at("platforms_sample_page"), d["sample_url"], new_tab=True).classes("pg-link")
        if d["notes"]:
            # нотатки реєстру — українські як є; іншою мовою підказуємо це
            with ui.row().classes("items-baseline gap-2 no-wrap"):
                ui.label(d["notes"]).style("font-size:13px; color:var(--ink-2)")
                if admin_lang() != "uk":
                    ui.label(at("platforms_notes_lang_hint")).classes("pg-sub")

        with ui.row().classes("items-center gap-6 pg-muted").style("font-size:13px"):
            ui.label(at("platforms_detail_trackers", total=row.get("trackers", 0), active=row.get("active", 0),
                        error=row.get("error", 0)))
            ui.label(at("platforms_detail_users", n=row.get("users", 0)))
            ui.label(at("platforms_detail_healed", n=row.get("healed_30d", 0)))

        total_ok = sum(v for _, v in d["daily_ok"])
        total_fail = sum(v for _, v in d["daily_fail"])
        with ui.element("div").classes("w-full grid gap-4").style("grid-template-columns: 1fr 1fr;"):
            with card(at("platforms_ok_attempts"), at("platforms_by_day_n", n=total_ok)):
                bar_chart(d["daily_ok"], color=STATUS["good"], height="170px")
            with card(at("platforms_fail_attempts"), at("platforms_by_day_n", n=total_fail)):
                bar_chart(d["daily_fail"], color=STATUS["critical"], height="170px")

        with card(at("platforms_recent_title"), at("last_n", n=len(d["recent"]))):
            if not d["recent"]:
                ui.label(at("platforms_recent_empty")).classes("pg-muted").style("font-size:13px")
            else:
                cols = [
                    {"name": "when", "label": at("col_when"), "field": "when", "align": "left"},
                    {"name": "domain", "label": at("col_domain"), "field": "domain", "align": "left", "classes": "pg-mono"},
                    {"name": "ok", "label": at("col_result"), "field": "ok", "align": "left"},
                    {"name": "how", "label": at("col_strategy_reason"), "field": "how", "align": "left", "classes": "pg-mono"},
                    {"name": "healed", "label": at("col_autoheal_short"), "field": "healed", "align": "left"},
                    {"name": "duration_ms", "label": at("col_ms"), "field": "duration_ms", "align": "right"},
                ]
                rows = [{**r, "how": r["strategy"] if r["ok"] else r["reason"]} for r in d["recent"]]
                tbl = ui.table(columns=cols, rows=rows, row_key="id").props("flat dense").classes("w-full")
                tbl.add_slot("body-cell-ok", r'''
                    <q-td :props="props">
                      <span class="pg-badge" :class="props.value ? 'good' : 'critical'"><span class="dot"></span>{{ props.value ? 'OK' : __ERROR__ }}</span>
                    </q-td>'''.replace("__ERROR__", json.dumps(at("status_error"), ensure_ascii=False)))
                tbl.add_slot("body-cell-healed", r'''
                    <q-td :props="props"><span v-if="props.value">✓</span><span v-else class="pg-muted">—</span></q-td>''')
    dlg.open()
