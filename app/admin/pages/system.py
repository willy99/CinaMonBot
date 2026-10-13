"""
/system — системна статистика: стан сервісу, ресурси машини, процеси, база даних.

Ресурси й процеси оновлюються кожні 10 с, база — кожні 60 с. Збір psutil іде
в окремому потоці (asyncio.to_thread), щоб не блокувати адмінку на вимірі CPU.
"""
from __future__ import annotations

import asyncio

from nicegui import ui

from app.admin import system as sysmod
from app.admin.i18n import admin_lang, adate, anum, at
from app.admin.theme import badge, card, kpi, layout

HOST_REFRESH_SECONDS = 10
DB_REFRESH_SECONDS = 60

ROLE_KEYS = {"bot": "system_role_bot", "admin": "system_role_admin", "browser": "system_role_browser"}

# Пороги свіжості останньої перевірки цін (с): free-трекери перевіряються раз на 6 год,
# premium — щогодини, диспетчер — кожні 5 хв.
CHECK_WARN_SECONDS = 2 * 3600
CHECK_CRIT_SECONDS = 7 * 3600


def _b(n) -> str:
    return sysmod.fmt_bytes(n, admin_lang())


def _dur(seconds) -> str:
    return sysmod.fmt_duration(seconds, admin_lang())


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    s = f"{v:.1f}"
    return (s.replace(".", ",") if admin_lang() == "uk" else s) + " %"


def meter(label: str, percent: float, detail: str) -> None:
    """Смуга завантаження: колір = рівень (норма / увага / критично), значення завжди підписане."""
    lvl = sysmod.level(percent)
    with ui.element("div").classes("w-full").style("margin-bottom: 14px;"):
        with ui.row().classes("w-full items-baseline justify-between no-wrap"):
            ui.label(label).style("font-size:13px; font-weight:600")
            ui.label(f"{_pct(percent)} · {detail}").classes("pg-muted").style(
                "font-size:12.5px; font-variant-numeric: tabular-nums")
        with ui.element("div").style(
            "height:8px; border-radius:4px; background: var(--surface-2); margin-top:6px; overflow:hidden"
        ):
            ui.element("div").style(
                f"height:100%; width:{max(0.0, min(100.0, percent)):.1f}%; border-radius:4px; background: var(--{lvl})"
            )


def info_rows(rows: list[tuple[str, str]]) -> None:
    with ui.element("div").classes("w-full grid gap-y-2").style("grid-template-columns: 1fr auto; font-size:13.5px"):
        for k, v in rows:
            ui.label(k).classes("pg-muted")
            ui.label(v).style("text-align:right; font-variant-numeric: tabular-nums")


