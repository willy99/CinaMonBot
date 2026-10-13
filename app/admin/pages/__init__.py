"""Імпорт реєструє сторінки: власні через @ui.page, а /login і /admins — зі спільного пакета botkit."""
from app.admin.pages import system, logs, dashboard, feedback, platforms, trackers, users  # noqa: F401
from app.botkit_setup import enable_admin_ui
from botkit.admin import pages as _botkit_pages

enable_admin_ui()          # тема й тексти адмінки → контекст botkit
_botkit_pages.register(with_settings=True)   # /login, /admins, /settings
