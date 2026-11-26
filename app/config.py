from functools import lru_cache
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ─── Telegram ────────────────────────────────────────────────
    BOT_TOKEN: SecretStr
    # Ліміт на користувача: будь-які апдейти (повідомлення, кнопки) за хвилину
    USER_REQUESTS_PER_MINUTE: int = 20

    # ─── Database ────────────────────────────────────────────────
    # Варіант 1: SQLite (просто вкажи шлях до файлу)
    #   DATABASE_URL=sqlite+aiosqlite:///./priceguard.db
    #
    # Варіант 2: PostgreSQL
    #   DATABASE_URL=postgresql+asyncpg://user:password@localhost/priceguard
    DATABASE_URL: str = "sqlite+aiosqlite:///./priceguard.db"

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.DATABASE_URL.startswith("postgresql")

    # ─── Scheduler ───────────────────────────────────────────────
    # Як часто перевіряти ціни (хвилини)
    FREE_TIER_CHECK_INTERVAL_MINUTES: int = 360    # 6 годин
    PREMIUM_CHECK_INTERVAL_MINUTES: int = 60       # 1 година
    # Аукціони (Premium): за N хв до кінця торгів — частіше (ставка наприкінці продовжує лот на +5 хв)
    AUCTION_END_WINDOW_MINUTES: int = 20
    AUCTION_END_INTERVAL_MINUTES: int = 2
    # Головний цикл планувальника (кожні N хвилин)
    SCHEDULER_INTERVAL_MINUTES: int = 15
    # Антиспам "знову в наявності": повторний RESTOCK по тому ж товару не раніше ніж через N днів
    # (якщо тільки ціна не стала нижчою, ніж була) — товар, що "мигає" в наявності, не спамить
    RESTOCK_COOLDOWN_DAYS: int = 7

    # ─── Scraping ────────────────────────────────────────────────
    SCRAPE_DELAY_SECONDS: float = 2.0
    # Скільки різних сторінок одного магазину перевіряємо за хвилину; решта — наступними хвилинами
    # (з однієї домашньої IP: сплеск запитів — найпевніший шлях під антибот)
    DOMAIN_MAX_CHECKS_PER_MINUTE: int = 6
    # Окремі межі для великих магазинів, які тримають більше: "rozetka.com.ua=12,prom.ua=10".
    # Навантажувальний тест (scripts/loadtest.py): на ~600+ юзерах Rozetka упирається в 6/хв, черга росте.
    DOMAIN_CAP_OVERRIDES: str = ""
    REQUEST_TIMEOUT_SECONDS: float = 15.0
    SCRAPE_MAX_RETRIES: int = 3

    # ─── JS-рендеринг (Playwright, опційно) ──────────────────────
    # Магазини з render_js=True у реєстрі парсяться через headless Chromium.
    # Без встановленого Playwright бот працює через httpx і лише попереджає в лог.
    RENDER_MAX_CONCURRENCY: int = 2          # одночасних сторінок у браузері
    RENDER_TIMEOUT_SECONDS: int = 35
    RENDER_FALLBACK: bool = True             # спробувати браузер, якщо httpx дав blocked/parse_failed
    RENDER_IDLE_CONTEXT_MINUTES: int = 10    # закривати браузер, якщо не було запитів
    RENDER_BROWSER_CHANNEL: str = "chromium" # "" → легша headless-shell (гірше проходить антиботи)

    FREE_TIER_MAX_ITEMS: int = 5
    PREMIUM_PRICE_UAH: int = 60

    # Реквізити для оплати Premium — лише з .env (у репозиторії їх бути не повинно)
    CARD_NUMBER: str = ""
    CARD_IBAN: str = ""



    # ─── AI Auto-Healing ────────────────────────────────────
    ANTHROPIC_API_KEY: str = ""
    # Telegram ID власника: адмін-команди, 2FA-коди, алерти. Лише з .env; 0 = адмін-функції вимкнені.
    ADMIN_TELEGRAM_ID: int = 0

    # ─── Admin dashboard (python admin.py) ───────────────────────
    ADMIN_PORT: int = 8088
    ADMIN_HOST: str = "127.0.0.1"          # 0.0.0.0 лише якщо є Tailscale/VPN або reverse proxy
    # Секрет для сесій дашборду. Порожній → генерується при старті (сесії злітають після рестарту).
    ADMIN_STORAGE_SECRET: str = ""
    ADMIN_SESSION_HOURS: int = 12
    ADMIN_2FA_ENABLED: bool = True         # код у Telegram після пароля
    ADMIN_2FA_CODE_TTL_SECONDS: int = 300
    ADMIN_LOGIN_MAX_ATTEMPTS: int = 5      # потім блок на ADMIN_LOGIN_LOCK_MINUTES
    ADMIN_LOGIN_LOCK_MINUTES: int = 15

    # ─── Payments (необов'язково на старті) ──────────────────────
    LIQPAY_PUBLIC_KEY: str = ""
    LIQPAY_PRIVATE_KEY: SecretStr = SecretStr("")

    # ─── Експлуатація (python ops.py backup | healthcheck) ───────
    BACKUP_DIR: str = "backups"
    BACKUP_KEEP_DAYS: int = 14
    BACKUP_REMOTE: str = ""                  # rsync-ціль поза машиною, напр. user@nas:/backups/priceguard
    HEALTHCHECK_PING_URL: str = ""           # healthchecks.io / Uptime Kuma push URL; порожньо — лише алерт адміну
    API_HEALTH_URL: str = ""                 # API немає — лишаємо порожнім

    # ─── Environment ─────────────────────────────────────────────
    ENVIRONMENT: str = "development"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()