@ui.page("/system")
async def system_page() -> None:
    with layout(at("nav_system"), "/system"):
        state: dict = {"snap": None, "db": None}

        @ui.refreshable
        def status_section() -> None:
            snap: sysmod.SystemSnapshot | None = state["snap"]
            db: sysmod.DbStats | None = state["db"]
            if snap is None:
                ui.label(at("system_loading")).classes("pg-muted")
                return
            procs = snap.processes
            with ui.row().classes("w-full items-center gap-2 wrap"):
                if "bot" in procs:
                    badge(at("system_bot_running", uptime=_dur(sysmod.seconds_since(procs["bot"].started))), "good")
                else:
                    badge(at("system_bot_down"), "critical")
                if "admin" in procs:
                    badge(at("system_admin_running", uptime=_dur(sysmod.seconds_since(procs["admin"].started))), "good")
                if "browser" in procs:
                    badge(at("system_browser_running", n=procs["browser"].count), "warning")
                else:
                    badge(at("system_browser_idle"))
                if db is not None:
                    ago = sysmod.seconds_since(db.last_check_at)
                    if ago is None:
                        badge(at("system_last_check_never"), "warning")
                    else:
                        kind = "critical" if ago >= CHECK_CRIT_SECONDS else "warning" if ago >= CHECK_WARN_SECONDS else "good"
                        badge(at("system_last_check", ago=_dur(ago)), kind)

            h = snap.host
            with ui.element("div").classes("w-full grid gap-3").style(
                "grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); margin-top: 6px;"
            ):
                kpi(at("system_kpi_cpu"), _pct(h.cpu_percent), delta_label=at("system_kpi_cpu_sub", n=h.cpu_logical),
                    icon="memory")
                kpi(at("system_kpi_ram"), _b(h.ram_used), delta_label=at("system_kpi_of", total=_b(h.ram_total)),
                    icon="developer_board")
                kpi(at("system_kpi_disk_free"), _b(h.disk_free), delta_label=at("system_kpi_of", total=_b(h.disk_total)),
                    icon="storage")
                share = snap.service_rss / h.ram_total * 100 if h.ram_total else 0
                kpi(at("system_kpi_service_ram"), _b(snap.service_rss),
                    delta_label=at("system_kpi_service_ram_sub", pct=_pct(share), cpu=_pct(snap.service_cpu_percent)),
                    icon="smart_toy")
                if db is not None:
                    kpi(at("system_kpi_db"), _b(db.file_bytes + db.wal_bytes),
                        delta_label=at("system_kpi_db_sub", rows=anum(db.total_rows)), icon="dataset")

        @ui.refreshable
        def host_section() -> None:
            snap: sysmod.SystemSnapshot | None = state["snap"]
            if snap is None:
                return
            h = snap.host
            with ui.element("div").classes("w-full grid gap-4").style(
                "grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));"
            ):
                with card(at("system_host_title"), at("system_refresh_every", n=HOST_REFRESH_SECONDS)):
                    meter(at("system_cpu"), h.cpu_percent, at("system_cores", logical=h.cpu_logical, physical=h.cpu_physical))
                    meter(at("system_ram"), h.ram_percent, at("system_used_of", used=_b(h.ram_used), total=_b(h.ram_total)))
                    if h.swap_total:
                        meter(at("system_swap"), h.swap_percent,
                              at("system_used_of", used=_b(h.swap_used), total=_b(h.swap_total)))
                    meter(at("system_disk"), h.disk_percent,
                          at("system_used_of", used=_b(h.disk_used), total=_b(h.disk_total)))
                    load = " / ".join(f"{x:g}" for x in h.load_avg) if h.load_avg else "—"
                    info_rows([
                        (at("system_os"), h.os_name),
                        (at("system_load"), load),
                        (at("system_uptime"), _dur(sysmod.seconds_since(h.boot_time))),
                        ("Python", h.python),
                    ])

                with card(at("system_procs_title"), at("system_procs_sub")):
                    rows = []
                    for role in ("bot", "admin", "browser"):
                        p = snap.processes.get(role)
                        if p is None:
                            continue
                        rows.append({
                            "role": at(ROLE_KEYS[role]),
                            # Головний PID; для браузера — перший із кількістю процесів
                            "pid": f"{p.pids[0]}" + (f" +{len(p.pids) - 1}" if role == "browser" and len(p.pids) > 1 else ""),
                            "ram": _b(p.rss),
                            "cpu": _pct(p.cpu_percent),
                            "threads": p.threads,
                            "uptime": _dur(sysmod.seconds_since(p.started)),
                            "restarts": "—" if p.restarts is None else str(p.restarts),
                        })
                    if not rows:
                        ui.label(at("system_no_procs")).classes("pg-muted").style("font-size:13px")
                    else:
                        cols = [
                            {"name": "role", "label": at("system_col_process"), "field": "role", "align": "left"},
                            {"name": "pid", "label": "PID", "field": "pid", "align": "left", "classes": "pg-mono"},
                            {"name": "ram", "label": at("system_col_ram"), "field": "ram", "align": "right"},
                            {"name": "cpu", "label": at("system_col_cpu"), "field": "cpu", "align": "right"},
                            {"name": "uptime", "label": at("system_col_uptime"), "field": "uptime", "align": "right"},
                            {"name": "restarts", "label": at("system_col_restarts"), "field": "restarts", "align": "right"},
                        ]
                        ui.table(columns=cols, rows=rows, row_key="role").props("flat dense").classes("w-full")
                    info_rows([
                        (at("system_logs"), _b(snap.logs_bytes)),
                        (at("system_collected"), adate(snap.collected_at, with_time=True)),
                    ])
                    ui.label(at("system_rss_note")).classes("pg-sub").style("margin-top: 8px;")

        @ui.refreshable
        def db_section() -> None:
            db: sysmod.DbStats | None = state["db"]
            if db is None:
                return
            total = sum(t.bytes or 0 for t in db.tables) or 1
            with card(at("system_db_title"), at("system_refresh_every", n=DB_REFRESH_SECONDS)):
                with ui.element("div").classes("w-full grid gap-6").style(
                    "grid-template-columns: minmax(260px, 1fr) minmax(0, 2fr);"
                ):
                    with ui.column().classes("gap-2"):
                        rows = [
                            (at("system_db_engine"), "SQLite" if db.engine == "sqlite" else "PostgreSQL"),
                            (at("system_db_size"), _b(db.file_bytes)),
                        ]
                        if db.engine == "sqlite":
                            rows += [
                                (at("system_db_wal"), _b(db.wal_bytes)),
                                (at("system_db_free"), _b(db.free_bytes)),
                            ]
                        rows += [
                            (at("system_db_rows"), anum(db.total_rows)),
                            (at("system_db_growth"),
                             at("system_db_growth_value", size=_b(db.growth_bytes_per_month))
                             if db.growth_bytes_per_month is not None else "—"),
                            (at("system_db_last_scrape"),
                             at("system_ago", ago=_dur(sysmod.seconds_since(db.last_scrape_at)))
                             if db.last_scrape_at else "—"),
                        ]
                        info_rows(rows)
                        if db.path:
                            ui.label(db.path).classes("pg-mono pg-sub").style("word-break: break-all; margin-top: 6px;")
                    trows = [{
                        "name": t.name,
                        "rows": anum(t.rows),
                        "size": _b(t.bytes) if t.bytes is not None else "—",
                        "share": round((t.bytes or 0) / total * 100, 1),
                    } for t in db.tables]
                    cols = [
                        {"name": "name", "label": at("system_col_table"), "field": "name", "align": "left", "classes": "pg-mono"},
                        {"name": "rows", "label": at("system_col_rows"), "field": "rows", "align": "right"},
                        {"name": "size", "label": at("system_col_size"), "field": "size", "align": "right"},
                        {"name": "share", "label": at("system_col_share"), "field": "share", "align": "left"},
                    ]
                    tbl = ui.table(columns=cols, rows=trows, row_key="name").props("flat dense").classes("w-full")
                    tbl.add_slot("body-cell-share", r'''
                        <q-td :props="props">
                          <div style="display:flex; align-items:center; gap:8px; min-width:140px">
                            <div style="flex:1; height:6px; border-radius:3px; background: var(--surface-2); overflow:hidden">
                              <div :style="{width: props.value + '%', height: '100%', background: 'var(--accent)', borderRadius: '3px'}"></div>
                            </div>
                            <span class="pg-muted" style="font-size:12px; min-width:42px; text-align:right">{{ props.value }}%</span>
                          </div>
                        </q-td>''')
            ui.label(at("system_db_note")).classes("pg-sub")

        async def refresh_host() -> None:
            try:
                state["snap"] = await asyncio.to_thread(sysmod.collect_system)
            except Exception as e:  # noqa: BLE001 — сторінка не має падати через psutil
                sysmod.logger.warning("system_collect_failed", error=str(e))
                return
            status_section.refresh()
            host_section.refresh()

        async def refresh_db() -> None:
            try:
                state["db"] = await sysmod.db_stats()
            except Exception as e:  # noqa: BLE001
                sysmod.logger.warning("system_db_stats_failed", error=str(e))
                return
            status_section.refresh()
            db_section.refresh()

        status_section()
        host_section()
        db_section()
        await asyncio.gather(refresh_db(), refresh_host())
        ui.timer(HOST_REFRESH_SECONDS, refresh_host)
        ui.timer(DB_REFRESH_SECONDS, refresh_db)
