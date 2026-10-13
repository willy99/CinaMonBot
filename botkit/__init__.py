"""
botkit — спільний код Snipket і ЦінаБот (PriceGuard): адмінка (автентифікація, логін, адміни, медіа),
логування, міграції Alembic, ops (бекап, перевірка живучості, heartbeat), ліміти частоти, запобіжник,
SSRF-захист, middleware бота.

Пакет нічого не імпортує з проєкту: проєкт один раз при старті передає свої налаштування, сесію БД,
моделі, i18n і тему через botkit.setup(...) (див. botkit/context.py). Підключається в проєкти через
git subtree у папку botkit/ — джерело правди ~/work/botkit (див. README.md).
"""
from botkit.context import AdminUI, Context, Models, ctx, setup

__all__ = ["AdminUI", "Context", "Models", "ctx", "setup"]
__version__ = "0.1.0"
