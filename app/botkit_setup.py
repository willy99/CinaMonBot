"""
Контекст спільного пакета botkit для ЦінаБот. Імпортується в кінці app/database/session.py, тож є в кожному
процесі, що працює з базою (бот, адмінка, ops, CLI, міграції). Тему адмінки додає enable_admin_ui()
(викликає app/admin/pages/__init__.py) — бот NiceGUI не тягне.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import botkit
from app.config import settings
from app.database.models import Admin, AdminAction, AppSetting, Heartbeat, User
from app.database.session import get_session, legacy_upgrade
from app.i18n import resolve_lang, t

ROOT = Path(__file__).resolve().parents[1]

CONTEXT = botkit.Context(
    app_name="ЦінаБот", settings=settings, get_session=get_session,
    models=botkit.Models(Admin, AdminAction, User, Heartbeat, AppSetting), root=ROOT,
    t=t, resolve_lang=resolve_lang, legacy_upgrade=legacy_upgrade,
    heartbeat_tick_seconds=300,            # dispatch щохвилини, але прогін буває довгим — поріг «не тікав» з запасом
    backup_prefix="priceguard-",
)
botkit.setup(CONTEXT)
import app.services.runtime_settings  # noqa: E402,F401 — реєструє редаговані параметри (rt.register)


def _readonly_rows(at) -> list[tuple[str, str]]:
    """Сторінка /settings: те, що змінюється лише в .env (з рестартом)."""
    from app.services.scrapers.render import setting as render_setting
    s = settings
    db = s.DATABASE_URL.split("///")[-1] if s.is_sqlite else s.DATABASE_URL.split("@")[-1]
    on_off = lambda v: at("enabled") if v else at("disabled")  # noqa: E731
    return [
        (at("setting_ro_database"), db),
        (at("setting_ro_2fa"), on_off(s.ADMIN_2FA_ENABLED)),
        (at("setting_ro_render"), f"{render_setting('RENDER_BROWSER_CHANNEL') or 'headless-shell'} · "
                                  f"fallback: {on_off(render_setting('RENDER_FALLBACK'))}"),
        (at("setting_ro_admin_tg"), str(s.ADMIN_TELEGRAM_ID or "—")),
    ]


def enable_admin_ui() -> None:
    from app.admin import theme
    from app.admin.i18n import at
    from app.timeutil import fmt

    ui = botkit.AdminUI(
        at=at, layout=theme.layout, card=theme.card, css=theme.CSS, apply_theme=theme.apply_theme,
        palette=theme.palette, settings_switches=theme.settings_switches, brand_icon="shield",
        brand_title="ЦінаБот Admin", fmt_dt=lambda d, pattern: fmt(d, pattern, "—"),
        badge=theme.badge, readonly_rows=lambda: _readonly_rows(at),
    )
    botkit.setup(replace(CONTEXT, admin_ui=ui))
