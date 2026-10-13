"""
Контекст проєкту для botkit. Викликати botkit.setup(...) один раз — у модулі, який імпортують усі процеси
(напр. app/botkit_setup.py, що його імпортує app/config.py або точки входу), ДО використання модулів botkit.

Налаштування (settings) мають поля, які читають модулі пакета:
  BOT_TOKEN (SecretStr), ADMIN_TELEGRAM_ID, DATABASE_URL,
  ADMIN_SESSION_HOURS, ADMIN_2FA_CODE_TTL_SECONDS, ADMIN_LOGIN_MAX_ATTEMPTS, ADMIN_LOGIN_LOCK_MINUTES,
  ADMIN_2FA_ENABLED, BACKUP_DIR, BACKUP_KEEP_DAYS, BACKUP_REMOTE, HEALTHCHECK_PING_URL, API_HEALTH_URL,
  USER_REQUESTS_PER_MINUTE, is_sqlite.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Models:
    """SQLAlchemy-моделі проєкту з потрібними полями (див. README: «Моделі»)."""
    Admin: Any
    AdminAction: Any
    User: Any
    Heartbeat: Any | None = None
    AppSetting: Any | None = None              # для botkit.runtime_settings (налаштування «на льоту»)


@dataclass(frozen=True)
class AdminUI:
    """Тема й тексти адмінки проєкту — для сторінок /login і /admins."""
    at: Callable[..., str]                      # переклад адмінки: at(key, **kw)
    layout: Callable[..., Any]                  # контекст-менеджер сторінки: layout(title, active_path)
    card: Callable[..., Any]
    css: str                                    # CSS теми (для сторінки логіну)
    apply_theme: Callable[[], None]
    palette: Callable[[], dict]
    settings_switches: Callable[[], None]       # перемикачі мови/теми
    brand_icon: str = "shield"                  # іконка на сторінці логіну
    brand_title: str | None = None              # None → at("page_title_suffix")
    fmt_dt: Callable[[Any, str], str] = field(default=lambda d, fmt: d.strftime(fmt) if d else "—")
    badge: Callable[..., Any] | None = None          # badge(text, kind) — для сторінки /settings
    readonly_rows: Callable[[], list[tuple[str, str]]] | None = None   # «лише з .env» на /settings


@dataclass(frozen=True)
class Context:
    app_name: str                                            # у 2FA-коді, алертах: «Snipket», «ЦінаБот»
    settings: Any
    get_session: Callable[[], Any]                           # async context manager з commit/rollback
    models: Models
    root: Path                                               # корінь проєкту (alembic.ini, logs/)
    t: Callable[..., str] | None = None                      # i18n бота: t(lang, key, **kw)
    resolve_lang: Callable[[str | None], str] | None = None
    admin_ui: AdminUI | None = None
    legacy_upgrade: Callable[[Any], Awaitable[None]] | None = None   # адитивна міграція баз до Alembic
    heartbeat_tick_seconds: Callable[[], Awaitable[int]] | int = 300
    backup_prefix: str = "db-"                                # префікс файлів бекапу: «snipket-», «priceguard-»
    alembic_ini: str = "alembic.ini"


_ctx: Context | None = None


def setup(context: Context) -> None:
    global _ctx
    _ctx = context


def ctx() -> Context:
    if _ctx is None:
        raise RuntimeError("botkit не налаштовано: виклич botkit.setup(Context(...)) при старті проєкту")
    return _ctx
