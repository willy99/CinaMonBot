"""
Візуальна система адмінки: палітра, CSS, каркас сторінки, KPI-плитки, графіки.

Палітра — з валідованої дата-віз палітри (категоріальні слоти, статусні кольори, chrome)
у двох режимах: темна (типово) і світла. Текст ніколи не фарбується в колір серії.

- CSS працює на токенах (--page, --surface, --ink, ...): темні значення на :root,
  світлі — під body.body--light (Quasar вмикає клас через ui.dark_mode).
- Константи модуля (PAGE, SERIES, STATUS, ACCENT, MUTED, ...) — ТЕМНІ значення, щоб
  сторінки, які їх імпортують, не ламались. Для графіків беріть palette() — вона
  повертає кольори поточного режиму (theme_mode()).
- Тексти — через app.admin.i18n.at(); мова і тема перемикаються в сайдбарі
  (settings_switches()), зберігаються в app.storage.user і застосовуються після reload.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from nicegui import ui

from botkit.admin.auth import current_admin, logout
from app.admin.i18n import (
    ADMIN_LANGUAGES, adate, admin_lang, amoney, anum, at, set_admin_lang, set_theme_mode, theme_mode,
)
from app.i18n import fmt_percent

# ─── Палітра ─────────────────────────────────────────────────────
# Модульні константи = темний режим (див. docstring). Світлі значення — у _LIGHT / palette().

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

_DARK = {
    "page": PAGE, "surface": SURFACE, "surface2": SURFACE_2, "border": BORDER,
    "ink": INK, "ink2": INK_2, "muted": MUTED, "grid": GRID, "axis": AXIS,
    "series": SERIES, "seq": SEQ, "status": STATUS, "accent": ACCENT,
    # акценти, що залежать від режиму: підсвітка активного пункту меню, рамка плитки при hover,
    # текст бейджів premium/critical (на темному — світлий відтінок, на світлому — темний)
    "accent_soft": "rgba(57,135,229,0.14)", "accent_border": "rgba(57,135,229,0.45)",
    "premium_ink": "#ffd98a", "premium_bg": "rgba(250,178,25,.10)",
    "critical_ink": "#ffb3b3", "critical_bg": "rgba(208,59,59,.12)",
}
_LIGHT = {
    "page": "#f9f9f7", "surface": "#fcfcfb", "surface2": "#f0efec", "border": "rgba(11,11,11,0.10)",
    "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7",
    "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
    "seq": ["#184f95", "#1c5cab", "#256abf", "#2a78d6", "#3987e5", "#5598e7", "#6da7ec", "#86b6ef"],
    "status": STATUS, "accent": "#2a78d6",
    "accent_soft": "rgba(42,120,214,0.12)", "accent_border": "rgba(42,120,214,0.45)",
    "premium_ink": "#7a5200", "premium_bg": "rgba(250,178,25,.16)",
    "critical_ink": "#a32424", "critical_bg": "rgba(208,59,59,.10)",
}


def palette() -> dict:
    """Кольори поточного режиму (theme_mode()) для графіків і inline-стилів."""
    return _LIGHT if theme_mode() == "light" else _DARK


def _tokens(p: dict) -> str:
    return (
        f"--page: {p['page']}; --surface: {p['surface']}; --surface-2: {p['surface2']}; --border: {p['border']};"
        f" --ink: {p['ink']}; --ink-2: {p['ink2']}; --muted: {p['muted']}; --grid: {p['grid']}; --axis: {p['axis']};"
        f" --accent: {p['accent']}; --accent-soft: {p['accent_soft']}; --accent-border: {p['accent_border']};"
        f" --premium-ink: {p['premium_ink']}; --premium-bg: {p['premium_bg']};"
        f" --critical-ink: {p['critical_ink']}; --critical-bg: {p['critical_bg']};"
    )


CSS = f"""
:root {{
  {_tokens(_DARK)}
  --good: {STATUS['good']}; --warning: {STATUS['warning']}; --serious: {STATUS['serious']}; --critical: {STATUS['critical']};
}}
html:has(body.body--light), body.body--light {{ {_tokens(_LIGHT)} }}
body.body--light {{ color-scheme: light; }}
body.body--dark {{ color-scheme: dark; }}
html, body, .q-page, .nicegui-content {{ background: var(--page) !important; color: var(--ink); }}
body {{ font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
.nicegui-content {{ padding: 0 !important; }}
.q-drawer {{ background: var(--surface) !important; border-right: 1px solid var(--border); }}
.q-header {{ background: var(--page) !important; border-bottom: 1px solid var(--border); }}
.q-footer {{ background: var(--page) !important; }}

.pg-nav a {{ display:flex; align-items:center; gap:12px; padding:10px 14px; border-radius:10px;
  color: var(--ink-2); text-decoration:none; font-size:14px; font-weight:500; transition: background .15s, color .15s; }}
.pg-nav a:hover {{ background: var(--surface-2); color: var(--ink); }}
.pg-nav a.active {{ background: var(--accent-soft); color: var(--ink); }}
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
.pg-badge.premium {{ color: var(--premium-ink); border-color: rgba(250,178,25,.35); background: var(--premium-bg); }}
.pg-badge.premium .dot {{ background: var(--warning); }}
.pg-badge.good .dot {{ background: var(--good); }}
.pg-badge.critical {{ color: var(--critical-ink); border-color: rgba(208,59,59,.35); background: var(--critical-bg); }}
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
.pg-kpi-link .pg-kpi:hover {{ border-color: var(--accent-border); }}
.pg-pager {{ display:flex; align-items:center; gap:6px; color: var(--muted); font-size: 13px; }}
.pg-ellipsis {{ display:inline-block; max-width: 380px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; vertical-align: bottom; }}
.q-btn-toggle .q-btn {{ padding: 0 14px; }}
.q-table__bottom .q-table__control {{ font-size: 12px; }}
.q-table__bottom, .q-table__top {{ color: var(--muted) !important; }}
.q-field--outlined .q-field__control:hover:before {{ border-color: var(--muted) !important; }}
.q-field__marginal, .q-field__append .q-icon {{ color: var(--muted); }}
.q-menu .q-item--active, .q-menu .q-item.q-manual-focusable--focused {{ color: var(--accent); }}
.q-tooltip {{ background: var(--surface-2); color: var(--ink); border: 1px solid var(--border); }}
.q-card {{ background: var(--surface); color: var(--ink); }}
.q-dialog__backdrop {{ background: rgba(0,0,0,.45); }}
.pg-danger {{ color: var(--critical-ink); }}
.pg-sub {{ color: var(--muted); font-size: 11px; }}
.pg-switches {{ display:flex; align-items:center; justify-content:space-between; gap:8px; }}
.pg-switches .q-btn {{ min-height: 28px; }}
.pg-lang .q-btn {{ padding: 0 10px; font-size: 12px; }}
"""

# (шлях, іконка, ключ i18n) — підпис береться через at(key)
NAV = [
    ("/", "dashboard", "nav_dashboard"),
    ("/users", "group", "nav_users"),
    ("/trackers", "track_changes", "nav_trackers"),
    ("/checks", "sync", "nav_checks"),
    ("/notifications", "notifications", "nav_notifications"),
    ("/platforms", "storefront", "nav_platforms"),
    ("/feedback", "forum", "nav_feedback"),
    ("/system", "monitor_heart", "nav_system"),
    ("/settings", "tune", "nav_settings"),
    ("/admins", "admin_panel_settings", "nav_admins"),
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

def apply_theme() -> None:
    """CSS + режим (ui.dark_mode) + кольори Quasar для поточної сесії. Викликати на кожній сторінці."""
    p = palette()
    ui.add_css(CSS)
    ui.dark_mode(theme_mode() == "dark")
    ui.colors(primary=p["accent"], positive=STATUS["good"], negative=STATUS["critical"], warning=STATUS["warning"])


def _switch_lang(code: str) -> None:
    set_admin_lang(code)
    ui.navigate.reload()


def _toggle_theme() -> None:
    set_theme_mode("light" if theme_mode() == "dark" else "dark")
    ui.navigate.reload()


def settings_switches() -> None:
    """Компактний ряд: перемикач мови UA/EN + кнопка сонце/місяць. Зміна зберігається в сесії і перезавантажує сторінку.
    Маркери для тестів: lang-<code>, theme-toggle."""
    lang, mode = admin_lang(), theme_mode()
    with ui.element("div").classes("pg-switches w-full"):
        with ui.button_group().props("flat dense unelevated").classes("pg-lang"):
            for code, label in ADMIN_LANGUAGES.items():
                props = "dense no-caps unelevated color=primary" if code == lang else "dense no-caps flat"
                ui.button(label, on_click=lambda _, c=code: _switch_lang(c)).props(props).mark(f"lang-{code}")
        ui.button(icon="light_mode" if mode == "dark" else "dark_mode", on_click=_toggle_theme) \
            .props("flat round dense").mark("theme-toggle") \
            .tooltip(at("theme_to_light") if mode == "dark" else at("theme_to_dark"))


async def _recheck_session() -> None:
    from botkit.admin.auth import check_session
    if not await check_session():
        ui.navigate.to("/login")


@contextmanager
def layout(title: str, active: str) -> Iterator[None]:
    """Сайдбар + хедер + контент. Використання: with layout(at("nav_dashboard"), "/"): ..."""
    admin = current_admin()
    apply_theme()
    ui.timer(30, _recheck_session, immediate=False)   # деактивація / зміна пароля закривають і вже відкриті вкладки
    p = palette()
    ui.page_title(f"{title} · {at('page_title_suffix')}")

    with ui.left_drawer(value=True, fixed=True).props("width=232 breakpoint=900").classes("pg-drawer"):
        with ui.column().classes("w-full h-full justify-between no-wrap").style("padding: 18px 12px;"):
            with ui.column().classes("w-full gap-1"):
                with ui.row().classes("items-center gap-3 no-wrap").style("padding: 4px 10px 18px;"):
                    ui.icon("shield", size="26px").style(f"color:{p['accent']}")
                    with ui.column().classes("gap-0"):
                        ui.label("ЦінаБот").style("font-weight:700; font-size:16px; line-height:1.1")
                        ui.label(at("app_subtitle")).classes("pg-muted").style("font-size:12px")
                with ui.element("div").classes("pg-nav w-full flex flex-col gap-1"):
                    badge_labels: dict[str, ui.label] = {}
                    for path, icon, key in NAV:
                        cls = "active" if path == active else ""
                        with ui.link(target=path).classes(cls):
                            ui.icon(icon)
                            ui.label(at(key))
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
                with ui.element("div").classes("w-full").style("padding: 0 6px 6px;"):
                    settings_switches()
                ui.separator()
                with ui.row().classes("items-center justify-between no-wrap w-full").style("padding: 8px 6px 0;"):
                    with ui.column().classes("gap-0"):
                        ui.label(admin["username"] if admin else "—").style("font-size:13px; font-weight:600")
                        ui.label(at("admin_role")).classes("pg-muted").style("font-size:11px")
                    ui.button(icon="logout", on_click=_do_logout).props("flat round dense").tooltip(at("logout"))

    with ui.header(elevated=False).classes("items-center").style("padding: 14px 28px;"):
        ui.label(title).classes("pg-h1")
        ui.space()
        ui.label(adate(datetime.now(timezone.utc), with_time=True)).classes("pg-muted").style("font-size:13px")

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
                ui.icon(icon, size="18px").classes("pg-muted")
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


def slot_classes(mapping: dict[str, str]) -> str:
    """{код: css-клас} → JS-об'єкт в одинарних лапках для прив'язок атрибутів у Vue-слотах
    (:class="{active:'good'}[props.value]"). json.dumps тут не годиться: подвійні лапки обірвали б атрибут."""
    return "{" + ", ".join(f"{k}:'{v}'" for k, v in mapping.items()) + "}"


# статус трекера → (ключ i18n, вид бейджа); ті самі ключі — для Vue-слотів через slot_labels()
TRACKER_STATUS_KEYS = {
    "active": "status_active",
    "paused": "status_paused",
    "paused_limit": "status_paused_limit",
    "error": "status_error",
    "out_of_stock": "status_out_of_stock",
}
TRACKER_STATUS_KINDS = {"active": "good", "paused": "warning", "paused_limit": "warning",
                        "error": "critical", "out_of_stock": "serious"}


def status_badge(status: str) -> None:
    key = TRACKER_STATUS_KEYS.get(status)
    badge(at(key) if key else status, TRACKER_STATUS_KINDS.get(status, ""))


# статус платформи (registry / queries) → ключ i18n і вид бейджа; дашборд і /platforms беруть звідси
PLATFORM_STATUS_KEYS = {
    "verified": "platform_verified", "experimental": "platform_experimental",
    "needs_js": "platform_needs_js", "blocked": "platform_blocked", "other": "platform_other",
}
PLATFORM_STATUS_KINDS = {"verified": "good", "experimental": "warning", "needs_js": "serious",
                         "blocked": "critical", "other": ""}


def platform_status_badge(status: str) -> None:
    key = PLATFORM_STATUS_KEYS.get(status)
    badge(at(key) if key else status, PLATFORM_STATUS_KINDS.get(status, ""))


def fmt_int(v) -> str:
    """Ціле з розділювачем тисяч мовою адмінки ('1 234' / '1,234'); рядки — як є."""
    return anum(v)


def money(amount, currency: str | None) -> str:
    """Сума з валютою мовою адмінки (= app.admin.i18n.amoney)."""
    return amoney(amount, currency)


def target_label(target, currency: str | None, target_percent=None) -> str:
    """Колонка "Ціль": '−15 % (≤ 3 825 грн)' для цілі у відсотках, сума — для абсолютної, '—' без цілі."""
    if not target:
        return "—"
    amount = money(target, currency)
    if target_percent:
        return f"−{fmt_percent(target_percent, admin_lang())} % (≤ {amount})"
    return amount


# ─── Графіки (ECharts) ───────────────────────────────────────────

def _base_axes(x_labels: list[str]) -> dict:
    p = palette()
    return {
        "xAxis": {
            "type": "category", "data": x_labels,
            "axisLine": {"lineStyle": {"color": p["axis"]}},
            "axisTick": {"show": False},
            "axisLabel": {"color": p["muted"], "fontSize": 11, "hideOverlap": True},
        },
        "yAxis": {
            "type": "value", "minInterval": 1,
            "splitLine": {"lineStyle": {"color": p["grid"], "width": 1}},
            "axisLabel": {"color": p["muted"], "fontSize": 11},
        },
        "grid": {"left": 8, "right": 12, "top": 16, "bottom": 8, "containLabel": True},
        "textStyle": {"fontFamily": "system-ui, -apple-system, 'Segoe UI', sans-serif"},
        "backgroundColor": "transparent",
    }


def _tooltip(trigger: str = "axis") -> dict:
    p = palette()
    return {
        "trigger": trigger,
        "backgroundColor": p["surface2"], "borderColor": p["border"], "borderWidth": 1,
        "textStyle": {"color": p["ink"], "fontSize": 12},
        "axisPointer": {"type": "line" if trigger == "axis" else "none", "lineStyle": {"color": p["axis"]}},
    }


def _series_color(color: str | None) -> str:
    """Колір серії для поточного режиму. None / темний слот SERIES[i] → той самий слот світлої палітри."""
    p = palette()
    if color is None:
        return p["accent"]
    if color in SERIES:
        return p["series"][SERIES.index(color)]
    return color


def bar_chart(series: list[tuple[str, int]], color: str | None = None, height: str = "240px", label_fmt: str | None = None):
    """Стовпчики по днях: одна серія, 24px макс, скруглення зверху. color — слот SERIES[i] або None (= акцент)."""
    color = _series_color(color)
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


def line_chart(series: list[tuple[str, int]], color: str | None = None, height: str = "240px", area: bool = True):
    color = _series_color(color)
    p = palette()
    labels = [d[5:].replace("-", ".") if len(d) == 10 else d for d, _ in series]
    values = [v for _, v in series]
    opt = _base_axes(labels)
    opt.update({
        "tooltip": _tooltip(),
        "series": [{
            "type": "line", "data": values, "smooth": False, "symbol": "circle", "symbolSize": 8,
            "showSymbol": False,
            "lineStyle": {"width": 2, "color": color},
            "itemStyle": {"color": color, "borderColor": p["surface"], "borderWidth": 2},
            "areaStyle": {"color": color, "opacity": 0.10} if area else None,
        }],
    })
    return ui.echart(opt).classes("w-full").style(f"height:{height}")


def hbar_chart(items: list[tuple[str, int]], color: str | None = None, height: str | None = None):
    """Горизонтальні стовпчики для категорій (домени, мови, валюти). Один колір — величина."""
    if not items or all(v == 0 for _, v in items):
        return ui.label(at("chart_no_data")).classes("pg-muted").style("font-size:13px; padding: 6px 0;")
    color = _series_color(color)
    p = palette()
    items = list(reversed(items))  # найбільше зверху
    labels = [k for k, _ in items]
    values = [v for _, v in items]
    h = height or f"{max(120, 34 * len(items) + 30)}px"
    opt = {
        "tooltip": _tooltip("item"),
        "grid": {"left": 8, "right": 44, "top": 4, "bottom": 4, "containLabel": True},
        "xAxis": {"type": "value", "minInterval": 1, "splitLine": {"lineStyle": {"color": p["grid"]}},
                  "axisLabel": {"color": p["muted"], "fontSize": 11}},
        "yAxis": {"type": "category", "data": labels, "axisLine": {"show": False}, "axisTick": {"show": False},
                  "axisLabel": {"color": p["ink2"], "fontSize": 12}},
        "series": [{
            "type": "bar", "data": values, "barMaxWidth": 20,
            "itemStyle": {"color": color, "borderRadius": [0, 4, 4, 0]},
            "label": {"show": True, "position": "right", "color": p["ink2"], "fontSize": 12},
        }],
        "backgroundColor": "transparent",
    }
    return ui.echart(opt).classes("w-full").style(f"height:{h}")


def price_history_chart(points: list[tuple[str, float]], currency: str, target: float | None = None, height: str = "260px"):
    p = palette()
    accent = p["accent"]
    # Часова вісь: точки — моменти змін ціни (queries.tracker_history), тож відстань між ними
    # має відповідати часу, а не номеру точки
    opt = _base_axes([])
    opt["xAxis"] = {
        "type": "time",
        "axisLine": {"lineStyle": {"color": p["axis"]}},
        "axisTick": {"show": False},
        "splitLine": {"show": False},
        "axisLabel": {"color": p["muted"], "fontSize": 11, "hideOverlap": True},
    }
    opt["yAxis"]["minInterval"] = None
    opt["yAxis"]["scale"] = True
    data = [[pt[0], pt[1]] for pt in points]
    series = [{
        "name": at("chart_price_series", currency=currency), "type": "line", "data": data, "step": "end",
        "symbol": "circle", "symbolSize": 8, "showSymbol": len(data) <= 40,
        "lineStyle": {"width": 2, "color": accent},
        "itemStyle": {"color": accent, "borderColor": p["surface"], "borderWidth": 2},
        "areaStyle": {"color": accent, "opacity": 0.10},
    }]
    if target:
        series[0]["markLine"] = {
            "silent": True, "symbol": "none",
            "lineStyle": {"color": STATUS["warning"], "type": "dashed", "width": 1},
            # insideEndTop — підпис над лінією всередині графіка, а не за правим краєм (обрізався)
            "label": {"color": p["ink2"], "fontSize": 11, "position": "insideEndTop",
                      "formatter": at("chart_target", target=f"{target:g}")},
            "data": [{"yAxis": target}],
        }
    opt.update({"tooltip": _tooltip(), "series": series})
    return ui.echart(opt).classes("w-full").style(f"height:{height}")
