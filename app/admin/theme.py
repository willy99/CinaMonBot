"""
Візуальна система адмінки: палітра, CSS, каркас сторінки, KPI-плитки, графіки.

Палітра — темна, з валідованої дата-віз палітри (категоріальні слоти 1–4,
статусні кольори, chrome). Текст ніколи не фарбується в колір серії.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Iterator

from nicegui import ui

from app.admin.auth import current_admin, logout
from app.i18n import fmt_money, fmt_percent

# ─── Палітра ─────────────────────────────────────────────────────

PAGE = "#0d0d0d"
SURFACE = "#1a1a19"
SURFACE_2 = "#232322"
BORDER = "rgba(255,255,255,0.10)"
INK = "#ffffff"
INK_2 = "#c3c2b7"
MUTED = "#898781"
GRID = "#2c2c2a"
AXIS = "#383835"

SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
SEQ = ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95"]
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
ACCENT = SERIES[0]

CSS = f"""
:root {{
  --page: {PAGE}; --surface: {SURFACE}; --surface-2: {SURFACE_2}; --border: {BORDER};
  --ink: {INK}; --ink-2: {INK_2}; --muted: {MUTED}; --grid: {GRID}; --accent: {ACCENT};
  --good: {STATUS['good']}; --warning: {STATUS['warning']}; --serious: {STATUS['serious']}; --critical: {STATUS['critical']};
}}
html, body, .q-page, .nicegui-content {{ background: var(--page) !important; color: var(--ink); }}
body {{ font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
.nicegui-content {{ padding: 0 !important; }}
.q-drawer {{ background: var(--surface) !important; border-right: 1px solid var(--border); }}
.q-header {{ background: var(--page) !important; border-bottom: 1px solid var(--border); }}
.q-footer {{ background: var(--page) !important; }}

.pg-nav a {{ display:flex; align-items:center; gap:12px; padding:10px 14px; border-radius:10px;
  color: var(--ink-2); text-decoration:none; font-size:14px; font-weight:500; transition: background .15s, color .15s; }}
.pg-nav a:hover {{ background: var(--surface-2); color: var(--ink); }}
.pg-nav a.active {{ background: rgba(57,135,229,0.14); color: var(--ink); }}
.pg-nav a.active .q-icon {{ color: var(--accent); }}
.pg-nav .q-icon {{ font-size: 20px; color: var(--muted); }}
.pg-nav-badge {{ margin-left:auto; min-width:20px; padding:0 6px; border-radius:999px; font-size:11px; font-weight:700; text-align:center;
  line-height:20px; background: var(--warning); color:#1a1a19; }}

.pg-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 18px 20px; }}
.pg-card-title {{ font-size: 13px; font-weight: 600; letter-spacing: .02em; color: var(--ink-2); text-transform: uppercase; }}
.pg-card-sub {{ font-size: 12px; color: var(--muted); }}

.pg-kpi {{ background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 16px 18px; min-width: 0; }}
.pg-kpi-label {{ font-size: 12px; font-weight: 500; color: var(--muted); letter-spacing: .02em; }}
.pg-kpi-value {{ font-size: 30px; font-weight: 700; color: var(--ink); line-height: 1.15; margin-top: 6px; }}
.pg-kpi-delta {{ font-size: 12px; color: var(--muted); margin-top: 6px; display:flex; align-items:center; gap:4px; }}
.pg-kpi-delta.up {{ color: var(--good); }}
.pg-kpi-delta.down {{ color: var(--serious); }}

.pg-badge {{ display:inline-flex; align-items:center; gap:6px; padding: 2px 10px; border-radius: 999px;
  font-size: 12px; font-weight: 600; border: 1px solid var(--border); color: var(--ink-2); background: var(--surface-2); white-space: nowrap; }}
.pg-badge .dot {{ width:8px; height:8px; border-radius:50%; background: var(--muted); }}
.pg-badge.premium {{ color: #ffd98a; border-color: rgba(250,178,25,.35); background: rgba(250,178,25,.10); }}
.pg-badge.premium .dot {{ background: var(--warning); }}
.pg-badge.good .dot {{ background: var(--good); }}
.pg-badge.critical {{ color: #ffb3b3; border-color: rgba(208,59,59,.35); background: rgba(208,59,59,.12); }}
.pg-badge.critical .dot {{ background: var(--critical); }}
.pg-badge.serious .dot {{ background: var(--serious); }}
.pg-badge.warning .dot {{ background: var(--warning); }}

.q-table {{ background: transparent !important; color: var(--ink-2) !important; }}
.q-table th {{ color: var(--muted) !important; font-weight: 600 !important; font-size: 12px !important; text-transform: uppercase; letter-spacing: .03em; border-color: var(--grid) !important; }}
.q-table td {{ border-color: var(--grid) !important; font-size: 13.5px; font-variant-numeric: tabular-nums; }}
.q-table tbody tr.clickable {{ cursor: pointer; }}
.q-table tbody tr:hover {{ background: var(--surface-2) !important; }}
.q-table__bottom {{ color: var(--muted) !important; border-top: 1px solid var(--grid) !important; }}
.q-table__card {{ background: transparent !important; box-shadow: none !important; }}
.q-field--outlined .q-field__control {{ background: var(--surface-2); border-radius: 10px; }}
.q-field--outlined .q-field__control:before {{ border-color: var(--border) !important; }}
.q-field__native, .q-field__input, .q-field__label {{ color: var(--ink) !important; }}
.q-field__label {{ color: var(--muted) !important; }}
.q-btn {{ border-radius: 10px; text-transform: none; font-weight: 600; letter-spacing: 0; }}
.q-chip {{ border-radius: 999px; }}
.q-dialog .q-card {{ background: var(--surface); color: var(--ink); border: 1px solid var(--border); border-radius: 14px; }}
.q-notification {{ border-radius: 12px; }}
.q-separator {{ background: var(--grid) !important; }}
.q-menu {{ background: var(--surface) !important; border: 1px solid var(--border); }}
.q-item {{ color: var(--ink-2); }}
.q-tab {{ text-transform: none; }}
.pg-mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; color: var(--ink-2); }}
.pg-h1 {{ font-size: 22px; font-weight: 700; color: var(--ink); letter-spacing: -0.01em; }}
.pg-muted {{ color: var(--muted); }}
.pg-link {{ color: var(--accent); text-decoration: none; }}
.pg-link:hover {{ text-decoration: underline; }}
.pg-card-link {{ text-decoration: none; color: var(--ink-2); }}
.pg-card-link:hover {{ color: var(--accent); }}
.pg-kpi-link .pg-kpi:hover {{ border-color: rgba(57,135,229,0.45); }}
.pg-pager {{ display:flex; align-items:center; gap:6px; color: var(--muted); font-size: 13px; }}
.pg-ellipsis {{ display:inline-block; max-width: 380px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; vertical-align: bottom; }}
.q-btn-toggle .q-btn {{ padding: 0 14px; }}
.q-table__bottom .q-table__control {{ font-size: 12px; }}
"""

NAV = [
    ("/", "dashboard", "Дашборд"),
    ("/users", "group", "Користувачі"),
    ("/trackers", "track_changes", "Трекери"),
    ("/platforms", "storefront", "Платформи"),
    ("/feedback", "forum", "Фідбек"),
    ("/admins", "admin_panel_settings", "Адміни"),
]

# Лічильники біля пунктів меню (зараз — нові фідбеки). Кеш на процес: layout() читає
# синхронно, а оновлює його сторінка фідбеку/дашборд і фонове оновлення раз на _BADGE_TTL.
NAV_BADGES: dict[str, int] = {}
_BADGE_TTL = 30.0
_badge_refreshed_at = 0.0


def set_nav_badge(path: str, count: int) -> None:
    global _badge_refreshed_at
    NAV_BADGES[path] = int(count or 0)
    _badge_refreshed_at = time.monotonic()


async def _refresh_nav_badges(labels: dict[str, ui.label]) -> None:
    """Фоном перечитує лічильники з бази і оновлює кеш + бейджі на поточній сторінці."""
    try:
        from app.admin import queries as q
        counts = await q.feedback_counts()
        set_nav_badge("/feedback", counts.get("new", 0))
        for path, lbl in labels.items():
            n = NAV_BADGES.get(path, 0)
            lbl.text = str(n)
            lbl.set_visibility(n > 0)
    except Exception:  # сторінка вже закрита / база недоступна — бейдж просто не оновиться
        pass


# ─── Каркас ──────────────────────────────────────────────────────

@contextmanager
def layout(title: str, active: str) -> Iterator[None]:
    """Сайдбар + хедер + контент. Використання: with layout("Дашборд", "/"): ..."""
    admin = current_admin()
    ui.add_css(CSS)
    ui.page_title(f"{title} · ЦінаБот Admin")
    ui.colors(primary=ACCENT, positive=STATUS["good"], negative=STATUS["critical"], warning=STATUS["warning"])

    with ui.left_drawer(value=True, fixed=True).props("width=232 breakpoint=900").classes("pg-drawer"):
        with ui.column().classes("w-full h-full justify-between no-wrap").style("padding: 18px 12px;"):
            with ui.column().classes("w-full gap-1"):
                with ui.row().classes("items-center gap-3 no-wrap").style("padding: 4px 10px 18px;"):
                    ui.icon("shield", size="26px").style(f"color:{ACCENT}")
                    with ui.column().classes("gap-0"):
                        ui.label("ЦінаБот").style("font-weight:700; font-size:16px; line-height:1.1")
                        ui.label("Admin").classes("pg-muted").style("font-size:12px")
                with ui.element("div").classes("pg-nav w-full flex flex-col gap-1"):
                    badge_labels: dict[str, ui.label] = {}
                    for path, icon, label in NAV:
                        cls = "active" if path == active else ""
                        with ui.link(target=path).classes(cls):
                            ui.icon(icon)
                            ui.label(label)
                            n = NAV_BADGES.get(path, 0)
                            b = ui.label(str(n)).classes("pg-nav-badge")
                            b.set_visibility(n > 0)
                            badge_labels[path] = b
                    if time.monotonic() - _badge_refreshed_at > _BADGE_TTL:
                        try:
                            asyncio.get_running_loop().create_task(_refresh_nav_badges(badge_labels))
                        except RuntimeError:
                            pass
            with ui.column().classes("w-full gap-1").style("padding: 0 4px;"):
                ui.separator()
                with ui.row().classes("items-center justify-between no-wrap w-full").style("padding: 8px 6px 0;"):
                    with ui.column().classes("gap-0"):
                        ui.label(admin["username"] if admin else "—").style("font-size:13px; font-weight:600")
                        ui.label("адміністратор").classes("pg-muted").style("font-size:11px")
                    ui.button(icon="logout", on_click=_do_logout).props("flat round dense").tooltip("Вийти")

    with ui.header(elevated=False).classes("items-center").style("padding: 14px 28px;"):
        ui.label(title).classes("pg-h1")
        ui.space()
        ui.label(datetime.now().strftime("%d.%m.%Y %H:%M")).classes("pg-muted").style("font-size:13px")

    with ui.column().classes("w-full").style("padding: 24px 28px 40px; max-width: 1440px; margin: 0 auto; gap: 20px;"):
        yield


def _do_logout() -> None:
    logout()
    ui.navigate.to("/login")


# ─── Компоненти ──────────────────────────────────────────────────

@contextmanager
def card(title: str | None = None, subtitle: str | None = None, classes: str = "",
         link: str | None = None) -> Iterator[None]:
    """Картка. link — заголовок стає посиланням "Назва →" на сторінку з деталями."""
    with ui.element("div").classes(f"pg-card w-full {classes}"):
        if title:
            with ui.row().classes("items-baseline justify-between w-full no-wrap").style("margin-bottom: 12px;"):
                if link:
                    ui.link(f"{title} →", link).classes("pg-card-title pg-card-link")
                else:
                    ui.label(title).classes("pg-card-title")
                if subtitle:
                    ui.label(subtitle).classes("pg-card-sub")
        yield


def kpi(label: str, value, delta: int | None = None, delta_label: str = "", icon: str | None = None,
        link: str | None = None) -> None:
    """Плитка з числом. delta: різниця з попереднім періодом (знак показує напрям). link — клік веде на сторінку."""
    if link:
        with ui.link(target=link).classes("pg-kpi-link").style("text-decoration:none; color:inherit; display:block"):
            _kpi_body(label, value, delta, delta_label, icon)
    else:
        _kpi_body(label, value, delta, delta_label, icon)


def _kpi_body(label: str, value, delta: int | None, delta_label: str, icon: str | None) -> None:
    with ui.element("div").classes("pg-kpi"):
        with ui.row().classes("items-center justify-between no-wrap w-full"):
            ui.label(label).classes("pg-kpi-label")
            if icon:
                ui.icon(icon, size="18px").style(f"color:{MUTED}")
        ui.label(fmt_int(value)).classes("pg-kpi-value")
        if delta is not None:
            cls = "up" if delta > 0 else "down" if delta < 0 else ""
            arrow = "north_east" if delta > 0 else "south_east" if delta < 0 else "remove"
            with ui.element("div").classes(f"pg-kpi-delta {cls}"):
                ui.icon(arrow, size="14px")
                ui.label(f"{delta:+d} {delta_label}".strip())
        elif delta_label:
            ui.label(delta_label).classes("pg-kpi-delta")


def badge(text: str, kind: str = "") -> None:
    with ui.element("span").classes(f"pg-badge {kind}"):
        ui.element("span").classes("dot")
        ui.label(text)


def status_badge(status: str) -> None:
    mapping = {
        "active": ("Активний", "good"),
        "paused": ("На паузі", "warning"),
        "paused_limit": ("Пауза: ліміт", "warning"),
        "error": ("Помилка", "critical"),
        "out_of_stock": ("Немає", "serious"),
    }
    text, kind = mapping.get(status, (status, ""))
    badge(text, kind)


def fmt_int(v) -> str:
    if isinstance(v, str):
        return v
    try:
        return f"{int(v):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def money(amount, currency: str | None) -> str:
    if amount is None:
        return "—"
    return fmt_money(Decimal(str(amount)), currency, "uk")


def target_label(target, currency: str | None, target_percent=None) -> str:
    """Колонка "Ціль": '−15 % (≤ 3 825 грн)' для цілі у відсотках, сума — для абсолютної, '—' без цілі."""
    if not target:
        return "—"
    amount = money(target, currency)
    if target_percent:
        return f"−{fmt_percent(target_percent)} % (≤ {amount})"
    return amount


# ─── Графіки (ECharts) ───────────────────────────────────────────

def _base_axes(x_labels: list[str]) -> dict:
    return {
        "xAxis": {
            "type": "category", "data": x_labels,
            "axisLine": {"lineStyle": {"color": AXIS}},
            "axisTick": {"show": False},
            "axisLabel": {"color": MUTED, "fontSize": 11, "hideOverlap": True},
        },
        "yAxis": {
            "type": "value", "minInterval": 1,
            "splitLine": {"lineStyle": {"color": GRID, "width": 1}},
            "axisLabel": {"color": MUTED, "fontSize": 11},
        },
        "grid": {"left": 8, "right": 12, "top": 16, "bottom": 8, "containLabel": True},
        "textStyle": {"fontFamily": "system-ui, -apple-system, 'Segoe UI', sans-serif"},
        "backgroundColor": "transparent",
    }


def _tooltip(trigger: str = "axis") -> dict:
    return {
        "trigger": trigger,
        "backgroundColor": SURFACE_2, "borderColor": BORDER, "borderWidth": 1,
        "textStyle": {"color": INK, "fontSize": 12},
        "axisPointer": {"type": "line" if trigger == "axis" else "none", "lineStyle": {"color": AXIS}},
    }


def bar_chart(series: list[tuple[str, int]], color: str = ACCENT, height: str = "240px", label_fmt: str | None = None):
    """Стовпчики по днях: одна серія, 24px макс, скруглення зверху."""
    labels = [d[5:].replace("-", ".") if len(d) == 10 else d for d, _ in series]
    values = [v for _, v in series]
    opt = _base_axes(labels)
    opt.update({
        "tooltip": _tooltip(),
        "series": [{
            "type": "bar", "data": values, "barMaxWidth": 24, "barCategoryGap": "35%",
            "itemStyle": {"color": color, "borderRadius": [4, 4, 0, 0]},
        }],
    })
    return ui.echart(opt).classes("w-full").style(f"height:{height}")


def line_chart(series: list[tuple[str, int]], color: str = ACCENT, height: str = "240px", area: bool = True):
    labels = [d[5:].replace("-", ".") if len(d) == 10 else d for d, _ in series]
    values = [v for _, v in series]
    opt = _base_axes(labels)
    opt.update({
        "tooltip": _tooltip(),
        "series": [{
            "type": "line", "data": values, "smooth": False, "symbol": "circle", "symbolSize": 8,
            "showSymbol": False,
            "lineStyle": {"width": 2, "color": color},
            "itemStyle": {"color": color, "borderColor": SURFACE, "borderWidth": 2},
            "areaStyle": {"color": color, "opacity": 0.10} if area else None,
        }],
    })
    return ui.echart(opt).classes("w-full").style(f"height:{height}")


def hbar_chart(items: list[tuple[str, int]], color: str = ACCENT, height: str | None = None):
    """Горизонтальні стовпчики для категорій (домени, мови, валюти). Один колір — величина."""
    if not items or all(v == 0 for _, v in items):
        return ui.label("Даних ще немає").classes("pg-muted").style("font-size:13px; padding: 6px 0;")
    items = list(reversed(items))  # найбільше зверху
    labels = [k for k, _ in items]
    values = [v for _, v in items]
    h = height or f"{max(120, 34 * len(items) + 30)}px"
    opt = {
        "tooltip": _tooltip("item"),
        "grid": {"left": 8, "right": 44, "top": 4, "bottom": 4, "containLabel": True},
        "xAxis": {"type": "value", "minInterval": 1, "splitLine": {"lineStyle": {"color": GRID}},
                  "axisLabel": {"color": MUTED, "fontSize": 11}},
        "yAxis": {"type": "category", "data": labels, "axisLine": {"show": False}, "axisTick": {"show": False},
                  "axisLabel": {"color": INK_2, "fontSize": 12}},
        "series": [{
            "type": "bar", "data": values, "barMaxWidth": 20,
            "itemStyle": {"color": color, "borderRadius": [0, 4, 4, 0]},
            "label": {"show": True, "position": "right", "color": INK_2, "fontSize": 12},
        }],
        "backgroundColor": "transparent",
    }
    return ui.echart(opt).classes("w-full").style(f"height:{h}")


def price_history_chart(points: list[tuple[str, float]], currency: str, target: float | None = None, height: str = "260px"):
    labels = [p[0] for p in points]
    values = [p[1] for p in points]
    opt = _base_axes(labels)
    opt["yAxis"]["minInterval"] = None
    opt["yAxis"]["scale"] = True
    opt["xAxis"]["axisLabel"]["formatter"] = "{value}"
    series = [{
        "name": f"Ціна, {currency}", "type": "line", "data": values, "step": "end",
        "symbol": "circle", "symbolSize": 8, "showSymbol": len(values) <= 40,
        "lineStyle": {"width": 2, "color": ACCENT},
        "itemStyle": {"color": ACCENT, "borderColor": SURFACE, "borderWidth": 2},
        "areaStyle": {"color": ACCENT, "opacity": 0.10},
    }]
    if target:
        series[0]["markLine"] = {
            "silent": True, "symbol": "none",
            "lineStyle": {"color": STATUS["warning"], "type": "dashed", "width": 1},
            "label": {"color": INK_2, "fontSize": 11, "formatter": f"ціль {target:g}"},
            "data": [{"yAxis": target}],
        }
    opt.update({"tooltip": _tooltip(), "series": series})
    return ui.echart(opt).classes("w-full").style(f"height:{height}")
