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
    # Головний цикл планувальника (кожні N хвилин)
    SCHEDULER_INTERVAL_MINUTES: int = 15

    # ─── Scraping ────────────────────────────────────────────────
    SCRAPE_DELAY_SECONDS: float = 2.0
    REQUEST_TIMEOUT_SECONDS: float = 15.0
    SCRAPE_MAX_RETRIES: int = 3

    # ─── Business ────────────────────────────────────────────────
    FREE_TIER_MAX_ITEMS: int = 5
    PREMIUM_PRICE_UAH: int = 60


    # ─── AI Auto-Healing ────────────────────────────────────
    ANTHROPIC_API_KEY: str = ""
    ADMIN_TELEGRAM_ID: int = 431742835

    # ─── Payments (необов'язково на старті) ──────────────────────
    LIQPAY_PUBLIC_KEY: str = ""
    LIQPAY_PRIVATE_KEY: SecretStr = SecretStr("")

    # ─── Environment ─────────────────────────────────────────────
    ENVIRONMENT: str = "development"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()