"""Дашборд: KPI, динаміка по днях, розподіли, Premium що закінчується, проблемні трекери."""
from __future__ import annotations

import asyncio

from nicegui import ui

from app.admin import queries as q
from app.admin.theme import (
    ACCENT, SERIES, STATUS, badge, bar_chart, card, hbar_chart, kpi, layout, line_chart, set_nav_badge, status_badge,
)
from app.config import settings


@ui.page("/")
async def dashboard_page() -> None:
    with layout("Дашборд", "/"):
        ov, series, breaks, expiring, problems, top, platforms, fb = await asyncio.gather(
            q.overview(), q.daily_series(30), q.breakdowns(), q.premium_expiring(14),
            q.problem_trackers(10), q.top_products(8), q.platforms_top(8), q.feedback_counts(),
        )
        set_nav_badge("/feedback", fb["new"])

        # ── KPI ──
        with ui.element("div").classes("w-full grid gap-3").style(
            "grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));"
        ):
            kpi("Користувачі", ov.users_total, delta=ov.users_new_7d - ov.users_new_prev_7d,
                delta_label="за 7 дн. vs попередні", icon="group", link="/users")
            kpi("Premium активних", ov.users_premium,
                delta_label=f"≈ {ov.revenue_month_uah} грн/міс", icon="workspace_premium", link="/users?tier=premium")
            kpi("Нових за 7 днів", ov.users_new_7d, delta_label=f"{ov.users_new_30d} за 30 днів", icon="person_add", link="/users")
            kpi("Активних трекерів", ov.trackers_active,
                delta_label=f"{ov.trackers_paused} на паузі · {ov.trackers_error} з помилкою", icon="track_changes", link="/trackers")
            kpi("Перевірок за 24 год", ov.checks_24h, delta=ov.checks_24h - ov.checks_prev_24h,
                delta_label="vs попередня доба", icon="sync", link="/checks")
            kpi("Сповіщень за 24 год", ov.notif_24h, delta=ov.notif_24h - ov.notif_prev_24h,
                delta_label=f"vs попередня доба · {ov.notif_failed_24h} невдалих", icon="notifications", link="/notifications")
            with ui.link(target="/feedback").style("text-decoration:none; color:inherit; display:block"):
                kpi("Нові фідбеки", fb["new"], delta_label=f"{fb['total']} всього · {fb['replied']} з відповіддю", icon="forum")

        # ── Динаміка ──
        with ui.element("div").classes("w-full grid gap-4").style(
            "grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));"
        ):
            with card("Нові користувачі", "по днях, 30 днів", link="/users"):
                bar_chart(series["users"], color=SERIES[0])
            with card("Перевірки цін", "по днях, 30 днів", link="/checks"):
                line_chart(series["checks"], color=SERIES[2])
            with card("Надіслані сповіщення", "по днях, 30 днів", link="/notifications"):
                line_chart(series["notifications"], color=SERIES[1])

        # ── Розподіли ──
        with ui.element("div").classes("w-full grid gap-4").style(
            "grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));"
        ):
            with card("Активні трекери по майданчиках"):
                hbar_chart(breaks["domain"], color=SERIES[0])
            with card("Мови користувачів"):
                hbar_chart(breaks["language"], color=SERIES[0])
            with card("Валюти активних трекерів"):
                hbar_chart(breaks["currency"], color=SERIES[0])
            with card("Типи сповіщень", "за 30 днів"):
                names = {"decrease": "Зниження ціни", "target_reached": "Досягнута ціль",
                         "restock": "Знову в наявності", "increase": "Підвищення"}
                hbar_chart([(names.get(k, k), v) for k, v in breaks["event"]], color=SERIES[0])

        # ── Платформи ──
        with card("Платформи", "топ-8 за активними трекерами"):
            if not platforms:
                ui.label("Поки нема трекерів").classes("pg-muted").style("font-size:13px")
            else:
                cols = [
                    {"name": "name", "label": "Платформа", "field": "name", "align": "left"},
                    {"name": "domain", "label": "Домен", "field": "domain", "align": "left", "classes": "pg-mono"},
                    {"name": "status", "label": "Статус", "field": "status", "align": "left"},
                    {"name": "active", "label": "Активних", "field": "active", "align": "right"},
                    {"name": "error_rate_24h", "label": "Помилок 24 год", "field": "error_rate_24h", "align": "right"},
                    {"name": "healed_24h", "label": "Вилікувано 24 год", "field": "healed_24h", "align": "right"},
                ]
                tbl = ui.table(columns=cols, rows=platforms, row_key="id").props("flat dense").classes("w-full")
                tbl.on("rowClick", lambda e: ui.navigate.to("/platforms"))
                tbl.add_slot("body-cell-name", r'''
                    <q-td :props="props"><span style="font-weight:600">{{ props.value }}</span></q-td>''')
                tbl.add_slot("body-cell-status", r'''
                    <q-td :props="props">
                      <span class="pg-badge" :class="{verified:'good', experimental:'warning', needs_js:'serious', blocked:'critical'}[props.value] || ''">
                        <span class="dot"></span>{{ {verified:'Перевірено', experimental:'Експериментально', needs_js:'Потрібен JS', blocked:'Блокує', other:'Інший сайт'}[props.value] || props.value }}
                      </span>
                    </q-td>''')
                tbl.add_slot("body-cell-error_rate_24h", r'''
                    <q-td :props="props">
                      <span v-if="props.value === null || props.value === undefined" style="color:#898781">—</span>
                      <span v-else :style="{color: props.value >= 30 ? '#ffb3b3' : props.value >= 10 ? '#ec835a' : 'inherit', fontWeight: props.value >= 10 ? 600 : 400}">{{ props.value }}% <span style="color:#898781; font-weight:400">({{ props.row.fails_24h }}/{{ props.row.checks_24h }})</span></span>
                    </q-td>''')
            ui.link("Усі платформи →", "/platforms").classes("pg-link").style("font-size:13px; margin-top: 10px; display:inline-block")

        # ── Таблиці ──
        with ui.element("div").classes("w-full grid gap-4").style(
            "grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));"
        ):
            with card("Premium закінчується", f"найближчі 14 днів · всього {len(expiring)}"):
                if not expiring:
                    ui.label("Нікого — усі підписки дійсні довше ніж 14 днів").classes("pg-muted").style("font-size:13px")
                else:
                    cols = [
                        {"name": "name", "label": "Юзер", "field": "name", "align": "left"},
                        {"name": "username", "label": "Username", "field": "username", "align": "left"},
                        {"name": "until", "label": "До", "field": "until", "align": "left"},
                        {"name": "days_left", "label": "Днів", "field": "days_left", "align": "right", "sortable": True},
                    ]
                    tbl = ui.table(columns=cols, rows=expiring, row_key="id").props("flat dense").classes("w-full")
                    tbl.on("rowClick", lambda e: ui.navigate.to(f"/users/{e.args[1]['id']}"))
                    tbl.add_slot("body-cell-days_left", r'''
                        <q-td :props="props">
                          <span :style="{color: props.value <= 3 ? '#ec835a' : 'inherit', fontWeight: 600}">{{ props.value }}</span>
                        </q-td>''')

            with card("Проблемні трекери", "помилки парсингу · 2+ поспіль"):
                if not problems:
                    with ui.row().classes("items-center gap-2"):
                        badge("Усе гаразд", "good")
                        ui.label("Жодного трекера з помилками").classes("pg-muted").style("font-size:13px")
                else:
                    cols = [
                        {"name": "title", "label": "Товар", "field": "title", "align": "left"},
                        {"name": "source", "label": "Сайт", "field": "source", "align": "left"},
                        {"name": "errors", "label": "Помилок", "field": "errors", "align": "right"},
                        {"name": "status", "label": "Статус", "field": "status", "align": "left"},
                        {"name": "last_checked", "label": "Перевірено", "field": "last_checked", "align": "left"},
                    ]
                    tbl = ui.table(columns=cols, rows=problems, row_key="id").props("flat dense").classes("w-full")
                    tbl.on("rowClick", lambda e: ui.navigate.to(f"/users/{e.args[1]['user_id']}"))
                    tbl.add_slot("body-cell-title", r'''
                        <q-td :props="props"><span class="pg-ellipsis" style="max-width:260px">{{ props.value }}</span></q-td>''')
                    tbl.add_slot("body-cell-status", r'''
                        <q-td :props="props">
                          <span class="pg-badge" :class="props.value === 'error' ? 'critical' : (props.value === 'paused' || props.value === 'paused_limit') ? 'warning' : 'good'">
                            <span class="dot"></span>{{ {active:'Активний', paused:'На паузі', paused_limit:'Пауза: ліміт', error:'Помилка', out_of_stock:'Немає'}[props.value] || props.value }}
                          </span>
                        </q-td>''')

            with card("Найпопулярніші товари", "скільки юзерів стежать"):
                if not top:
                    ui.label("Поки нема активних трекерів").classes("pg-muted").style("font-size:13px")
                else:
                    hbar_chart(top, color=SERIES[0])

            with card("Налаштування бота", "з .env / config.py"):
                rows = [
                    ("Безкоштовний ліміт", f"{settings.FREE_TIER_MAX_ITEMS} товарів"),
                    ("Ціна Premium", f"{settings.PREMIUM_PRICE_UAH} грн/міс"),
                    ("Інтервал Free", f"{settings.FREE_TIER_CHECK_INTERVAL_MINUTES // 60} год"),
                    ("Інтервал Premium", f"{settings.PREMIUM_CHECK_INTERVAL_MINUTES} хв"),
                    ("База даних", "SQLite" if settings.is_sqlite else "PostgreSQL"),
                    ("2FA адмінки", "увімкнено" if settings.ADMIN_2FA_ENABLED else "вимкнено"),
                ]
                with ui.element("div").classes("w-full grid gap-y-2").style("grid-template-columns: 1fr auto; font-size:13.5px"):
                    for k, v in rows:
                        ui.label(k).classes("pg-muted")
                        ui.label(v).style("text-align:right; font-variant-numeric: tabular-nums")
