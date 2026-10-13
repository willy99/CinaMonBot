"""Дашборд: KPI, динаміка по днях, розподіли, Premium що закінчується, проблемні трекери."""
from __future__ import annotations

import asyncio

from nicegui import ui

from app.admin import queries as q
from app.admin.i18n import amoney, at, event_label, slot_labels
from app.admin.theme import (
    PLATFORM_STATUS_KEYS, SERIES, TRACKER_STATUS_KEYS, badge, bar_chart, card, hbar_chart, kpi, layout, line_chart,
    set_nav_badge,
)
from app.config import settings
from app.services import runtime_settings as rt


@ui.page("/")
async def dashboard_page() -> None:
    await rt.refresh()               # ліміт Free, ціна Premium, інтервали — з урахуванням /settings
    with layout(at("nav_dashboard"), "/"):
        ov, series, breaks, expiring, problems, top, platforms, fb = await asyncio.gather(
            q.overview(), q.daily_series(30), q.breakdowns(), q.premium_expiring(14),
            q.problem_trackers(10), q.top_products(8), q.platforms_top(8), q.feedback_counts(),
        )
        set_nav_badge("/feedback", fb["new"])

        # ── KPI ──
        with ui.element("div").classes("w-full grid gap-3").style(
            "grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));"
        ):
            kpi(at("kpi_users"), ov.users_total, delta=ov.users_new_7d - ov.users_new_prev_7d,
                delta_label=at("kpi_users_delta"), icon="group", link="/users")
            kpi(at("kpi_premium"), ov.users_premium,
                delta_label=at("kpi_premium_delta", revenue=amoney(ov.revenue_month_uah, "UAH")),
                icon="workspace_premium", link="/users?tier=premium")
            kpi(at("kpi_new_7d"), ov.users_new_7d, delta_label=at("kpi_new_7d_delta", n=ov.users_new_30d),
                icon="person_add", link="/users")
            kpi(at("kpi_trackers"), ov.trackers_active,
                delta_label=at("kpi_trackers_delta", paused=ov.trackers_paused, errors=ov.trackers_error),
                icon="track_changes", link="/trackers")
            kpi(at("kpi_checks"), ov.checks_24h, delta=ov.checks_24h - ov.checks_prev_24h,
                delta_label=at("kpi_checks_delta"), icon="sync", link="/checks")
            kpi(at("kpi_notifs"), ov.notif_24h, delta=ov.notif_24h - ov.notif_prev_24h,
                delta_label=at("kpi_notifs_delta", failed=ov.notif_failed_24h), icon="notifications", link="/notifications")
            with ui.link(target="/feedback").style("text-decoration:none; color:inherit; display:block"):
                kpi(at("kpi_feedback"), fb["new"],
                    delta_label=at("kpi_feedback_delta", total=fb["total"], replied=fb["replied"]), icon="forum")

        # ── Динаміка ──
        with ui.element("div").classes("w-full grid gap-4").style(
            "grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));"
        ):
            with card(at("card_new_users"), at("card_by_day_30"), link="/users"):
                bar_chart(series["users"], color=SERIES[0])
            with card(at("card_checks"), at("card_by_day_30"), link="/checks"):
                line_chart(series["checks"], color=SERIES[2])
            with card(at("card_notifs"), at("card_by_day_30"), link="/notifications"):
                line_chart(series["notifications"], color=SERIES[1])

        # ── Розподіли ──
        with ui.element("div").classes("w-full grid gap-4").style(
            "grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));"
        ):
            with card(at("card_trackers_by_domain")):
                hbar_chart(breaks["domain"], color=SERIES[0])
            with card(at("card_languages")):
                hbar_chart(breaks["language"], color=SERIES[0])
            with card(at("card_currencies")):
                hbar_chart(breaks["currency"], color=SERIES[0])
            with card(at("card_event_types"), at("card_last_30")):
                hbar_chart([(event_label(k), v) for k, v in breaks["event"]], color=SERIES[0])

        # ── Платформи ──
        with card(at("card_platforms"), at("card_platforms_sub")):
            if not platforms:
                ui.label(at("platforms_empty")).classes("pg-muted").style("font-size:13px")
            else:
                cols = [
                    {"name": "name", "label": at("col_platform"), "field": "name", "align": "left"},
                    {"name": "domain", "label": at("col_domain"), "field": "domain", "align": "left", "classes": "pg-mono"},
                    {"name": "status", "label": at("col_status"), "field": "status", "align": "left"},
                    {"name": "active", "label": at("col_active"), "field": "active", "align": "right"},
                    {"name": "error_rate_24h", "label": at("col_errors_24h"), "field": "error_rate_24h", "align": "right"},
                    {"name": "healed_24h", "label": at("col_healed_24h"), "field": "healed_24h", "align": "right"},
                ]
                tbl = ui.table(columns=cols, rows=platforms, row_key="id").props("flat dense").classes("w-full")
                tbl.on("rowClick", lambda e: ui.navigate.to("/platforms"))
                tbl.add_slot("body-cell-name", r'''
                    <q-td :props="props"><span style="font-weight:600">{{ props.value }}</span></q-td>''')
                tbl.add_slot("body-cell-status", r'''
                    <q-td :props="props">
                      <span class="pg-badge" :class="{verified:'good', experimental:'warning', needs_js:'serious', blocked:'critical'}[props.value] || ''">
                        <span class="dot"></span>{{ __LABELS__[props.value] || props.value }}
                      </span>
                    </q-td>'''.replace("__LABELS__", slot_labels(PLATFORM_STATUS_KEYS)))
                tbl.add_slot("body-cell-error_rate_24h", r'''
                    <q-td :props="props">
                      <span v-if="props.value === null || props.value === undefined" class="pg-muted">—</span>
                      <span v-else :style="{color: props.value >= 30 ? 'var(--critical-ink)' : props.value >= 10 ? 'var(--serious)' : 'inherit', fontWeight: props.value >= 10 ? 600 : 400}">{{ props.value }}% <span class="pg-muted" style="font-weight:400">({{ props.row.fails_24h }}/{{ props.row.checks_24h }})</span></span>
                    </q-td>''')
            ui.link(at("platforms_all"), "/platforms").classes("pg-link").style("font-size:13px; margin-top: 10px; display:inline-block")

        # ── Таблиці ──
        with ui.element("div").classes("w-full grid gap-4").style(
            "grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));"
        ):
            with card(at("card_premium_expiring"), at("card_premium_expiring_sub", n=len(expiring))):
                if not expiring:
                    ui.label(at("premium_expiring_empty")).classes("pg-muted").style("font-size:13px")
                else:
                    cols = [
                        {"name": "name", "label": at("col_user_name"), "field": "name", "align": "left"},
                        {"name": "username", "label": at("col_username"), "field": "username", "align": "left"},
                        {"name": "until", "label": at("col_until"), "field": "until", "align": "left"},
                        {"name": "days_left", "label": at("col_days"), "field": "days_left", "align": "right", "sortable": True},
                    ]
                    tbl = ui.table(columns=cols, rows=expiring, row_key="id").props("flat dense").classes("w-full")
                    tbl.on("rowClick", lambda e: ui.navigate.to(f"/users/{e.args[1]['id']}"))
                    tbl.add_slot("body-cell-days_left", r'''
                        <q-td :props="props">
                          <span :style="{color: props.value <= 3 ? 'var(--serious)' : 'inherit', fontWeight: 600}">{{ props.value }}</span>
                        </q-td>''')

            with card(at("card_problem_trackers"), at("card_problem_trackers_sub")):
                if not problems:
                    with ui.row().classes("items-center gap-2"):
                        badge(at("all_ok"), "good")
                        ui.label(at("problem_trackers_empty")).classes("pg-muted").style("font-size:13px")
                else:
                    cols = [
                        {"name": "title", "label": at("col_product"), "field": "title", "align": "left"},
                        {"name": "source", "label": at("col_site"), "field": "source", "align": "left"},
                        {"name": "errors", "label": at("col_errors"), "field": "errors", "align": "right"},
                        {"name": "status", "label": at("col_status"), "field": "status", "align": "left"},
                        {"name": "last_checked", "label": at("col_checked"), "field": "last_checked", "align": "left"},
                    ]
                    tbl = ui.table(columns=cols, rows=problems, row_key="id").props("flat dense").classes("w-full")
                    tbl.on("rowClick", lambda e: ui.navigate.to(f"/users/{e.args[1]['user_id']}"))
                    tbl.add_slot("body-cell-title", r'''
                        <q-td :props="props"><span class="pg-ellipsis" style="max-width:260px">{{ props.value }}</span></q-td>''')
                    tbl.add_slot("body-cell-status", r'''
                        <q-td :props="props">
                          <span class="pg-badge" :class="props.value === 'error' ? 'critical' : (props.value === 'paused' || props.value === 'paused_limit') ? 'warning' : 'good'">
                            <span class="dot"></span>{{ __LABELS__[props.value] || props.value }}
                          </span>
                        </q-td>'''.replace("__LABELS__", slot_labels(TRACKER_STATUS_KEYS)))

            with card(at("card_top_products"), at("card_top_products_sub")):
                if not top:
                    ui.label(at("top_products_empty")).classes("pg-muted").style("font-size:13px")
                else:
                    hbar_chart(top, color=SERIES[0])

            with card(at("card_bot_settings"), at("card_bot_settings_sub")):
                rows = [
                    (at("setting_free_limit"), at("setting_free_limit_value", n=rt.cached("FREE_TIER_MAX_ITEMS"))),
                    (at("setting_premium_price"),
                     at("setting_premium_price_value", price=amoney(rt.cached("PREMIUM_PRICE_UAH"), "UAH"))),
                    (at("setting_free_interval"), at("setting_hours", n=rt.cached("FREE_TIER_CHECK_INTERVAL_MINUTES") // 60)),
                    (at("setting_premium_interval"), at("setting_minutes", n=rt.cached("PREMIUM_CHECK_INTERVAL_MINUTES"))),
                    (at("setting_database"), "SQLite" if settings.is_sqlite else "PostgreSQL"),
                    (at("setting_2fa"), at("enabled") if settings.ADMIN_2FA_ENABLED else at("disabled")),
                ]
                with ui.element("div").classes("w-full grid gap-y-2").style("grid-template-columns: 1fr auto; font-size:13.5px"):
                    for k, v in rows:
                        ui.label(k).classes("pg-muted")
                        ui.label(v).style("text-align:right; font-variant-numeric: tabular-nums")
