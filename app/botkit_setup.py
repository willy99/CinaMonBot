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
from app.database.models import Admin, AdminAction, Heartbeat, User
from app.database.session import get_session, legacy_upgrade
from app.i18n import resolve_lang, t

ROOT = Path(__file__).resolve().parents[1]

CONTEXT = botkit.Context(
    app_name="ЦінаБот", settings=settings, get_session=get_session,
    models=botkit.Models(Admin, AdminAction, User, Heartbeat), root=ROOT,
    t=t, resolve_lang=resolve_lang, legacy_upgrade=legacy_upgrade,
    heartbeat_tick_seconds=300,            # dispatch_price_checks — кожні 5 хв
    backup_prefix="priceguard-",
)
botkit.setup(CONTEXT)


def enable_admin_ui() -> None:
    from app.admin import theme
    from app.admin.i18n import at
    from app.timeutil import fmt

    ui = botkit.AdminUI(
        at=at, layout=theme.layout, card=theme.card, css=theme.CSS, apply_theme=theme.apply_theme,
        palette=theme.palette, settings_switches=theme.settings_switches, brand_icon="shield",
        brand_title="ЦінаБот Admin", fmt_dt=lambda d, pattern: fmt(d, pattern, "—"),
    )
    botkit.setup(replace(CONTEXT, admin_ui=ui))
